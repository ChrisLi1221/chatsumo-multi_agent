# agents/llm_agents.py
from typing import Optional, List, Dict, Any
from agents.contracts import Proposal
from agents.state_refactored import AgentState
import json, os, re, time, httpx
from agents.local_config import load_local_api_key

load_local_api_key()

Json = Dict[str, Any]


_COMPATIBLE_OPTIONAL_REQUEST_PARAMS = {"response_format"}


def resolved_llm_config(
    *,
    model: Optional[str] = None,
    openai_base_url: Optional[str] = None,
) -> Json:
    """Return the non-secret LLM endpoint configuration used at runtime."""
    return {
        "api": "openai_chat_completions",
        "model": model or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "base_url": (
            openai_base_url
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/"),
        "sampling": "model_default",
        "optional_parameter_fallback": "omit_unsupported_response_format",
    }


def unsupported_optional_request_parameter(
    response: httpx.Response,
    payload: Dict[str, Any],
) -> Optional[str]:
    """Identify an optional request field that an API explicitly rejects.

    Some OpenAI-compatible providers do not implement JSON response mode.  We
    only relax that field when the response names it and clearly reports that
    it is unsupported; unrelated client errors continue to fail normally.
    """
    if response.status_code != 400:
        return None

    error: Dict[str, Any] = {}
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
    except Exception:
        pass

    message = str(error.get("message") or response.text or "").lower()
    code = str(error.get("code") or "").lower()
    param = str(error.get("param") or "").lower()
    explicitly_unsupported = (
        "unsupported" in message
        or "does not support" in message
        or code in {"unsupported_parameter", "unsupported_value"}
    )
    if not explicitly_unsupported:
        return None

    if param in _COMPATIBLE_OPTIONAL_REQUEST_PARAMS and param in payload:
        return param
    for candidate in _COMPATIBLE_OPTIONAL_REQUEST_PARAMS:
        if candidate in payload and candidate in message:
            return candidate
    return None

############# system prompt #####################
BASE_SYSTEM_PROMPT = """You are a specialized agent for a SUMO traffic simulation system.

STRICT OUTPUT RULE:
- Return EXACTLY ONE JSON object. No code fences, no extra text.
- Schema: {"action": "...", "params": {...}, "reason": "...", "decision": {"summary": "..."}}

STRICT ROAD NAME RULE:
- ALWAYS use original OSM "name" tags with SPACES (e.g., "Main Street").
- Do NOT use underscores or identifier-style strings.
- If an input has underscores, convert them back to spaces.

OPERATIONAL RULES:
- If the latest observation contains "status":"error", use MODIFY to fix it based on 'candidates'.
- Use deterministic seeds (seed=42) unless requested otherwise.
- Choose STOP if the assigned goal is complete or no further action is needed.
"""

########## planner prompt ###############

PLANNER_SYSTEM_PROMPT = """You are a TOP-LEVEL planner for a SUMO traffic simulation multi-agent system.

Your job:
- Read USER_INPUT and the current simulation state.
- Decompose the task into a sequence of steps (1–5).
- For EACH step, write short natural-language instructions that include NECESSARY physical parameters for downstream agents.

Return EXACTLY ONE JSON object with this schema:
{
  "status": "ok",
  "version": "v1",
  "plan": [
    {
      "step_id": <int>,
      "goal": "short description",
      "agent_prompts": {
        "builder": "instruction string",
        "demand": "instruction string",
        "modifier": "instruction string",
        "runner_planner": "instruction string",
        "analyst": "instruction string"
      }
    }
  ],
  "reason": "explanation",
  "decision": { "summary": "brief summary" }
}

Parameter Alignment Contract (Include these in your natural language instructions):
1. For Builder: Must specify "city_name", "distance_miles" (e.g. 1.5), and "volume" (total trips).
2. For Modifier (Network Modification): 
   - Use ONLY if the user explicitly asks to modify, remove, or optimize the network/TLS.
   - For broad optimization requests such as "optimize the network", "reduce delay", or
     "reduce congestion", specify op="debate_optimize". Do NOT invent placeholder target IDs.
   - Use specific ops such as remove_edge, make_road_one_way, or tls_optimize_and_apply ONLY when the user or
     evidence provides a concrete edge_id or tls_id.
3. For Demand (CRITICAL Logic):
   - IF specific locations: Specify "from_edge", "to_edge", and "vph".
     *Example*: "Generate flow from Main Street to Congress Street with 800 vph."
   - ELSE: Specify "flows" (total vehicles).
     *Example*: "Generate 1200 random flows for the network."
4. For Runner: Specify if "gui" is needed and "steps" limit.
5. For Analyst: Specify the "metric" (mean_speed, co2, travel_time, waiting_time, density, congestion, bottlenecks, critical_roads, or all).
   - Use "congestion" when the user asks for congested roads, bottlenecks, delay hotspots, or critical roads.
   - Use "all" when the user asks for a general analysis report.

Pipeline Policy:
- Sequence Logic: Network must be fully ready before Demand generation.
- Standard flow (4 steps): Builder -> Demand -> Runner -> Analyst.
- Modification flow (5 steps): Builder -> Modifier -> Demand -> Runner -> Analyst.
- Each step MUST contain ONLY ONE active agent_prompt. All other agent_prompts in that step MUST be "".
- Reuse existing artifacts from STATE_SNAPSHOT whenever possible.
- If STATE_SNAPSHOT shows tripinfo=OK and the user only asks to show/analyze/report an existing metric
  (e.g., CO2, emissions, mean speed, travel time, waiting time, congestion, bottlenecks), output ONLY ONE
  Analyst step. Do NOT rebuild the network, regenerate demand, or rerun SUMO.
- If STATE_SNAPSHOT shows existing artifacts and the user asks to optimize, improve, reduce delay,
  reduce congestion, tune signals, or modify the network/TLS, this is NOT metric-only analysis.
  Output Modifier -> Runner -> Analyst so the intervention is applied, simulated, and evaluated.
- If STATE_SNAPSHOT shows cfg=OK but tripinfo=MISS and the user asks for results, output Runner -> Analyst.
- Only use Builder or Demand when the user explicitly asks for a new scenario, new city/network, new demand,
  route change, modification, or rerun.

Rules:
- Output MUST be valid JSON only.
- Road name handling: Use original OSM names with spaces (e.g., "Main Street").
- If NO modification is requested, SKIP the modifier step entirely.

Instruction Style Examples:
- "Build a network for Troy with 0.5 miles radius and 1000 volume." (Builder)
- "Remove the edge with ID '12345678' from the generated network." (Modifier)
- "Use op=debate_optimize to select an executable optimization for delay reduction." (Modifier)
- "Generate 1200 random flows for the modified network." (Demand)
- "Run simulation for 3600 steps with GUI enabled." (Runner)
- "Analyze the simulation results focusing on congestion." (Analyst)
- If tripinfo already exists and user says "show me the CO2": "Analyze the existing simulation results focusing on co2." (Analyst only)
"""

############### role ####################
ROLE_PROMPTS = {
    "builder": BASE_SYSTEM_PROMPT + """
You are the BUILDER Agent. Your sole responsibility is to generate network parameters.

DECISION LOGIC:
1. If the instruction mentions a city name (e.g., "Troy", "Albany"), you MUST use "type": "build_from_realworld".
2. Only use "type": "roundabout" if the user explicitly asks for a generic roundabout.

STRICT PARAMETER RULE:
- For "build_from_realworld": 
    - REQUIRED: "city_name" (string), "distance_miles" (float), "volume" (int).
    - Do NOT include "radius" or "lanes" unless building a roundabout.
- Your "params" object MUST be flat. Do NOT wrap it in extra keys like "OSM".

ACTION: "BUILD_NET"
- Example Correct: {"action": "BUILD_NET", "params": {"type": "build_from_realworld", "city_name": "Troy", "distance_miles": 0.31, "volume": 1000}}
""",

    "demand": BASE_SYSTEM_PROMPT + """
You are the DEMAND Agent. Your role is to generate traffic.

DECISION LOGIC:
1. IF the user specified specific streets or a path (e.g., "from Main St to State St"):
   - Use "type": "generate_flow_route"
   - Params: {"from_edge": str, "to_edge": str, "vph": int}
2. ELSE (If only 'medium traffic', '1000 vehicles', or no specific path is mentioned):
   - Use "type": "build_routes_random"
   - Params: {"flows": int} (default flows to 1000 if not specified)

STRICT RULE:
- Do NOT hallucinate edge IDs. If the user mentions street names, use the street names as strings.
- Only use 'generate_flow_route' if both 'from' and 'to' locations are clear.
""",

    "modifier": BASE_SYSTEM_PROMPT + """
You are the MODIFIER Agent. You modify an existing network.
ACTION: "MODIFY"
CAPABILITY CONSTRAINT:
- You may ONLY choose executable modifier operations listed below.
- If the requested intervention needs an unsupported operation such as adding lanes,
  reversing individual lanes, or creating new roads, choose "STOP"
  and explain the missing capability in decision.summary.

EXECUTABLE OPS (Required key "op"):
- "debate_optimize": use capability-constrained multi-agent debate to select an executable optimization. Set {"enable_bundles": true} only when the user explicitly requests a combined/multi-intervention optimization.
- "remove_edge": {"edge_id": str}. Use an exact SUMO edge ID; a street name is accepted only when unique.
- "make_road_one_way": {"keep_edge_id": str, "opposite_edge_id": str (optional)}. Keep the exact directed edge and remove its reverse edge; lane count is unchanged.
- "reroute_flows": {"avoid_edges": list[str] containing 1--3 distinct edge IDs, "reroute_fraction": 0.25|0.5|0.75|1.0, "min_route_retention": 1.0, "avoid_penalty_s": float (optional)}
- "tls_apply_program": {"tl_id": str, "cycle": float, "splits": list[float]}
- "tls_optimize_and_apply": {"tl_id": str, "cycle": float}
- "tls_set_program": {"tls_id": str, "program_xml": str}

DECISION LOGIC:
- For broad requests such as "optimize", "reduce congestion", "reduce delay", or
  "improve traffic" without a specific requested operation, choose op="debate_optimize".
""",

    "runner_planner": BASE_SYSTEM_PROMPT + """
You are the RUNNER Agent. You execute the simulation.
ACTION: "RUN"
PARAMS CONTRACT:
- Optional: {"steps": int, "gui": bool}
GUIDANCE: Set gui to true only if explicitly requested.
""",

    "analyst": BASE_SYSTEM_PROMPT + """
You are the ANALYST Agent. You interpret results.
ACTION: "ANALYZE"
PARAMS CONTRACT (Required key "metric"):
- Values:
  - "mean_speed": average vehicle speed from tripinfo.xml
  - "travel_time": average and top longest travel times from tripinfo.xml
  - "waiting_time": waiting/time-loss delay summary from tripinfo.xml
  - "co2": total emissions from tripinfo.xml
  - "density": edge density ranking from edgedata.xml
  - "congestion": congested roads, bottlenecks, and critical roads from edgedata.xml + net.xml
  - "bottlenecks": same as congestion
  - "critical_roads": same as congestion
  - "compare": compare recent stored runs from SQLite
  - "report": LLM-written concise report based on metric tool evidence
  - "all": LLM-written comprehensive report based on speed, travel time, emissions, and network diagnosis

DECISION LOGIC:
- If the user asks for congested roads, bottlenecks, critical roads, network diagnosis, or delay hotspots, choose "congestion".
- If the user asks only for mean speed, choose "mean_speed".
- If the user asks for travel time or longest trips, choose "travel_time".
- If the user asks for emissions, CO2, fuel, or pollution, choose "co2".
- If the user asks to compare runs, compare previous/current results, or before-after performance, choose "compare".
- If the user asks for a short report, summary report, or natural-language interpretation, choose "report".
- If the user asks for a general report or multiple metrics, choose "all".
"""
}

# -----------------------------
# Agents
# -----------------------------
def planner_agent(user_input: str, state: AgentState) -> Json:
    """
    Convert user input into a structured JSON plan with per-agent prompts.
    The returned object is already a dict (call_llm parses JSON).
    """
    # Give planner both the user input and state snapshot
    user_prompt = "USER_INPUT:\n" + (
        user_input or ""
    ).strip() + "\n\n" "STATE_SNAPSHOT:\n" + render_state_to_text(state)

    return call_llm(
        system=PLANNER_SYSTEM_PROMPT,
        user=user_prompt,
        force_json=True,
        state=state,
        usage_scope="planner",
    )


def builder_agent(state: AgentState) -> Optional[Proposal]:
    out = _planner_call("builder", state)
    print(f"[builder_agent] out={out}")
    action = out.get("action")
    params = out.get("params", {}) or {}
    summary = (out.get("decision", {}) or {}).get("summary", action)

    if action in ("BUILD_NET",):
        # 把 planner 的意图映射成 provider 调用
        params.pop("type", None)

        return Proposal(
            agent_id="builder",
            summary=summary,
            changes=[
                {
                    "tool": "provider_sumo_builder",
                    "action": "build_from_realworld",  # 你可换成 write_sumocfg / roundabout etc
                    "args": params,
                }
            ],
        )
    return None


def demand_agent(state: AgentState) -> Optional[Proposal]:
    out = _planner_call("demand", state)
    params = out.get("params", {}) or {}
    
    # 1. 识别动作类型（从 Prompt 中获取，或者根据参数判断）
    # 如果有 from_edge 则用 flow，否则用 random
    if "from_edge" in params:
        action_type = "generate_flow_route"
        route_args = {
            "net_path": state.artifacts.net,
            "from_edge": params["from_edge"],
            "to_edge": params["to_edge"],
            "vph": params.get("vph", 600),
            # route_output_path 由 orchestrator 自动注入
        }
    else:
        action_type = "build_routes_random"
        route_args = {
            "net": state.artifacts.net,
            "flows": params.get("flows", 1000),
            "period": 3600,
            # out_dir 由 orchestrator 自动注入
        }

    # 2. 返回包含两个 Step 的 Proposal
    return Proposal(
        agent_id="demand",
        summary="Generating routes and writing SUMO config",
        changes=[
            # 第一步：生成路由
            {
                "tool": "provider_traffic_generator",
                "action": action_type,
                "args": route_args
            },
            # 第二步：生成配置文件 (使用你刚写的 write_sumocfg)
            {
                "tool": "provider_traffic_generator",
                "action": "write_sumocfg",
                "args": {
                    "net": state.artifacts.net,
                    # 注意：此时 state.artifacts.routes 可能还没更新
                    # 我们在 orchestrator 中会处理这个占位符
                    "routes": "routes.rou.xml" 
                }
            }
        ]
    )


def modifier_agent(state: AgentState) -> Optional[Proposal]:
    out = _planner_call("modifier", state)
    print(f"[modifier_agent] out={out}")
    action = out.get("action")
    params = out.get("params", {}) or {}
    summary = (out.get("decision", {}) or {}).get("summary", action)

    if action == "MODIFY":
        op = str(params.get("op") or "").strip()
        placeholder_ids = {"existing_tls", "current_tls", "default_tls", "tls_id", "target_id"}
        requested_target = str(params.get("tl_id") or params.get("tls_id") or params.get("target_id") or "").strip()
        user_msg = str((state.context or {}).get(CTX_USER_INPUT_KEY) or (state.context or {}).get("last_user_msg") or "").lower()
        broad_optimization = any(
            term in user_msg
            for term in [
                "optimize", "optimise", "improve", "reduce delay", "reduce congestion",
                "优化", "改善", "降低延误", "减少延误", "降低拥堵", "减少拥堵",
            ]
        )
        if op != "debate_optimize" and broad_optimization and (
            not requested_target or requested_target.lower() in placeholder_ids
        ):
            params = {"op": "debate_optimize", "objective_profile": "delay"}
            summary = "Use capability-constrained debate optimizer to reduce delay."

        return Proposal(
            agent_id="modifier",
            summary=summary,
            changes=[
                {
                    "tool": "provider_sumo_modifier",
                    "action": params.pop("op", "tls_optimize_and_apply"),
                    "args": params,
                }
            ],
        )
    return None


def analyst_agent(state: AgentState) -> Optional[Proposal]:
    out = _planner_call("analyst", state)
    print(f"[analyst_agent] out={out}")
    action = out.get("action")
    params = out.get("params", {}) or {}

    if action == "ANALYZE":
        metric = str(params.get("metric") or "all").strip().lower()
        top_n = int(params.get("top_n") or 10)

        metric_aliases = {
            "average_speed": "mean_speed",
            "speed": "mean_speed",
            "avg_speed": "mean_speed",
            "delay": "waiting_time",
            "time_loss": "waiting_time",
            "emissions": "co2",
            "emission": "co2",
            "fuel": "co2",
            "pollution": "co2",
            "bottleneck": "congestion",
            "bottlenecks": "congestion",
            "critical": "congestion",
            "critical_roads": "congestion",
            "hotspots": "congestion",
            "network_diagnosis": "congestion",
            "brief_report": "report",
            "short_report": "report",
            "summary_report": "report",
            "traffic_report": "report",
            "report": "report",
            "compare": "compare",
            "comparison": "compare",
            "before_after": "compare",
        }
        metric = metric_aliases.get(metric, metric)

        def metric_change(tool_action: str, args: Optional[dict] = None) -> dict:
            return {
                "tool": "provider_metrics",
                "action": tool_action,
                "args": args or {},
            }

        if metric == "mean_speed":
            changes = [metric_change("tripinfo_mean_speed", {"unit": "km/h"})]
        elif metric in {"travel_time", "waiting_time"}:
            changes = [metric_change("tripinfo_travel_time_summary", {"top_n": top_n})]
        elif metric == "co2":
            changes = [metric_change("emissions_total", {"unit": params.get("unit", "kg")})]
        elif metric == "density":
            changes = [metric_change("edgedata_density_summary", {"top_n": top_n})]
        elif metric == "congestion":
            changes = [metric_change("network_diagnosis", {"top_n": top_n})]
        elif metric == "report":
            changes = [
                metric_change("tripinfo_mean_speed", {"unit": "km/h"}),
                metric_change("tripinfo_travel_time_summary", {"top_n": top_n}),
                metric_change("emissions_total", {"unit": params.get("unit", "kg")}),
                metric_change("network_diagnosis", {"top_n": top_n}),
                metric_change("llm_report", {"style": "brief", "top_n": top_n}),
            ]
        elif metric == "compare":
            changes = [
                metric_change("compare_runs", {"limit": int(params.get("limit") or 2)}),
                metric_change("llm_report", {"style": "comparison", "top_n": top_n}),
            ]
        elif metric == "all":
            changes = [
                metric_change("tripinfo_mean_speed", {"unit": "km/h"}),
                metric_change("tripinfo_travel_time_summary", {"top_n": top_n}),
                metric_change("emissions_total", {"unit": params.get("unit", "kg")}),
                metric_change("network_diagnosis", {"top_n": top_n}),
                metric_change("llm_report", {"style": "brief", "top_n": top_n}),
            ]
        else:
            changes = [metric_change("network_diagnosis", {"top_n": top_n})]

        tool_names = ", ".join(c["action"] for c in changes)
        return Proposal(
            agent_id="analyst",
            summary=f"Analyze {metric} using metrics tool(s): {tool_names}",
            changes=changes,
            validation_plan={
                "metric": metric,
                "requires": ["tripinfo.xml"] if metric not in {"density", "congestion", "report"} else ["net.xml", "edgedata.xml", "tripinfo.xml"],
            },
        )
    return None


def run_planner_agent(state: AgentState) -> List[Json]:
    out = _planner_call("runner_planner", state)
    print(f"[run_planner_agent] out={out}")
    action = out.get("action")
    params = out.get("params", {}) or {}
    if action == "RUN":
        # 统一成 list
        return [params]
    return []


REPORT_SYSTEM_PROMPT = """You are a transportation systems analyst writing a SUMO simulation report.

Rules:
- Use ONLY the provided metric tool evidence.
- Do not invent roads, values, causes, or interventions not supported by evidence.
- Write in the same language as the user's latest request when obvious.
- If style="brief", write 5-7 sentences.
- If style="comparison", compare the previous and current run, report KPI deltas, say whether performance improved, and mention the likely reason only if supported by evidence.
- If style="technical", write a structured report with these sections:
  1. Overall Traffic State
  2. Key Performance Indicators
  3. Congestion and Bottlenecks
  4. Interpretation
  5. Recommended Next Steps
- Use concrete numbers from evidence whenever available.
- Mention top bottleneck roads and critical roads if provided.
- Explain what the metrics imply, not just list them.
- Keep recommendations actionable and tied to observed bottlenecks.

Return EXACTLY ONE JSON object:
{"report": "..."}
"""


def generate_analyst_report(
    state: AgentState,
    *,
    analyst_results: List[Json],
    style: str = "brief",
) -> str:
    """Generate an LLM-written report from already-executed metric tool results."""
    ctx = state.context if isinstance(state.context, dict) else {}
    user_msg = ctx.get(CTX_USER_INPUT_KEY) or ctx.get("last_user_msg") or ""
    evidence = {
        "style": style,
        "user_request": user_msg,
        "artifacts": _get_artifacts(state),
        "metric_tool_results": analyst_results,
    }

    out = call_llm(
        system=REPORT_SYSTEM_PROMPT,
        user=json.dumps(evidence, ensure_ascii=False, default=str),
        force_json=True,
        state=state,
        usage_scope="analyst_report",
        max_tokens=900,
    )
    report = str(out.get("report") or "").strip()
    if not report:
        raise RuntimeError(f"analyst_report returned empty report: {out}")
    return report


_PLANNER_FN = None


def set_planner(fn):
    """
    fn(role: str, state: AgentState) -> dict
    Set the backend planner for all role agents.
    """
    global _PLANNER_FN
    _PLANNER_FN = fn


# -----------------------------
# Helper functions
# -----------------------------

CTX_PLAN_KEY = "current_plan"
CTX_STEP_IDX_KEY = "current_plan_step_idx"
CTX_USER_INPUT_KEY = "last_user_input"


def set_current_plan(
    state: AgentState, plan: Json, *, step_idx: int = 0, user_input: str = ""
) -> None:
    state.context = state.context or {}
    state.context[CTX_PLAN_KEY] = plan
    state.context[CTX_STEP_IDX_KEY] = int(step_idx)
    if user_input:
        state.context[CTX_USER_INPUT_KEY] = user_input


def _get_plan_prompt_for_role(state: AgentState, role: str) -> Optional[str]:
    ctx = state.context or {}
    plan = ctx.get(CTX_PLAN_KEY) or {}
    step_idx = int(ctx.get(CTX_STEP_IDX_KEY, 0) or 0)

    steps = plan.get("plan") or []
    if not steps or step_idx >= len(steps):
        return None

    agent_prompts = steps[step_idx].get("agent_prompts")
    if agent_prompts is None:
        return None

    if role not in agent_prompts:
        return None

    p = agent_prompts.get(role)

    if isinstance(p, str):
        if p.strip() == "":
            return ""
        return p.strip()

    return None


def _j(x: Any, n: int = 240) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:
        s = str(x)
    return s if len(s) <= n else s[: n - 3] + "..."


def _exists(p: Optional[str]) -> bool:
    return bool(p) and os.path.exists(p)


def _get_artifacts(state: Any) -> Dict[str, Optional[str]]:
    art = getattr(state, "artifacts", None)

    def g(name: str) -> Optional[str]:
        if art is None:
            return None
        if isinstance(art, dict):
            return art.get(name)
        return getattr(art, name, None)

    return {
        "workspace": getattr(state, "workspace", None) or g("workspace"),
        "net": g("net") or getattr(state, "net", None),
        "routes": g("routes") or getattr(state, "routes", None),
        "cfg": g("cfg") or getattr(state, "cfg", None),
    }


def _history(state: Any) -> List[Any]:
    h = getattr(state, "history", None)
    return h if isinstance(h, list) else []


def _latest_error(hist: List[Any]) -> Optional[str]:
    for step in reversed(hist[-20:]):
        obs = getattr(step, "observation", None)
        if isinstance(obs, dict):
            if obs.get("status") == "error" or "error" in obs:
                return _j(obs, 500)
        if isinstance(obs, str) and ("Traceback" in obs or "ERROR" in obs):
            lines = obs.splitlines()
            return "\n".join(lines[-12:]) if len(lines) > 12 else obs
    return None


def _latest_kpi(state: Any) -> Optional[Dict[str, Any]]:
    k = getattr(state, "last_kpi", None)
    if k is None:
        ctx = getattr(state, "context", None)
        if isinstance(ctx, dict):
            k = ctx.get("last_kpi")
    if k is None:
        return None
    if isinstance(k, dict):
        return k
    if hasattr(k, "to_dict"):
        try:
            return k.to_dict()
        except Exception:
            pass
    return {"kpi": str(k)}


def render_state_to_text(
    state: Any, *, max_steps: int = 6, max_chars: int = 2500
) -> str:
    goal = getattr(state, "goal", None)
    task_id = getattr(state, "task_id", None)
    scenario_id = getattr(state, "scenario_id", None)
    agent_id = getattr(state, "agent_id", None)

    a = _get_artifacts(state)
    hist = _history(state)

    # recent steps: keep only action + tiny params + tiny obs
    step_lines = []
    for s in hist[-max_steps:]:
        idx = getattr(s, "idx", None)
        act = getattr(s, "action", "")
        params = getattr(s, "params", {}) or {}
        obs = getattr(s, "observation", None)
        head = f"[{idx}] {act}" if idx is not None else act
        line = f"{head} | p={_j(params,120)}"
        if obs is not None:
            line += f" | o={_j(obs,160)}"
        step_lines.append(line)

    parts = []
    parts.append(f"GOAL: {goal or ''}".strip())
    parts.append(
        f"IDs: task={task_id or '-'} scn={scenario_id or '-'} agent={agent_id or '-'}"
    )

    parts.append(
        "ARTIFACTS: "
        f"net={'OK' if _exists(a['net']) else 'MISS'} "
        f"routes={'OK' if _exists(a['routes']) else 'MISS'} "
        f"cfg={'OK' if _exists(a['cfg']) else 'MISS'} "
        f"| ws={a['workspace'] or '-'}"
    )
    parts.append(
        f"PATHS: net={a['net'] or '-'} | routes={a['routes'] or '-'} | cfg={a['cfg'] or '-'}"
    )

    kpi = _latest_kpi(state)
    if kpi:
        parts.append("KPI: " + _j(kpi, 320))

    err = _latest_error(hist)
    if err:
        parts.append("ERROR: " + err)

    if step_lines:
        parts.append("RECENT:\n" + "\n".join(step_lines))

    text = "\n".join(parts).strip()
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _extract_json_obj(text: str) -> Json:
    """
    Best-effort: parse JSON or extract the first {...} block.
    """
    text = (text or "").strip()
    # direct json
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    else:
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON must be an object")
        return parsed

    # extract first {...}
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"LLM output is not JSON: {text[:200]}")
    return json.loads(m.group(0))


class LLMOutputError(RuntimeError):
    """A response arrived, but cannot be used as a completed JSON decision."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f"{category}: {message}")


def _extract_usage(data: Dict[str, Any]) -> Dict[str, int]:
    prompt_tokens = int(data.get("prompt_eval_count") or 0)
    completion_tokens = int(data.get("eval_count") or 0)
    if prompt_tokens == 0 and completion_tokens == 0 and isinstance(data.get("usage"), dict):
        usage = data["usage"]
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _accumulate_usage(state: Optional[AgentState], usage: Dict[str, int], scope: str) -> None:
    if state is None:
        return
    if not isinstance(state.context, dict):
        state.context = {}

    total = state.context.get("token_usage")
    if not isinstance(total, dict):
        total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    total["calls"] = int(total.get("calls", 0)) + 1
    total["prompt_tokens"] = int(total.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0))
    total["completion_tokens"] = int(total.get("completion_tokens", 0)) + int(usage.get("completion_tokens", 0))
    total["total_tokens"] = int(total.get("total_tokens", 0)) + int(usage.get("total_tokens", 0))
    state.context["token_usage"] = total

    by_scope = state.context.get("token_usage_by_scope")
    if not isinstance(by_scope, dict):
        by_scope = {}
    scoped = by_scope.get(scope)
    if not isinstance(scoped, dict):
        scoped = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    scoped["calls"] = int(scoped.get("calls", 0)) + 1
    scoped["prompt_tokens"] = int(scoped.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0))
    scoped["completion_tokens"] = int(scoped.get("completion_tokens", 0)) + int(usage.get("completion_tokens", 0))
    scoped["total_tokens"] = int(scoped.get("total_tokens", 0)) + int(usage.get("total_tokens", 0))
    by_scope[scope] = scoped
    state.context["token_usage_by_scope"] = by_scope


def _llm_debug_enabled() -> bool:
    return os.getenv("LLM_DEBUG_RAW", "").strip().lower() in {"1", "true", "yes", "on"}


def _record_llm_compatibility_fallback(
    state: Optional[AgentState],
    *,
    scope: str,
    model: str,
    omitted_parameter: str,
) -> None:
    if state is None:
        return
    if not isinstance(state.context, dict):
        state.context = {}
    records = state.context.get("llm_compatibility_fallbacks")
    if not isinstance(records, list):
        records = []
    records.append(
        {
            "scope": scope,
            "model": model,
            "omitted_parameter": omitted_parameter,
            "reason": "api_explicitly_rejected_optional_parameter",
        }
    )
    state.context["llm_compatibility_fallbacks"] = records


def call_llm(
    system: str,
    user: str,
    *,
    model: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    max_tokens: int = 1024,
    timeout: float = 60.0,
    retries: int = 2,
    force_json: bool = True,
    state: Optional[AgentState] = None,
    usage_scope: str = "llm",
) -> Json:
    """
    Call the OpenAI Chat Completions API and return a JSON object.

    Env vars (optional):
      - OPENAI_API_KEY (required)
      - OPENAI_BASE_URL (default https://api.openai.com/v1)
      - OPENAI_MODEL (default gpt-4o-mini)
      - OPENAI_TIMEOUT (default 60)

    Returns:
      Parsed JSON dict from model output.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    config = resolved_llm_config(model=model, openai_base_url=openai_base_url)
    base_url = str(config["base_url"])
    mdl = str(config["model"])
    timeout = float(os.getenv("OPENAI_TIMEOUT", str(timeout)))

    url = f"{base_url}/chat/completions"

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": user.strip()},
    ]

    payload: Dict[str, Any] = {
        "model": mdl,
        "messages": messages,
        "max_completion_tokens": int(max_tokens),
    }

    if force_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    active_payload = dict(payload)
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        record: Dict[str, Any] = {}
        try:
            with httpx.Client(timeout=timeout) as client:
                while True:
                    record = {
                        "scope": usage_scope, "requested_model": mdl,
                        "attempt": attempt + 1,
                        "max_completion_tokens": active_payload["max_completion_tokens"],
                        "response_format": active_payload.get("response_format"),
                    }
                    if state is not None:
                        if not isinstance(state.context, dict):
                            state.context = {}
                        state.context.setdefault("llm_response_metadata", []).append(record)
                    started = time.monotonic()
                    resp = client.post(url, json=active_payload, headers=headers)
                    record.update(http_status=resp.status_code, elapsed_s=time.monotonic()-started)
                    record["request_id"] = getattr(resp, "headers", {}).get("x-request-id")
                    if not resp.is_error:
                        break
                    detail = resp.text.replace(api_key, "[REDACTED]")
                    record.update(outcome="http_error", error=detail)
                    unsupported_param = unsupported_optional_request_parameter(
                        resp,
                        active_payload,
                    )
                    if unsupported_param:
                        active_payload.pop(unsupported_param, None)
                        _record_llm_compatibility_fallback(
                            state,
                            scope=usage_scope,
                            model=mdl,
                            omitted_parameter=unsupported_param,
                        )
                        if _llm_debug_enabled():
                            print(
                                f"[llm_compatibility_fallback] scope={usage_scope} "
                                f"model={mdl} omitted={unsupported_param}"
                            )
                        continue
                    if resp.status_code == 400 and (
                        "max_tokens or model output limit was reached" in detail.lower()
                    ):
                        raise LLMOutputError("http_output_limit", detail)
                    if _llm_debug_enabled():
                        print(f"[llm_http_error] scope={usage_scope} model={mdl} status={resp.status_code} body={detail}")
                    raise RuntimeError(f"OpenAI API error {resp.status_code}: {detail}")
                data = resp.json()
            record.update({
                "response_model": data.get("model"), "response_id": data.get("id"),
                "created": data.get("created"), "system_fingerprint": data.get("system_fingerprint"),
                "usage": data.get("usage") or {},
            })
            usage = _extract_usage(data)
            _accumulate_usage(state, usage, usage_scope)

            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            finish = choice.get("finish_reason")
            refusal = message.get("refusal")
            record.update(finish_reason=finish, choice_count=len(choices),
                          raw_content=content.replace(api_key, "[REDACTED]") if isinstance(content, str) else None,
                          content_chars=len(content) if isinstance(content, str) else None,
                          refusal=str(refusal).replace(api_key, "[REDACTED]") if refusal else None)
            # Check termination BEFORE parsing, including syntactically valid truncated JSON.
            if finish == "length":
                raise LLMOutputError("output_token_limit", "Response ended with finish_reason=length")
            if refusal or finish == "content_filter":
                raise LLMOutputError("refusal_or_filter", "Response was refused or filtered")
            if finish in {"tool_calls", "function_call"}:
                raise LLMOutputError("unexpected_tool_call", "Expected a JSON decision, received a tool call")
            if not choices:
                raise LLMOutputError("missing_choices", "Response contains no choices")
            if not isinstance(content, str):
                raise LLMOutputError("unsupported_content_type", "Expected text content")
            if not content.strip():
                raise LLMOutputError("empty_content", "No visible response; termination was not length")
            if _llm_debug_enabled():
                print(f"[llm_raw] scope={usage_scope} model={mdl} content={record['raw_content']!r}")
            try:
                result = _extract_json_obj(content)
            except (ValueError, TypeError) as exc:
                raise LLMOutputError("invalid_json_object", str(exc).replace(api_key, "[REDACTED]")) from exc
            record["outcome"] = "ok"
            return result

        except Exception as e:
            last_err = e
            record["outcome"] = e.category if isinstance(e, LLMOutputError) else record.get("outcome", "transport_or_protocol_error")
            record["error"] = str(e).replace(api_key, "[REDACTED]")
            if _llm_debug_enabled():
                print(f"[llm_error] scope={usage_scope} model={mdl} attempt={attempt + 1} error={e}")
            # Identical-budget retries cannot reliably remedy a truncated response;
            # do not retry refusals or substitute a deterministic selector.
            if isinstance(e, LLMOutputError) and e.category in {
                "output_token_limit", "http_output_limit", "refusal_or_filter"
            }:
                raise

    if isinstance(last_err, LLMOutputError):
        raise last_err
    raise RuntimeError(f"call_llm failed after {retries+1} attempts: {last_err}")


def _planner_call(role: str, state: AgentState) -> Json:
    # 获取 Planner 给该 Agent 的具体指令
    planner_prompt = _get_plan_prompt_for_role(state, role)
    print(f"[{role}] planner_prompt={planner_prompt}")
    
    # 构造更丰富的上下文，包含当前的物理状态（Artifacts）
    user_context = f"INSTRUCTION: {planner_prompt}\n\nCURRENT STATE:\n{render_state_to_text(state)}"

    system = ROLE_PROMPTS[role]

    return call_llm(
        system=system,
        user=user_context,
        force_json=True,
        state=state,
        usage_scope=f"role:{role}",
    )
