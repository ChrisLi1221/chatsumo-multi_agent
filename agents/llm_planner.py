from __future__ import annotations
import os, json, re
from typing import Dict, Any, Optional, Tuple
import httpx

from agents.llm_agents import unsupported_optional_request_parameter

# -------- 配置 --------
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TIMEOUT_S = float(os.environ.get("OPENAI_TIMEOUT", "60"))

# 允许的动作（与 agent_mcp.py 对齐）
ALLOWED_ACTIONS = {
    "BUILD_NET": {
        "type": "object",
        "required": [],
        "properties": {
            "type": {
                "enum": ["build_from_realworld", "roundabout"],
                "default": "build_from_realworld",
            },
            "city_name": {"type": "string"},
            "distance_miles": {"type": "integer"},
            "volume": {"type": "integer"},
        },
    },
    "BUILD_ROUTES": {
        "type": "object",
        "required": ["mode"],
        "properties": {
            "mode": {"enum": ["random", "flow"], "default": "random"},
            # Random mode params
            "flows": {"type": "integer", "default": 800},
            "period": {"type": "float", "default": 3600},
            "seed": {"type": "integer", "default": 42},
            # Flow mode params
            "from_edge": {"type": "string"},
            "to_edge": {"type": "string"},
            "vph": {"type": "integer"},
        },
    },
    "RUN": {
        "type": "object",
        "required": [],
        "properties": {
            "gui": {"type": "boolean", "default": True},
            "steps": {"type": "integer", "default": 1800},
            "force": {"type": "boolean", "default": False},
        },
    },
    "ANALYZE": {
        "type": "object", 
        "required": [], 
        "properties": {
            "metric": {
                "enum": ["mean_speed", "co2", "travel_time", "waiting_time", "all"],
                "default": "mean_speed"
            }
        }
    },
    "MODIFY": {
        "type": "object",
        "required": ["op"],
        "properties": {
            "op": {"enum": ["edge_set_speed", "tls_set_program", "veh_reroute", "remove_edge", "make_road_one_way", "tls_optimize_and_apply"]},
            "edge_id": {"type": "string"},
            "keep_edge_id": {"type": "string"},
            "opposite_edge_id": {"type": "string"},
            "volume": {"type": "number"},
            "vmax": {"type": "number"},
            "tls_id": {"type": "string"},
            "program_xml": {"type": "string"},
            "demands": {"type": "array", "items": {"type": "number"}},
            "cycle": {"type": "number"},
            "min_green": {"type": "number"},
            "yellow": {"type": "number"},
            "all_red": {"type": "number"},
            "phase_edge_groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
            "route_path": {"type": "string"},
            "vehicle_ids": {"type": "array", "items": {"type": "string"}},
            "via_edges": {"type": "array", "items": {"type": "string"}},
        },
    },
    "STOP": {"type": "object", "required": [], "properties": {}},
}

SYSTEM_PROMPT = """
You are a planner for a SUMO traffic simulation agent.

Return EXACTLY ONE JSON object with this schema, nothing else:
{
  "action": "BUILD_NET" | "BUILD_ROUTES" | "RUN" | "ANALYZE" |  "STOP" | "MODIFY",
  "params": { ... },
  "reason": "one short sentence",
  "decision": { "summary": "one short sentence (<=80 chars)" }
}

Rules:
- No code fences, no extra text. Output MUST be valid JSON only.

- Choose the next best action given current artifacts:
  * If no net: BUILD_NET
  * Else if no routes or cfg: BUILD_ROUTES
  * If the user explicitly asks for a modification:
    MODIFY → RUN → ANALYZE
  * Else if artifacts (net + routes + cfg) are ready but no modification was requested:
    RUN → ANALYZE
  * Otherwise: STOP unless user asks more
- Keep 'params' minimal, valid, and action-specific.

- Prefer deterministic runs (randomTrips seed=42) unless exploration is requested.

- If the user has NOT requested any modification and the simulation artifacts (net, routes, cfg) are ready, the next action MUST be RUN followed by ANALYZE.

- If the user requests a modification at any time, do NOT run the simulation beforehand. First perform MODIFY, then RUN, then ANALYZE.

- If the latest observation contains "status":"error", do NOT proceed.
  First choose a MODIFY action to fix the error based on error_type and retry.
  If "candidates" are provided, select one candidate and retry once.

Road name handling (STRICT):
- When referring to or extracting road/street names from OSM data, ALWAYS preserve the original street name formatting as stored in OSM "name" tags.
- Road names MUST use spaces, not underscores (e.g., "Colonie Street", NOT "Colonie_Street").
- Do NOT normalize, tokenize, or transform road names into identifier-style strings.
- If a road name appears in an identifier-like format (e.g., with underscores), convert it back to its natural language form using spaces before using it in any MODIFY operation.
- All MODIFY operations that target roads by name must use the exact human-readable street name.

Action-specific PARAMS contract (REQUIRED keys):
- When "action" = "BUILD_NET":
  - If using OSM (default): params MUST include
    { "type": "build_from_realworld", "city_name": <string>, "distance_miles": <number>, "volume": <integer> }
  - If using roundabout: params MUST include
    { "type": "roundabout", "radius": <integer>, "lanes": <integer> }

- When "action" = "BUILD_ROUTES":
  - OPTION A (Random traffic - Default):
    params MUST include { "mode": "random", "seed": <int>, "period": <number>, "flows": <integer> }
  - OPTION B (Specific Flow):
    If user asks for traffic from one specific location/street to another:
    params MUST include { "mode": "flow", "from_edge": <string>, "to_edge": <string>, "vph": <integer> }

- When "action" = "RUN":
  params must include { "steps": <integer>, "gui": <boolean> }

- When "action" = "ANALYZE":
  - Default (general stats): params = { "metric": "mean_speed" }
  - If user asks for specific data, set "metric" to:
    "co2" (emissions), "travel_time", "waiting_time", or "all".

- When "action" = "MODIFY":
  params MUST include { "op": <string> }
  - When "op" = "remove_edge": params MUST include { "edge_id": <string> }. Prefer an exact SUMO edge ID.
  - When "op" = "make_road_one_way": params MUST include { "keep_edge_id": <exact SUMO edge ID> }. Optionally include { "opposite_edge_id": <exact reverse edge ID> } when reverse candidates are ambiguous.
  - When "op" = "edge_set_speed": params MUST include { "edge_id": <string>, "vmax": <number> }
  - When "op" = "tls_set_program": params MUST include { "tls_id": <string>, "program_xml": <string> }
  - When "op" = "tls_optimize_and_apply": params MUST include { "tls_id": <string> } and SHOULD include either { "demands": <number[]> } or { "route_path": <string>, "phase_edge_groups": <string[][]> }. Optional keys: { "cycle": <number>, "min_green": <number>, "yellow": <number>, "all_red": <number> }

- When "action" = "STOP":
  params MUST be {}

Context you can assume:
- The user's preferred city (if provided by user message) SHOULD be used as "city_name".
- If "city_name" is not provided anywhere and action is BUILD_NET (OSM), still output city_name explicitly if you can infer it from the latest user request.

Output policy:
- If an action requires certain keys, include them explicitly in "params".
- Do not invent unknown file paths. Prefer city-based OSM generation via "type":"build_from_realworld".

""".strip()

def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取并解析 JSON（容错修复）"""
    # 直接就是 JSON
    try:
        return json.loads(text)
    except Exception:
        pass
    # 代码块/混入文本时，抓第一个 {...}
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        block = m.group(0)
        try:
            return json.loads(block)
        except Exception:
            # 退一步用宽松解析（若安装了 json5）
            try:
                import json5  # type: ignore

                return json5.loads(block)
            except Exception:
                return None
    return None


def _extract_usage(data: Dict[str, Any]) -> Dict[str, int]:
    # Ollama commonly returns prompt_eval_count / eval_count.
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


def _accumulate_usage(state, usage: Dict[str, int]) -> Dict[str, int]:
    ctx = getattr(state, "context", None)
    if not isinstance(ctx, dict):
        state.context = {}
        ctx = state.context
    current = ctx.get("token_usage")
    if not isinstance(current, dict):
        current = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    current["calls"] = int(current.get("calls", 0)) + 1
    current["prompt_tokens"] = int(current.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens", 0))
    current["completion_tokens"] = int(current.get("completion_tokens", 0)) + int(usage.get("completion_tokens", 0))
    current["total_tokens"] = int(current.get("total_tokens", 0)) + int(usage.get("total_tokens", 0))
    ctx["token_usage"] = current
    return current


async def _openai_chat(messages: list[dict]) -> Tuple[str, Dict[str, int]]:
    """
    调用 OpenAI Chat Completions API；返回字符串内容。
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "max_completion_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    active_payload = dict(payload)
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        while True:
            r = await client.post(url, json=active_payload, headers=headers)
            if not r.is_error:
                break
            unsupported_param = unsupported_optional_request_parameter(
                r,
                active_payload,
            )
            if unsupported_param:
                active_payload.pop(unsupported_param, None)
                continue
            raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text}")
        data = r.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"], _extract_usage(data)
        return json.dumps(data, ensure_ascii=False), _extract_usage(data)


def _summarize_state_for_prompt(state) -> str:
    a = state.artifacts
    lines = [
        f"Goal: {state.goal}",
        f"Workspace: {state.workspace}",
        f"Artifacts: net={a.net}, routes={a.routes}, cfg={a.cfg}",
    ]
    if state.history:
        lines.append("History:")
        for i, rec in enumerate(state.history, start=1):
            lines.append(
                f"- Step {i}: action={rec.action}, "
                f"params={rec.params}, "
                f"obs={rec.observation}, "
                f"kpi={rec.kpi}"
            )

    return "\n".join(lines)


async def decide_next_action(llm, state) -> Dict[str, Any]:
    """
    对外保持与现有 agent 接口一致：
      return {"action": "...", "params": {...}, "reason": "..."}
    'llm' 形参保留（可不使用），由本模块直接调 Ollama。
    """
    try:
        # contracts = await mcp.call("get_all_tool_contracts", {})
        # # 更新 ALLOWED_ACTIONS 以反映实际可用工具（可选）

        state_text = _summarize_state_for_prompt(state)
        user_prompt = f"Current state:\n{state_text}\n\nReturn JSON only."
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        raw, usage = await _openai_chat(messages)
        cumulative_usage = _accumulate_usage(state, usage)
        # print(f"[planner] raw={raw}")
        parsed = _extract_json(raw)
        if not parsed:
            # 调试用：把模型原始输出塞到 reason，方便你观察（只在这一步临时打印到 stderr）
            import sys

            print("[planner][raw]", raw, file=sys.stderr)
            raise ValueError("LLM did not return valid JSON")

        action = str(parsed.get("action", "")).strip().upper()
        params = parsed.get("params") or {}
        reason = parsed.get("reason") or "by deepseek planner"
        decision = parsed.get("decision") or {}
        summary = str(decision.get("summary") or reason)[
            :80
        ]  # 兜底：用 reason，限制长度

        # 约束动作合法
        # 合法性与默认值（与你的现有逻辑一致）
        if action not in ALLOWED_ACTIONS:
            a = state.artifacts
            if not a.net:
                action, params, reason = "BUILD_NET", {}, "auto-correct: require net"
            elif not a.routes or not a.cfg:
                action, params, reason = (
                    "BUILD_ROUTES",
                    {},
                    "auto-correct: require routes & cfg",
                )
            else:
                action, params, reason = (
                    "RUN",
                    {"gui": False},
                    "auto-correct: default run",
                )

        # 轻度参数规范化（只保留 schema 里允许的 key）
        schema = ALLOWED_ACTIONS.get(action, {"properties": {}})
        allowed = set((schema.get("properties") or {}).keys())
        if allowed:
            params = {k: v for k, v in params.items() if k in allowed}

        # 默认值兜底
        if action == "BUILD_NET" and not params:
            params = {
                "type": "build_from_realworld"
            }  # 也可以检查 state.context 里的 osm_path
        if action == "BUILD_ROUTES" and "seed" not in params:
            params["seed"] = 42

        return {
            "raw": raw,
            "action": action,
            "params": params,
            "reason": reason,
            "decision": {"summary": summary},
            "token_usage": usage,
            "token_usage_cumulative": cumulative_usage,
        }

    except Exception as e:
        print(f"[planner] LLM planning failed: {e}")
        raise
