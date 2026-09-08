import argparse
import glob
import json
import os
import re
from collections import Counter
from statistics import mean, median
from typing import Any, Dict, List, Optional, Tuple


def _safe_mean(xs: List[float]) -> Optional[float]:
    return mean(xs) if xs else None


def _safe_median(xs: List[float]) -> Optional[float]:
    return median(xs) if xs else None


def _parse_name(path: str) -> Tuple[str, str]:
    name = os.path.basename(path)
    m = re.match(r"summary_(single|multi)_(simple|medium|complex)\.json$", name)
    if not m:
        return "unknown", os.path.splitext(name)[0]
    return m.group(1), m.group(2)


def _extract_token_usage(run: Dict[str, Any]) -> Dict[str, int]:
    if isinstance(run.get("token_usage"), dict):
        tu = run["token_usage"]
        return {
            "calls": int(tu.get("calls", 0) or 0),
            "prompt_tokens": int(tu.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(tu.get("completion_tokens", 0) or 0),
            "total_tokens": int(tu.get("total_tokens", 0) or 0),
        }
    state = run.get("state") or {}
    tu = state.get("token_usage") if isinstance(state, dict) else {}
    if isinstance(tu, dict):
        return {
            "calls": int(tu.get("calls", 0) or 0),
            "prompt_tokens": int(tu.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(tu.get("completion_tokens", 0) or 0),
            "total_tokens": int(tu.get("total_tokens", 0) or 0),
        }
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _extract_tti(run: Dict[str, Any]) -> Optional[float]:
    if run.get("tti_seconds") is not None:
        try:
            return float(run["tti_seconds"])
        except Exception:
            pass

    hist = run.get("history")
    if isinstance(hist, list) and hist:
        ts_vals = [x.get("ts") for x in hist if isinstance(x, dict) and isinstance(x.get("ts"), (int, float))]
        if len(ts_vals) >= 2:
            return float(max(ts_vals) - min(ts_vals))
    return None


def _extract_failure_reason(run: Dict[str, Any]) -> str:
    status = str(run.get("status", "")).lower()
    if status == "ok":
        return "ok"

    # single format
    if run.get("error"):
        return f"single_error:{run.get('error')}"
    if run.get("stop_reason") and run.get("stop_reason") != "analyze_completed":
        return f"single_stop:{run.get('stop_reason')}"

    # multi format
    out = run.get("output")
    if isinstance(out, dict):
        err = out.get("error")
        if isinstance(err, dict):
            role = err.get("role")
            msg = err.get("message")
            if role or msg:
                return f"multi_error:{role}:{msg}"
        if err is not None:
            return f"multi_error:{err}"

    return status or "unknown"


def _analyze_summary(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    agent, scenario = _parse_name(path)
    runs = data.get("results") if isinstance(data.get("results"), list) else []

    statuses = [str(r.get("status", "unknown")).lower() for r in runs if isinstance(r, dict)]
    ok_runs = sum(1 for s in statuses if s == "ok")

    tti_vals: List[float] = []
    token_totals: List[int] = []
    token_prompts: List[int] = []
    token_completions: List[int] = []
    token_calls: List[int] = []

    fail_counter: Counter[str] = Counter()

    for r in runs:
        if not isinstance(r, dict):
            continue
        tti = _extract_tti(r)
        if tti is not None:
            tti_vals.append(tti)

        tu = _extract_token_usage(r)
        token_totals.append(tu["total_tokens"])
        token_prompts.append(tu["prompt_tokens"])
        token_completions.append(tu["completion_tokens"])
        token_calls.append(tu["calls"])

        fail_reason = _extract_failure_reason(r)
        if fail_reason != "ok":
            fail_counter[fail_reason] += 1

    n = len(runs)
    record = {
        "file": path,
        "agent": agent,
        "scenario": scenario,
        "query": data.get("query"),
        "runs": n,
        "ok_runs": ok_runs,
        "error_runs": n - ok_runs,
        "success_rate": (ok_runs / n) if n else 0.0,
        "tti_mean_s": _safe_mean(tti_vals),
        "tti_median_s": _safe_median(tti_vals),
        "token_total_mean": _safe_mean([float(x) for x in token_totals]),
        "token_total_median": _safe_median([float(x) for x in token_totals]),
        "token_prompt_mean": _safe_mean([float(x) for x in token_prompts]),
        "token_completion_mean": _safe_mean([float(x) for x in token_completions]),
        "token_calls_mean": _safe_mean([float(x) for x in token_calls]),
        "top_fail_reasons": fail_counter.most_common(5),
    }

    # multi has by-scope usage
    if agent == "multi":
        scope_totals: Counter[str] = Counter()
        scope_calls: Counter[str] = Counter()
        for r in runs:
            state = r.get("state") if isinstance(r, dict) else None
            by_scope = state.get("token_usage_by_scope") if isinstance(state, dict) else None
            if not isinstance(by_scope, dict):
                continue
            for scope, d in by_scope.items():
                if not isinstance(d, dict):
                    continue
                scope_totals[scope] += int(d.get("total_tokens", 0) or 0)
                scope_calls[scope] += int(d.get("calls", 0) or 0)
        record["token_scope_totals"] = dict(scope_totals)
        record["token_scope_calls"] = dict(scope_calls)

    return record


def _build_pairwise(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_scenario: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in records:
        s = r.get("scenario", "unknown")
        a = r.get("agent", "unknown")
        by_scenario.setdefault(s, {})[a] = r

    out: List[Dict[str, Any]] = []
    for scenario, grp in sorted(by_scenario.items()):
        single = grp.get("single")
        multi = grp.get("multi")
        row = {"scenario": scenario}
        if single:
            row["single_success_rate"] = single.get("success_rate")
            row["single_tti_mean_s"] = single.get("tti_mean_s")
            row["single_token_total_mean"] = single.get("token_total_mean")
        if multi:
            row["multi_success_rate"] = multi.get("success_rate")
            row["multi_tti_mean_s"] = multi.get("tti_mean_s")
            row["multi_token_total_mean"] = multi.get("token_total_mean")

        if single and multi:
            row["delta_success_rate_multi_minus_single"] = (multi.get("success_rate") or 0.0) - (single.get("success_rate") or 0.0)
            st = single.get("tti_mean_s")
            mt = multi.get("tti_mean_s")
            row["delta_tti_mean_s_multi_minus_single"] = (mt - st) if (st is not None and mt is not None) else None
            sk = single.get("token_total_mean")
            mk = multi.get("token_total_mean")
            row["delta_token_mean_multi_minus_single"] = (mk - sk) if (sk is not None and mk is not None) else None
        out.append(row)
    return out


def _write_markdown(records: List[Dict[str, Any]], pairwise: List[Dict[str, Any]], path: str) -> None:
    lines: List[str] = []
    lines.append("# Experiment Analysis")
    lines.append("")
    lines.append("## Per-Experiment Metrics")
    lines.append("")
    lines.append("| file | agent | scenario | runs | success_rate | tti_mean_s | token_total_mean |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for r in sorted(records, key=lambda x: (x.get("scenario", ""), x.get("agent", ""))):
        lines.append(
            f"| {os.path.basename(r['file'])} | {r.get('agent')} | {r.get('scenario')} | {r.get('runs')} | "
            f"{(r.get('success_rate') or 0):.3f} | {r.get('tti_mean_s')} | {r.get('token_total_mean')} |"
        )

    lines.append("")
    lines.append("## Single vs Multi (By Scenario)")
    lines.append("")
    lines.append("| scenario | single_success | multi_success | delta_success(m-s) | single_tti | multi_tti | delta_tti | single_tok | multi_tok | delta_tok |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in pairwise:
        lines.append(
            f"| {p.get('scenario')} | {p.get('single_success_rate')} | {p.get('multi_success_rate')} | "
            f"{p.get('delta_success_rate_multi_minus_single')} | {p.get('single_tti_mean_s')} | {p.get('multi_tti_mean_s')} | "
            f"{p.get('delta_tti_mean_s_multi_minus_single')} | {p.get('single_token_total_mean')} | {p.get('multi_token_total_mean')} | "
            f"{p.get('delta_token_mean_multi_minus_single')} |"
        )

    lines.append("")
    lines.append("## Top Failure Reasons")
    lines.append("")
    for r in sorted(records, key=lambda x: (x.get("scenario", ""), x.get("agent", ""))):
        lines.append(f"### {os.path.basename(r['file'])}")
        reasons = r.get("top_fail_reasons") or []
        if not reasons:
            lines.append("- no failures")
        else:
            for reason, cnt in reasons:
                lines.append(f"- {reason}: {cnt}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default="./experiment", help="Directory containing summary_*.json files")
    ap.add_argument("--output-json", default="./experiment/analysis_summary.json", help="Output JSON path")
    ap.add_argument("--output-md", default="./experiment/analysis_report.md", help="Output markdown report path")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.input_dir, "summary_*.json")))
    if not files:
        raise SystemExit(f"No summary_*.json files found in: {args.input_dir}")

    records = [_analyze_summary(p) for p in files]
    pairwise = _build_pairwise(records)

    out = {
        "input_dir": os.path.abspath(args.input_dir),
        "files": files,
        "records": records,
        "pairwise": pairwise,
    }

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    _write_markdown(records, pairwise, args.output_md)

    print(f"[*] Wrote JSON analysis: {args.output_json}")
    print(f"[*] Wrote Markdown report: {args.output_md}")


if __name__ == "__main__":
    main()
