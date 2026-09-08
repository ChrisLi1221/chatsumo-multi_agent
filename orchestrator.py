"""
orchestrator.py

A minimal, round-based Multi-Agent Orchestrator for ChatSUMO-Agent.

Design goals:
- Backward compatible with your single-agent workflow.
- Introduces explicit Task/Scenario/Run/KPI/Proposal bookkeeping via SQLite (through AgentState).
- Keeps "tool execution" pluggable: you can wire to MCP providers or direct Python calls.

How to use (example):
  python orchestrator.py \
    --db data/chatsumo.db \
    --workspace runs/batch_tti/albany_demand

Notes:
- This orchestrator is intentionally conservative: round-based (no async concurrency) to avoid
  multi-agent race conditions while you're bringing up versioning + DB.
"""

from __future__ import annotations

import argparse
import os
import json
import time
import sys
from typing import Any, Dict, List, Optional, Callable, Tuple

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from contextlib import AsyncExitStack

from agents.state_refactored import AgentState, KPI

from agents.llm_agents import (
    call_llm,
    set_current_plan,
    planner_agent,
    builder_agent,
    demand_agent,
    modifier_agent,
    analyst_agent,
    run_planner_agent,
    generate_analyst_report,
)

from agents.contracts import Proposal
from agents.capabilities import (
    normalize_capability_args,
    validate_modifier_capability,
)
from agents.objectives import (
    get_weight_profile,
    infer_weight_profile,
    objective_delta,
)
from agents.debate_optimizer import run_debate_optimizer


Json = Dict[str, Any]
VirtualChangeResult = Optional[Tuple[str, Optional[str], str, Dict[str, Any]]]

SERVER_MAP = {
    "provider_sumo_builder": "builder",
    "provider_traffic_generator": "demand",
    "provider_sumo_modifier": "modifier",
    "provider_metrics": "metrics",
}

# -----------------------------
# Agent interfaces (pluggable)
# -----------------------------
# Each specialist agent can be:
#   - a pure Python callable
#   - a wrapper that calls your LLM planner
#   - a wrapper that calls FastMCP tools
#
# Signature:
#   fn(state: AgentState) -> Proposal | None
#
# Runner agent can instead return a run_config list.
ProposalFn = Callable[[AgentState], Optional[Proposal]]
RunPlanFn = Callable[[AgentState], List[Json]]
RunExecFn = Callable[[AgentState, Json], Tuple[str, Optional[KPI]]]  # returns (status, kpi)

# -----------------
# MCPBridge
# -----------------
class MCPBridge:
    def __init__(self):
        self.configs = {
            "builder": [sys.executable, "providers/provider_sumo_builder.py"],
            "demand": [sys.executable, "providers/provider_traffic_generator.py"],
            "modifier": [sys.executable, "providers/provider_sumo_modifier.py"],
            "runner": [sys.executable, "providers/provider_sumo_runner.py"],
            "metrics": [sys.executable, "providers/provider_metrics.py"],
        }
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self._owner_loop = None

    async def _connect_server(self, name, cmd):
        params = StdioServerParameters(command=cmd[0], args=cmd[1:])
        transport = await self.exit_stack.enter_async_context(stdio_client(params))
        read, write = transport
        session = await self.exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self.sessions[name] = session
        print(f"✅ Connected to {name} server")

    async def start(self):
        self._owner_loop = asyncio.get_running_loop()
        # IMPORTANT: keep enter/exit of async contexts in the same task.
        # Using gather() here can make AnyIO cancel scopes be exited from a
        # different task during close(), which raises RuntimeError.
        for name, cmd in self.configs.items():
            await self._connect_server(name, cmd)

    async def close(self):
        await self.exit_stack.aclose()

    async def acall(self, server_name: str, tool_name: str, arguments: dict):
        """Async tool call for async contexts (e.g., baseline.py)."""
        session = self.sessions.get(server_name)
        if not session:
            raise Exception(f"Server {server_name} not connected")
        result = await session.call_tool(tool_name, arguments)
        return result.content[0].text

    def call(self, server_name: str, tool_name: str, arguments: dict):
        """同步包装器，用于在 Orchestrator 中调用异步工具"""
        session = self.sessions.get(server_name)
        if not session:
            raise Exception(f"Server {server_name} not connected")

        # In async contexts, caller must use acall().
        try:
            _ = asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if has_running_loop:
            raise RuntimeError(
                "MCPBridge.call() cannot be used inside a running event loop; use await acall()."
            )

        loop = self._owner_loop
        if loop is None:
            raise RuntimeError("MCPBridge.start() must be called before MCPBridge.call()")

        if loop.is_closed():
            raise RuntimeError("MCPBridge owner loop is closed")

        result = loop.run_until_complete(session.call_tool(tool_name, arguments))
        return result.content[0].text

# -----------------------------
# Orchestrator
# -----------------------------

class Orchestrator:
    def __init__(
        self,
        state: AgentState,
        db_path: str,
        agent_id: str = "orchestrator",
        task_id: Optional[str] = None,
        scenario_id: Optional[str] = None,
    ) -> None:
        self.state = state
        self.db_path = db_path
        self.agent_id = agent_id

        # Attach DB store and ensure (task_id, scenario_id)
        self.state.attach_store(db_path=db_path, agent_id=agent_id, task_id=task_id, scenario_id=scenario_id)

        # Registry (you'll plug in real implementations)
        self.builder: Optional[ProposalFn] = None
        self.modifier: Optional[ProposalFn] = None
        self.analyst: Optional[ProposalFn] = None
        self.run_planner: Optional[RunPlanFn] = None
        self.run_executor: Optional[RunExecFn] = None
        self.demand: Optional[ProposalFn] = None

        self.round_idx = 0

    # -------- wiring --------

    def wire_builder(self, fn: ProposalFn) -> "Orchestrator":
        self.builder = fn
        return self

    def wire_modifier(self, fn: ProposalFn) -> "Orchestrator":
        self.modifier = fn
        return self

    def wire_demand(self, fn: ProposalFn) -> "Orchestrator":
        self.demand = fn
        return self

    def wire_analyst(self, fn: ProposalFn) -> "Orchestrator":
        self.analyst = fn
        return self

    def wire_runner(self, plan_fn: RunPlanFn, exec_fn: RunExecFn) -> "Orchestrator":
        self.run_planner = plan_fn
        self.run_executor = exec_fn
        return self

    def run_once(
        self,
        user_msg: str,
        *,
        max_repair_attempts: int = 1,
        repair_attempt: int = 0,
    ) -> dict:
        # 0) top-level planner -> lightweight plan
        plan = planner_agent(user_msg, self.state)
        print(f"[*] Planner output: {plan}")

        # --- guard: irrelevant user input ---
        if plan.get("status") == "error":
            msg = (plan.get("error") or {}).get(
                "message",
                "Input is not related to traffic simulation. Please rephrase."
            )
            return {"status": "error", "error": plan.get("error"), "message": msg}

        if repair_attempt == 0:
            self.round_idx += 1
        r = self.round_idx

        self.state.context = self.state.context or {}
        self.state.context["last_user_msg"] = user_msg
        self.state.context["repair_attempt"] = repair_attempt
        self.state.context["max_repair_attempts"] = max_repair_attempts

        steps = (plan or {}).get("plan") or []
        if not steps:
            return {"status": "error", "message": "Planner returned empty plan. Please rephrase."}

        out = {
            "status": "ok",
            "task_id": self.state.task_id,
            "scenario_id": self.state.scenario_id,
            "round": r,
            "repair_attempt": repair_attempt,
            "max_repair_attempts": max_repair_attempts,
            "plan": plan,
            "proposals": [],
            "runs": [],
            "analyst_results": [],
            "debate_results": [],
            "report": None,
            "analysis": [],
        }

        def _public_out(payload: dict) -> dict:
            payload.pop("analysis", None)
            return payload

        if repair_attempt == 0:
            # log user input once
            self._log_step(
                "User input received",
                action="USER_MSG",
                params={"round": r},
                observation={"text": user_msg},
            )
        else:
            self._log_step(
                "Retrying after step error",
                action="REPAIR_RETRY",
                params={"round": r, "repair_attempt": repair_attempt},
                observation={"text": user_msg},
            )

        # helper: pick the only active role in this step (non-empty prompt)
        def _active_role(step: dict) -> str | None:
            ap = (step.get("agent_prompts") or {})
            active = [k for k, v in ap.items() if isinstance(v, str) and v.strip()]
            if not active:
                return None
            # Your planner outputs exactly 1 active agent per step; take the first.
            return active[0]

        def _repair_or_return(step_idx: int, role: Optional[str], err: str) -> dict:
            err_msg = f"Step {step_idx} ({role}) failed: {err}"
            print(f"[!] {err_msg}")
            err_payload = {
                "status": "error",
                "step_idx": step_idx,
                "role": role,
                "message": str(err),
                "repair_attempt": repair_attempt,
            }
            out["status"] = "error"
            out["error"] = err_payload
            out["analysis"].append(err_msg)
            self.state.context["last_error"] = err_payload
            self._log_step(
                "Step execution failed",
                action="STEP_ERROR",
                params={"round": r, "step_idx": step_idx, "role": role},
                observation=err_payload,
            )
            if repair_attempt < max_repair_attempts:
                print(
                    f"[*] Repairing: replanning attempt {repair_attempt + 1}/{max_repair_attempts}"
                )
                return self.run_once(
                    user_msg,
                    max_repair_attempts=max_repair_attempts,
                    repair_attempt=repair_attempt + 1,
                )
            return _public_out(out)

        # execute steps in order
        for step_idx, step in enumerate(steps):
            goal = step.get("goal", "")
            role = _active_role(step)

            # Make current step visible to agents via context
            set_current_plan(
                self.state,
                plan,
                step_idx=step_idx,
                user_input=user_msg if step_idx == 0 else ""
            )

            self._log_step(
                "Planner step",
                action="PLAN_STEP",
                params={"round": r, "step_idx": step_idx, "role": role},
                observation={"goal": goal, "step": step},
            )

            if role is None:
                out["analysis"].append(f"Step {step_idx}: no active agent prompt; skipped.")
                continue

            # Map role -> callable
            if role == "builder":
                fn = self.builder
                step_kind = "PROPOSAL"
            elif role == "demand":
                fn = self.demand
                step_kind = "PROPOSAL"
            elif role == "modifier":
                fn = self.modifier
                step_kind = "PROPOSAL"
            elif role == "analyst":
                fn = self.analyst
                step_kind = "PROPOSAL"
            elif role == "runner_planner":
                fn = None
                step_kind = "RUN"
            else:
                return _repair_or_return(step_idx, role, f"unknown role '{role}'")

            out["analysis"].append(f"Step {step_idx} goal: {goal}")

            try:
                # (A) proposal steps (builder/demand/modifier/analyst)
                if step_kind == "PROPOSAL":
                    if fn is None:
                        out["analysis"].append(f"Step {step_idx}: role '{role}' not wired; skipped.")
                        continue
                    
                    p = fn(self.state)  # ProposalFn(state)->Proposal|None
                    if p is None:
                        out["analysis"].append(f"Step {step_idx}: {role} returned no proposal.")
                        continue

                    # record proposal (AgentState expects Proposal object)
                    self.state.record_proposal(p, status="proposed")
                    out["proposals"].append(p.summary)

                    # fork scenario after each proposal step
                    self.state.scenario_id = self._merge_and_fork_scenario([p], round_idx=r)
                    out["scenario_id"] = self.state.scenario_id
                    debate_results = (self.state.context or {}).get("debate_results")
                    if isinstance(debate_results, list):
                        out["debate_results"] = debate_results
                    if role == "analyst":
                        report = (self.state.context or {}).get("last_report")
                        if isinstance(report, str) and report.strip():
                            out["report"] = report
                        else:
                            analyst_results = (self.state.context or {}).get("analyst_results")
                            if isinstance(analyst_results, list):
                                out["analyst_results"] = analyst_results
                    continue

                # (B) run step (runner_planner -> run_plans -> run_executor)
                if step_kind == "RUN":
                    # Ask run_planner agent to produce a RUN config (via override prompt)
                    run_plans = self.run_planner(self.state) if self.run_planner else []
                    if not run_plans:
                        raise RuntimeError("run_planner returned no run configuration")
                        
                    if self.run_executor is None:
                        raise RuntimeError("run_executor is not wired")


                    for cfg in run_plans:
                        run_id = self.state.record_run_start(
                            config=cfg,
                            seed=cfg.get("seed"),
                            output_dir=cfg.get("output_dir"),
                        )
                        status, kpi = self.run_executor(self.state, cfg)
                        if status == "done" and kpi is not None:
                            self.state.record_kpi(kpi)
                            self._persist_run_result(run_id, kpi)
                        self.state.record_run_finish(status=status, output_dir=cfg.get("output_dir"))

                        out["runs"].append({
                            "cfg": cfg,
                            "status": status,
                            "kpi": None if kpi is None else kpi.to_dict(),
                        })

                        if status != "done":
                            raise RuntimeError(f"run step returned status='{status}'")
                        
                    continue
            except Exception as e:
                return _repair_or_return(step_idx, role, str(e))

        return _public_out(out)

    # -------- helpers --------

    def _persist_run_result(self, run_id: Optional[str], kpi: KPI) -> None:
        if (
            not run_id
            or self.state.store is None
            or self.state.task_id is None
            or self.state.scenario_id is None
        ):
            return

        ctx = self.state.context if isinstance(self.state.context, dict) else {}
        diagnosis = ctx.get("last_network_diagnosis") if isinstance(ctx.get("last_network_diagnosis"), dict) else {}
        report = ctx.get("last_report") if isinstance(ctx.get("last_report"), str) else None
        result_id = self.state.store.write_run_result(
            run_id=run_id,
            task_id=self.state.task_id,
            scenario_id=self.state.scenario_id,
            artifacts=self.state.artifacts.to_dict(),
            kpi=kpi.to_dict(),
            diagnosis=diagnosis,
            report=report,
        )
        self.state.add_record(
            thought="Persisted run result snapshot",
            action="RUN_RESULT",
            params={"run_id": run_id, "result_id": result_id},
            observation={"kpi": kpi.to_dict(), "artifacts": self.state.artifacts.to_dict()},
            run_id=run_id,
        )

    def _compare_recent_runs(self, limit: int = 2) -> dict:
        if self.state.store is None or self.state.task_id is None:
            return {"status": "error", "message": "No SQLite store/task is attached."}

        runs = self.state.store.fetch_recent_run_results(self.state.task_id, limit=max(2, limit))
        if len(runs) < 2:
            return {
                "status": "insufficient_data",
                "message": "Need at least two stored run results to compare.",
                "available_runs": runs,
            }

        current = runs[0]
        previous = runs[1]

        def num(d: dict, key: str) -> float:
            try:
                return float((d or {}).get(key) or 0.0)
            except Exception:
                return 0.0

        cur_kpi = current.get("kpi") or {}
        prev_kpi = previous.get("kpi") or {}
        deltas = {}
        for key in ["mean_speed", "mean_delay", "throughput"]:
            cur = num(cur_kpi, key)
            prev = num(prev_kpi, key)
            delta = cur - prev
            deltas[key] = {
                "previous": prev,
                "current": cur,
                "delta": delta,
                "pct_change": (delta / prev * 100.0) if prev else None,
            }

        improved_delay = deltas["mean_delay"]["delta"] < 0
        improved_speed = deltas["mean_speed"]["delta"] > 0
        ctx = self.state.context if isinstance(self.state.context, dict) else {}
        profile_name = infer_weight_profile(str(ctx.get("last_user_input") or ctx.get("last_user_msg") or ""))
        weights = get_weight_profile(profile_name)
        objective = objective_delta(prev_kpi, cur_kpi, baseline=prev_kpi, weights=weights)

        return {
            "status": "ok",
            "previous_run": previous,
            "current_run": current,
            "deltas": deltas,
            "objective": {
                "profile": profile_name,
                **objective,
            },
            "interpretation": {
                "delay_improved": improved_delay,
                "speed_improved": improved_speed,
                "objective_improved": objective["improved"],
                "overall": "improved" if (improved_delay or improved_speed) else "not_improved",
            },
        }

    def _merge_and_fork_scenario(self, proposals: List[Proposal], round_idx: int) -> str:
        new_ws, new_scn_id = self._prepare_scenario_workspace(proposals, round_idx)

        for proposal in proposals:
            for change in proposal.changes or []:
                self._execute_change(change, new_ws)

        return new_scn_id

    def _prepare_scenario_workspace(self, proposals: List[Proposal], round_idx: int) -> Tuple[str, str]:
        current_ws = os.path.abspath(self.state.workspace)
        round_prefix = f"scn_round_{round_idx}_"

        if round_prefix in os.path.basename(current_ws):
            print(f"[*] Reusing existing workspace for Round {round_idx}")
            return current_ws, str(self.state.scenario_id)

        new_ws = os.path.join(os.path.dirname(current_ws), f"{round_prefix}{int(time.time())}")
        os.makedirs(new_ws, exist_ok=True)
        new_scn_id = self.state.fork_scenario(new_workspace=new_ws, meta={
            "round": round_idx,
            "merged": True,
            "proposal_summaries": [p.summary for p in proposals],
        })
        print(f"[*] Forked new workspace for Round {round_idx}: {new_ws}")
        return new_ws, new_scn_id

    def _execute_change(self, change: Dict[str, Any], new_ws: str) -> None:
        tool_key = change.get("tool")
        action = change.get("action")
        args = dict(change.get("args") or {})

        if not tool_key or not action:
            raise RuntimeError(f"Invalid proposal change, missing tool/action: {change}")

        handled = self._handle_virtual_change(tool_key, action, args, new_ws)
        if handled is None:
            return
        tool_key, server_name, action, args = handled

        if not server_name:
            raise RuntimeError(f"Unknown tool server mapping for tool '{tool_key}'")

        args = self._prepare_tool_args(server_name, action, args, new_ws)
        result = self._call_tool(server_name, action, args)
        self._record_tool_result(server_name, action, args, result)
        self._sync_artifacts_from_result(result)

    def _handle_virtual_change(
        self,
        tool_key: str,
        action: str,
        args: Dict[str, Any],
        new_ws: str,
    ) -> VirtualChangeResult:
        if tool_key == "provider_metrics" and action == "llm_report":
            self._handle_llm_report(args)
            return None

        if tool_key == "provider_metrics" and action == "compare_runs":
            self._handle_compare_runs(args)
            return None

        if tool_key == "provider_sumo_modifier" and action == "debate_optimize":
            selected = self._handle_debate_optimize(args, new_ws)
            if selected.get("action") == "bundle":
                bundle_id = str(selected.get("bundle_id") or "bundle")
                print(f"[*] Debate optimizer selected bundle {bundle_id}")
                for index, action_spec in enumerate(selected.get("actions") or [], 1):
                    step_action = str(action_spec.get("action") or "")
                    step_ws = os.path.join(new_ws, f"bundle_step_{index:02d}_{step_action}")
                    os.makedirs(step_ws, exist_ok=True)
                    self._execute_change(
                        {
                            "tool": action_spec.get("tool") or "provider_sumo_modifier",
                            "action": step_action,
                            "args": dict(action_spec.get("args") or {}),
                        },
                        step_ws,
                    )
                return None
            selected_tool = selected.get("tool", "provider_sumo_modifier")
            selected_action = selected.get("action")
            selected_args = dict(selected.get("args") or {})
            if not selected_action:
                raise RuntimeError(f"Debate optimizer selected invalid action: {selected}")
            print(f"[*] Debate optimizer selected {selected_tool}.{selected_action}")
            return selected_tool, SERVER_MAP.get(selected_tool), selected_action, selected_args

        return tool_key, SERVER_MAP.get(tool_key), action, args

    def _handle_llm_report(self, args: Dict[str, Any]) -> None:
        self.state.context = self.state.context or {}
        analyst_results = self.state.context.get("analyst_results") or []
        report = generate_analyst_report(
            self.state,
            analyst_results=analyst_results,
            style=str(args.get("style") or "brief"),
        )
        self.state.context["last_report"] = report
        if self.state.store is not None and self.state.task_id is not None:
            self.state.store.update_latest_run_result_report(self.state.task_id, report)
        self._append_analyst_result("llm_report", args, {"report": report})
        print("[*] Generated LLM analyst report")

    def _handle_compare_runs(self, args: Dict[str, Any]) -> None:
        compare_result = self._compare_recent_runs(limit=int(args.get("limit", 2) or 2))
        self._append_analyst_result("compare_runs", args, compare_result)
        self.state.context = self.state.context or {}
        self.state.context["last_compare_result"] = compare_result
        print("[*] Compared recent run results from SQLite")

    def _handle_debate_optimize(self, args: Dict[str, Any], new_ws: str) -> Dict[str, Any]:
        self.state.context = self.state.context or {}
        diagnosis = self._ensure_network_diagnosis(args, new_ws)
        diagnosis = self._ensure_bottleneck_subgraph(diagnosis, args)
        user_request = str(
            self.state.context.get("last_user_input")
            or self.state.context.get("last_user_msg")
            or ""
        )
        debate_result = run_debate_optimizer(
            self.state,
            diagnosis=diagnosis,
            user_request=user_request,
            objective_profile=args.get("objective_profile"),
            call_llm_fn=call_llm,
        )
        self.state.context["last_debate_result"] = debate_result
        debate_results = self.state.context.get("debate_results")
        if not isinstance(debate_results, list):
            debate_results = []
        debate_results.append(debate_result)
        self.state.context["debate_results"] = debate_results
        self._print_debate_summary(debate_result)
        selected_action = debate_result.get("selected_action")
        if args.get("enable_bundles") and debate_result.get("bundle_candidates"):
            selected_bundle = dict(debate_result["bundle_candidates"][0])
            selected_action = {
                "tool": "bundle_optimizer",
                "action": "bundle",
                "bundle_id": selected_bundle.get("bundle_id"),
                "actions": selected_bundle.get("actions") or [],
                "compatibility": selected_bundle.get("compatibility"),
            }
        if not selected_action:
            raise RuntimeError(json.dumps(debate_result, ensure_ascii=False))
        return selected_action

    def _print_debate_summary(self, debate_result: Dict[str, Any]) -> None:
        print("[*] Debate optimizer proposals:")
        for proposal in debate_result.get("proposals") or []:
            validation = proposal.get("validation") or {}
            print(
                "    - "
                f"{proposal.get('agent')}: "
                f"{proposal.get('action')} "
                f"score={proposal.get('score')} "
                f"ok={validation.get('ok')} "
                f"claim={proposal.get('claim')}"
            )
        selected = debate_result.get("selected") or {}
        judge = debate_result.get("judge") or {}
        print(
            "[*] Debate judge selected: "
            f"{selected.get('proposal_id') or '-'}; "
            f"reason={judge.get('reason') or '-'}"
        )

    def _ensure_network_diagnosis(self, args: Dict[str, Any], new_ws: str) -> Dict[str, Any]:
        diagnosis = self.state.context.get("last_network_diagnosis") if isinstance(self.state.context, dict) else None
        if isinstance(diagnosis, dict):
            return diagnosis

        cfg_dir = os.path.dirname(self.state.artifacts.cfg) if self.state.artifacts.cfg else new_ws
        tripinfo_path = self.state.artifacts.tripinfo or os.path.join(cfg_dir, "tripinfo.xml")
        edge_data_path = os.path.join(cfg_dir, "edgedata.xml")
        if not (self.state.artifacts.net and os.path.exists(edge_data_path)):
            return {}

        diag_args = {
            "edgedata_xml": edge_data_path,
            "net_file": self.state.artifacts.net,
            "tripinfo_xml": tripinfo_path,
            "top_n": int(args.get("top_n") or 10),
        }
        diagnosis = self._parse_tool_result(
            bridge.call("metrics", "network_diagnosis", diag_args),
            "metrics.network_diagnosis",
        )
        self.state.context = self.state.context or {}
        self.state.context["last_network_diagnosis"] = diagnosis
        return diagnosis if isinstance(diagnosis, dict) else {}

    def _ensure_bottleneck_subgraph(self, diagnosis: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(diagnosis, dict):
            return {}
        if isinstance(diagnosis.get("bottleneck_subgraph"), dict):
            return diagnosis
        if not self.state.artifacts.net:
            return diagnosis

        seed_ids = self._bottleneck_seed_edge_ids(
            diagnosis,
            limit=int(args.get("subgraph_seed_n") or 5),
        )
        if not seed_ids:
            return diagnosis

        subgraph_args = {
            "net_file": self.state.artifacts.net,
            "edge_ids": seed_ids,
            "hops": int(args.get("subgraph_hops") or 1),
            "max_edges": int(args.get("subgraph_max_edges") or 80),
        }
        subgraph = self._parse_tool_result(
            bridge.call("metrics", "bottleneck_subgraph", subgraph_args),
            "metrics.bottleneck_subgraph",
        )
        diagnosis = dict(diagnosis)
        diagnosis["bottleneck_subgraph"] = subgraph
        self.state.context = self.state.context or {}
        self.state.context["last_network_diagnosis"] = diagnosis
        self.state.context["last_bottleneck_subgraph"] = subgraph
        return diagnosis

    def _bottleneck_seed_edge_ids(self, diagnosis: Dict[str, Any], limit: int = 5) -> List[str]:
        seeds: List[str] = []
        for section in ("bottlenecks", "top_congested_edges", "critical_roads"):
            for row in diagnosis.get(section) or []:
                edge_id = row.get("edge_id") if isinstance(row, dict) else None
                if edge_id and edge_id not in seeds:
                    seeds.append(str(edge_id))
                if len(seeds) >= limit:
                    return seeds
        return seeds

    def _prepare_tool_args(
        self,
        server_name: str,
        action: str,
        args: Dict[str, Any],
        new_ws: str,
    ) -> Dict[str, Any]:
        args = dict(args or {})
        if "output_dir" in args and "out_dir" not in args:
            args["out_dir"] = args.pop("output_dir")

        if action == "build_from_realworld":
            args["out_dir"] = new_ws
        elif action == "generate_flow_route":
            args["route_output_path"] = new_ws
        elif action == "build_routes_random":
            args["out_dir"] = new_ws
        elif action in {
            "remove_edge",
            "make_road_one_way",
            "set_edge_speed",
            "set_corridor_speed",
        }:
            args["net_path"] = self.state.artifacts.net
            if self.state.artifacts.routes:
                args["route_path"] = self.state.artifacts.routes
            if self.state.artifacts.cfg:
                args["cfg_path"] = self.state.artifacts.cfg
            args["out_dir"] = new_ws
        elif action == "reroute_flows":
            args["net_path"] = self.state.artifacts.net
            args["route_path"] = self.state.artifacts.routes
            args["cfg_path"] = self.state.artifacts.cfg
            args["out_dir"] = new_ws
        elif action in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
            args["out_dir"] = new_ws
        elif action == "write_sumocfg":
            args["net"] = self.state.artifacts.net
            args["routes"] = self.state.artifacts.routes
            if not args.get("routes") or args["routes"] == "routes.rou.xml":
                args["routes"] = os.path.join(new_ws, "routes.rou.xml")
        elif server_name == "metrics":
            args = self._prepare_metrics_args(action, args, new_ws)

        if server_name == "modifier":
            args = self._prepare_modifier_args(action, args)

        return args

    def _prepare_metrics_args(self, action: str, args: Dict[str, Any], new_ws: str) -> Dict[str, Any]:
        cfg_dir = os.path.dirname(self.state.artifacts.cfg) if self.state.artifacts.cfg else new_ws
        tripinfo_path = self.state.artifacts.tripinfo or os.path.join(cfg_dir, "tripinfo.xml")
        edge_data_path = args.get("edgedata_xml") or os.path.join(cfg_dir, "edgedata.xml")

        if action in {"tripinfo_mean_speed", "tripinfo_travel_time_summary", "emissions_total"}:
            args["path"] = tripinfo_path
        elif action == "edgedata_density_summary":
            args["edgedata_xml"] = edge_data_path
            if self.state.artifacts.net:
                args["net_file"] = self.state.artifacts.net
        elif action == "network_diagnosis":
            args["edgedata_xml"] = edge_data_path
            args["net_file"] = self.state.artifacts.net
            args["tripinfo_xml"] = tripinfo_path
        elif action == "traffic_report":
            args["tripinfo_xml"] = tripinfo_path
            args["net_file"] = self.state.artifacts.net
            args["edgedata_xml"] = edge_data_path
        elif action == "bottleneck_subgraph":
            args["net_file"] = self.state.artifacts.net
        return args

    def _prepare_modifier_args(self, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if action in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
            args["cfg_path"] = self.state.artifacts.cfg
            if action == "tls_optimize_and_apply" and self.state.artifacts.routes:
                args.setdefault("route_path", self.state.artifacts.routes)

        args = normalize_capability_args(action, args)
        capability_check = validate_modifier_capability(action, args, self.state.artifacts)
        if not capability_check.get("ok"):
            raise RuntimeError(json.dumps(capability_check, ensure_ascii=False))
        return args

    def _call_tool(self, server_name: str, action: str, args: Dict[str, Any]) -> Dict[str, Any]:
        print(f"[*] Executing {server_name}.{action}...")
        print(f"    with args: {args}")
        result = bridge.call(server_name, action, args)
        res_json = self._parse_tool_result(result, f"{server_name}.{action}")
        if res_json.get("status") == "error" or res_json.get("success") is False:
            raise RuntimeError(f"{server_name}.{action} error: {res_json.get('message') or res_json}")
        return res_json

    def _parse_tool_result(self, result: Any, label: str) -> Dict[str, Any]:
        try:
            res_json = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError as e:
            raw = result if isinstance(result, str) else repr(result)
            raise RuntimeError(f"{label} returned invalid JSON: {raw[:500]}") from e
        if not isinstance(res_json, dict):
            raise RuntimeError(f"{label} returned non-JSON object: {result}")
        return res_json

    def _record_tool_result(
        self,
        server_name: str,
        action: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        if server_name != "metrics":
            return
        self._append_analyst_result(action, args, result)
        if action == "network_diagnosis":
            self.state.context["last_network_diagnosis"] = result
        if action == "traffic_report" and result.get("report"):
            self.state.context["last_report"] = result["report"]

    def _append_analyst_result(self, action: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        self.state.context = self.state.context or {}
        analyst_results = self.state.context.get("analyst_results")
        if not isinstance(analyst_results, list):
            analyst_results = []
        analyst_results.append({
            "action": action,
            "args": args,
            "result": result,
        })
        self.state.context["analyst_results"] = analyst_results

    def _sync_artifacts_from_result(self, result: Dict[str, Any]) -> None:
        update_data: Dict[str, str] = {}
        result = dict(result or {})
        if result.get("route_path") and not result.get("routes"):
            result["routes"] = result["route_path"]

        self._collect_artifact_updates(result, update_data)

        nested_art = result.get("artifacts")
        if isinstance(nested_art, dict):
            nested = dict(nested_art)
            if nested.get("route_path") and not nested.get("routes"):
                nested["routes"] = nested["route_path"]
            self._collect_artifact_updates(nested, update_data)

        if update_data:
            self.state.set_artifact(**update_data)
            print(f"[*] Artifacts synchronized: {list(update_data.keys())}")

    def _collect_artifact_updates(self, data: Dict[str, Any], update_data: Dict[str, str]) -> None:
        for key in ["net", "routes", "cfg", "tripinfo"]:
            if data.get(key):
                abs_path = os.path.abspath(data[key])
                update_data[key] = abs_path
                setattr(self.state.artifacts, key, abs_path)

    def _log_step(
        self,
        thought: str,
        action: str,
        params: Json,
        observation: Any = None,
        scenario_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        scenario_id = scenario_id or self.state.scenario_id
        if self.state.store and self.state.persist_history:
            # Use AgentState.add_record so history + DB stay consistent
            self.state.add_record(
                thought=thought,
                action=action,
                params=params,
                observation=observation,
                kpi=None,
                scenario_id=scenario_id,
                run_id=run_id,
            )
        else:
            self.state.add_record(thought, action, params, observation)


# -----------------------------
# Default stubs (replace these)
# -----------------------------

def mcp_run_executor(state: AgentState, cfg: dict) -> Tuple[str, Optional[KPI]]:
    """
    调用真正的 MCP Runner Tool
    """
    try:
        run_args = {
            "cfg_path": state.artifacts.cfg,
            "gui": cfg.get("gui", False),
            "step_limit": cfg.get("steps", 3600)
        }
        run_res = bridge.call("runner", "run", run_args)
        run_data = json.loads(run_res) if isinstance(run_res, str) else run_res
        if isinstance(run_data, dict) and (
            run_data.get("status") == "error" or run_data.get("success") is False
        ):
            raise RuntimeError(run_data.get("message") or run_data)
        if isinstance(run_data, dict) and run_data.get("tripinfo"):
            state.artifacts.tripinfo = os.path.abspath(run_data["tripinfo"])
            state.set_artifact(tripinfo=state.artifacts.tripinfo)
        
        tripinfo_path = state.artifacts.tripinfo
        if not tripinfo_path:
            tripinfo_path = os.path.join(os.path.dirname(state.artifacts.cfg), "tripinfo.xml")
        
        metric_args = {
            "path": tripinfo_path, # runner 运行后会更新路径
            "unit": "km/h"
        }
        print(f"[*] Fetching metrics with args: {metric_args}")
        speed_data = json.loads(bridge.call("metrics", "tripinfo_mean_speed", metric_args))
        emissions_data = json.loads(bridge.call(
            "metrics", "emissions_total", {"path": tripinfo_path, "unit": "kg"}
        ))
        
        print(f"[*] Metrics result: {speed_data}")

        edge_data_path = os.path.join(os.path.dirname(state.artifacts.cfg), "edgedata.xml")
        diagnosis = None
        if state.artifacts.net and os.path.exists(edge_data_path):
            diag_args = {
                "edgedata_xml": edge_data_path,
                "net_file": state.artifacts.net,
                "tripinfo_xml": tripinfo_path,
                "top_n": 10,
            }
            print(f"[*] Fetching network diagnosis with args: {diag_args}")
            diagnosis = json.loads(bridge.call("metrics", "network_diagnosis", diag_args))
            state.context = state.context or {}
            state.context["last_network_diagnosis"] = diagnosis
        else:
            state.context = state.context or {}
            state.context["last_network_diagnosis_warning"] = (
                f"edgedata not found at {edge_data_path}; make sure write_sumocfg includes edgeData output"
            )

        delay_data = (diagnosis or {}).get("delay") if isinstance(diagnosis, dict) else {}
        mean_delay = 0.0
        if isinstance(delay_data, dict) and delay_data.get("available"):
            mean_delay = float(
                delay_data.get("penalized_avg_time_loss_s")
                or delay_data.get("avg_time_loss_s")
                or delay_data.get("avg_waiting_time_s")
                or 0.0
            )

        completed = int((delay_data or {}).get("completed_trips") or speed_data.get("count") or 0)
        departed = int((delay_data or {}).get("departed_trips") or completed)
        unfinished = int((delay_data or {}).get("unfinished_trips") or max(0, departed - completed))
        completion_rate = (completed / departed) if departed > 0 else 0.0
        co2_kg = float(((emissions_data.get("totals") or {}).get("CO2_abs")) or 0.0)
        
        kpi = KPI(
            mean_speed=speed_data.get("value", 0.0),
            throughput=completion_rate,
            mean_delay=mean_delay,
            co2=co2_kg,
            completed_trips=completed,
            departed_trips=departed,
            unfinished_trips=unfinished,
        )
        return "done", kpi
    except Exception as e:
        print(f"Run failed: {e}")
        return "failed", None


# -----------------------------
# CLI
# -----------------------------

def _auto_db_path(workspace: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = os.path.abspath(workspace or "runs")
    return os.path.join(root, f"chatsumo_{stamp}.db")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", type=str, required=True, help="Workspace folder for this task")
    ap.add_argument(
        "--db",
        type=str,
        default="",
        help="Optional SQLite db path. If omitted, a new timestamped db is created under --workspace.",
    )
    ap.add_argument("--agent-id", type=str, default="orchestrator", help="Agent id for this orchestrator")
    args = ap.parse_args()
    db_path = os.path.abspath(args.db) if args.db else _auto_db_path(args.workspace)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    global bridge
    bridge = MCPBridge()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(bridge.start())

    state = None
    orch = None

    print("Interactive mode. Type 'exit' to quit.")
# 2. 直接进入交互循环
    while True:
        user_msg = input("\nUser> ").strip()
        if not user_msg:
            continue
        if user_msg.lower() in {"exit", "quit"}:
            break
        
        # 3. 延迟初始化：仅在收到第一次有效输入时创建 state 和 orchestrator
        if state is None:
            # 使用第一次输入作为初始 goal
            state = AgentState.create(goal=user_msg, workspace=args.workspace)
            state.context = {
                "workspace": os.path.abspath(args.workspace),
                "db_path": db_path,
            }
            
            orch = Orchestrator(state=state, db_path=db_path, agent_id=args.agent_id)
            
            # 注册 Agent 逻辑
            orch.wire_builder(builder_agent)
            orch.wire_demand(demand_agent)
            orch.wire_modifier(modifier_agent)
            orch.wire_analyst(analyst_agent)
            orch.wire_runner(run_planner_agent, mcp_run_executor)
            
            print(f"[*] Session initialized with goal: {user_msg}")
            print(f"[*] SQLite database: {db_path}")
        
        # 4. 执行当前轮次
        out = orch.run_once(user_msg)
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
