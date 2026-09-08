import argparse
import asyncio
import json
import os
import re
from typing import Any, Dict, List

import orchestrator as orch_mod
from agents.state_refactored import AgentState


def _safe_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "prompt"


def _detect_level(prompt_item: Dict[str, str]) -> str:
    # Priority: explicit level field, then infer from id like L1_xxx.
    lv = str(prompt_item.get("level") or "").strip().upper()
    if lv in {"L1", "L2", "L3"}:
        return lv
    pid = str(prompt_item.get("id") or "").strip().upper()
    m = re.match(r"^(L[123])(?:[_-].*)?$", pid)
    if m:
        return m.group(1)
    m2 = re.match(r"^(L[123])", pid)
    if m2:
        return m2.group(1)
    return "UNKNOWN"


def _load_prompts_jsonl(path: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            pid = str(obj.get("id") or f"prompt_{i:03d}")
            prompt = str(obj.get("prompt") or "").strip()
            if not prompt:
                continue
            items.append({"id": pid, "prompt": prompt, "level": str(obj.get("level") or "")})
    return items


def _state_snapshot(state: AgentState) -> Dict[str, Any]:
    token_usage = {}
    token_usage_by_scope = {}
    if isinstance(state.context, dict):
        token_usage = state.context.get("token_usage") or {}
        token_usage_by_scope = state.context.get("token_usage_by_scope") or {}
    diagnosis = {}
    diagnosis_warning = None
    analyst_results = []
    report = None
    if isinstance(state.context, dict):
        diagnosis = state.context.get("last_network_diagnosis") or {}
        diagnosis_warning = state.context.get("last_network_diagnosis_warning")
        analyst_results = state.context.get("analyst_results") or []
        report = state.context.get("last_report")
    return {
        "goal": state.goal,
        "workspace": state.workspace,
        "artifacts": {
            "net": state.artifacts.net,
            "routes": state.artifacts.routes,
            "cfg": state.artifacts.cfg,
            "tripinfo": state.artifacts.tripinfo,
        },
        "history_len": len(state.history),
        "token_usage": token_usage,
        "token_usage_by_scope": token_usage_by_scope,
        "diagnosis": diagnosis,
        "diagnosis_warning": diagnosis_warning,
        "analyst_results": [] if report else analyst_results,
        "report": report,
    }


def _run_once_batch(
    query: str,
    run_dir: str,
    db_path: str,
    agent_id: str,
    max_repair_attempts: int,
) -> Dict[str, Any]:
    state = AgentState.create(goal=query, workspace=run_dir)
    state.context = {"workspace": os.path.abspath(run_dir)}

    orch = orch_mod.Orchestrator(state=state, db_path=db_path, agent_id=agent_id)
    orch.wire_builder(orch_mod.builder_agent)
    orch.wire_demand(orch_mod.demand_agent)
    orch.wire_modifier(orch_mod.modifier_agent)
    orch.wire_analyst(orch_mod.analyst_agent)
    orch.wire_runner(orch_mod.run_planner_agent, orch_mod.mcp_run_executor)

    out = orch.run_once(query, max_repair_attempts=max_repair_attempts)
    if out is None:
        out = {"status": "error", "error": {"message": "orchestrator.run_once returned None"}}

    result = {
        "status": out.get("status", "unknown"),
        "max_repair_attempts": max_repair_attempts,
        "query": query,
        "output": out,
        "state": _state_snapshot(state),
        "history": [r.to_dict() for r in state.history],
    }
    return result


def _parse_repair_groups(s: str) -> List[int]:
    vals: List[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        vals.append(int(part))
    if not vals:
        vals = [0, 1, 2]
    # dedupe + stable sort
    return sorted(set(vals))


def _run_group(
    query: str,
    runs: int,
    group_dir: str,
    agent_id: str,
    max_repair_attempts: int,
) -> Dict[str, Any]:
    os.makedirs(group_dir, exist_ok=True)
    all_results: List[Dict[str, Any]] = []

    for i in range(1, runs + 1):
        run_dir = os.path.join(group_dir, f"run_{i:02d}")
        os.makedirs(run_dir, exist_ok=True)
        db_path = os.path.join(run_dir, "chatsumo.db")

        print(
            f"\n{'=' * 20} Group repair={max_repair_attempts} "
            f"Run {i}/{runs} {'=' * 20}"
        )

        bridge = orch_mod.MCPBridge()
        orch_mod.bridge = bridge

        loop = asyncio.new_event_loop()
        result: Dict[str, Any] = {
            "status": "error",
            "max_repair_attempts": max_repair_attempts,
            "query": query,
            "output": {"status": "error", "error": {"message": "run not started"}},
            "state": {"workspace": run_dir},
            "history": [],
        }
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(bridge.start())
            result = _run_once_batch(
                query,
                run_dir,
                db_path,
                agent_id,
                max_repair_attempts=max_repair_attempts,
            )
        except KeyboardInterrupt:
            result = {
                "status": "interrupted",
                "max_repair_attempts": max_repair_attempts,
                "query": query,
                "output": {"status": "interrupted", "error": {"message": "Interrupted by user (Ctrl+C)"}},
                "state": {"workspace": run_dir},
                "history": [],
            }
            print("[!] Interrupted by user, stopping current group.")
            all_results.append({**result, "run_index": i})
            result_path = os.path.join(run_dir, "result.json")
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump({**result, "run_index": i}, f, ensure_ascii=False, indent=2)
            try:
                loop.run_until_complete(bridge.close())
            except Exception:
                pass
            loop.close()
            break
        except Exception as e:
            result = {
                "status": "error",
                "max_repair_attempts": max_repair_attempts,
                "query": query,
                "output": {"status": "error", "error": {"message": str(e)}},
                "state": {"workspace": run_dir},
                "history": [],
            }
        finally:
            try:
                loop.run_until_complete(bridge.close())
            except Exception as close_err:
                result.setdefault("output", {}).setdefault("warnings", []).append(
                    f"bridge close warning: {close_err}"
                )
            loop.close()

        result["run_index"] = i
        all_results.append(result)

        result_path = os.path.join(run_dir, "result.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[*] Saved run result: {result_path}")

    summary = {
        "query": query,
        "runs": runs,
        "max_repair_attempts": max_repair_attempts,
        "ok_runs": sum(1 for r in all_results if r.get("status") == "ok"),
        "error_runs": sum(1 for r in all_results if r.get("status") == "error"),
        "interrupted_runs": sum(1 for r in all_results if r.get("status") == "interrupted"),
        "results": all_results,
    }
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", type=str, default="", help="Single user input to run")
    ap.add_argument("--prompts", type=str, default="", help="Path to prompts jsonl (fields: id, prompt)")
    ap.add_argument("--runs", type=int, default=10, help="Number of repeated runs")
    ap.add_argument("--ablation-dir", type=str, default="./ablation/orchestrator", help="Output root dir")
    ap.add_argument("--agent-id", type=str, default="orchestrator_batch", help="Agent id for Orchestrator")
    ap.add_argument(
        "--repair-groups",
        type=str,
        default="0,1,2",
        help="Comma-separated max_repair_attempts groups, e.g. '0,1,2'",
    )
    ap.add_argument("--start", type=int, default=1, help="1-based start index for prompts mode")
    ap.add_argument("--limit", type=int, default=0, help="0 means all prompts")
    args = ap.parse_args()

    ablation_root = os.path.abspath(args.ablation_dir)
    os.makedirs(ablation_root, exist_ok=True)
    repair_groups = _parse_repair_groups(args.repair_groups)

    if bool(args.query.strip()) == bool(args.prompts.strip()):
        raise SystemExit("Provide exactly one of --query or --prompts")

    def _run_one_query(query: str, out_dir: str) -> Dict[str, Any]:
        group_summaries: List[Dict[str, Any]] = []
        for rep in repair_groups:
            group_dir = os.path.join(out_dir, f"repair_{rep}")
            group_summary = _run_group(
                query=query,
                runs=args.runs,
                group_dir=group_dir,
                agent_id=args.agent_id,
                max_repair_attempts=rep,
            )
            group_summaries.append(group_summary)

            group_summary_path = os.path.join(group_dir, "summary.json")
            with open(group_summary_path, "w", encoding="utf-8") as f:
                json.dump(group_summary, f, ensure_ascii=False, indent=2)
            print(f"[*] Saved group summary: {group_summary_path}")

        compare = {
            "query": query,
            "runs_per_group": args.runs,
            "repair_groups": repair_groups,
            "groups": group_summaries,
        }
        compare_path = os.path.join(out_dir, "summary_closed_loop_compare.json")
        with open(compare_path, "w", encoding="utf-8") as f:
            json.dump(compare, f, ensure_ascii=False, indent=2)
        print(f"\n[*] Closed-loop comparison finished. Summary saved to: {compare_path}")
        return {"summary_path": compare_path, "query": query}

    if args.query.strip():
        _run_one_query(args.query.strip(), ablation_root)
        return

    prompts = _load_prompts_jsonl(args.prompts)
    if not prompts:
        raise SystemExit(f"No valid prompts in {args.prompts}")

    start_idx = max(1, int(args.start))
    end_idx = len(prompts) if args.limit <= 0 else min(len(prompts), start_idx - 1 + args.limit)

    manifest: Dict[str, Any] = {
        "prompts_file": os.path.abspath(args.prompts),
        "runs_per_group": args.runs,
        "repair_groups": repair_groups,
        "start": start_idx,
        "end": end_idx,
        "total_prompts": len(prompts),
        "jobs": [],
        "level_dirs": {
            "L1": os.path.join(ablation_root, "L1"),
            "L2": os.path.join(ablation_root, "L2"),
            "L3": os.path.join(ablation_root, "L3"),
            "UNKNOWN": os.path.join(ablation_root, "unknown"),
        },
    }

    for idx in range(start_idx - 1, end_idx):
        p = prompts[idx]
        level = _detect_level(p)
        level_dir = os.path.join(ablation_root, level if level in {"L1", "L2", "L3"} else "unknown")
        os.makedirs(level_dir, exist_ok=True)
        prompt_dir = os.path.join(level_dir, f"{idx+1:02d}_{_safe_name(p['id'])}")
        os.makedirs(prompt_dir, exist_ok=True)
        print(
            f"\n{'#' * 16} Prompt {idx+1}/{len(prompts)}: {p['id']} "
            f"(level={level}) {'#' * 16}"
        )
        print(p["prompt"])
        res = _run_one_query(p["prompt"], prompt_dir)
        manifest["jobs"].append(
            {
                "index": idx + 1,
                "id": p["id"],
                "level": level,
                "out_dir": prompt_dir,
                "summary_path": res["summary_path"],
            }
        )
        with open(os.path.join(ablation_root, "run_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n[*] All prompts finished. Manifest: {os.path.join(ablation_root, 'run_manifest.json')}")


if __name__ == "__main__":
    main()
