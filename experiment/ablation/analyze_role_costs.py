import argparse
import glob
import json
import os
import re
from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Dict, List


def _safe_mean(xs: List[float]) -> float:
    return mean(xs) if xs else 0.0


def _scope_label(scope: str) -> str:
    if scope.startswith("role:"):
        return scope.split(":", 1)[1]
    return scope


def _load_multi_files(input_dir: str) -> List[str]:
    files = sorted(glob.glob(os.path.join(input_dir, "summary_multi_*.json")))
    if not files:
        raise SystemExit(f"No summary_multi_*.json found in {input_dir}")
    return files


def _scenario_from_path(path: str) -> str:
    name = os.path.basename(path)
    m = re.match(r"summary_multi_(simple|medium|complex)\.json$", name)
    return m.group(1) if m else name


def _analyze_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    runs = data.get("results") or []
    per_scope_totals: Counter[str] = Counter()
    per_scope_calls: Counter[str] = Counter()

    run_scope_totals: Dict[str, List[float]] = defaultdict(list)
    run_total_tokens: List[float] = []

    for r in runs:
        if not isinstance(r, dict):
            continue
        state = r.get("state") or {}
        total_usage = state.get("token_usage") if isinstance(state, dict) else {}
        by_scope = state.get("token_usage_by_scope") if isinstance(state, dict) else {}

        total_tokens = 0
        if isinstance(total_usage, dict):
            total_tokens = int(total_usage.get("total_tokens") or 0)
            run_total_tokens.append(float(total_tokens))

        if not isinstance(by_scope, dict):
            continue

        for scope, usage in by_scope.items():
            if not isinstance(usage, dict):
                continue
            label = _scope_label(scope)
            t = int(usage.get("total_tokens") or 0)
            c = int(usage.get("calls") or 0)
            per_scope_totals[label] += t
            per_scope_calls[label] += c
            run_scope_totals[label].append(float(t))

    sum_tokens = sum(per_scope_totals.values())
    scope_rows = []
    for scope, t in sorted(per_scope_totals.items(), key=lambda kv: kv[1], reverse=True):
        share = (t / sum_tokens) if sum_tokens else 0.0
        scope_rows.append(
            {
                "scope": scope,
                "total_tokens": t,
                "calls": int(per_scope_calls.get(scope, 0)),
                "mean_tokens_per_run": _safe_mean(run_scope_totals.get(scope, [])),
                "share_of_tokens": share,
            }
        )

    return {
        "file": path,
        "scenario": _scenario_from_path(path),
        "query": data.get("query"),
        "runs": int(data.get("runs") or len(runs)),
        "ok_runs": int(data.get("ok_runs") or 0),
        "error_runs": int(data.get("error_runs") or 0),
        "mean_total_tokens_per_run": _safe_mean(run_total_tokens),
        "roles": scope_rows,
    }


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_tokens: Counter[str] = Counter()
    total_calls: Counter[str] = Counter()
    per_run_tokens: Dict[str, List[float]] = defaultdict(list)

    for rec in records:
        for row in rec.get("roles", []):
            scope = row["scope"]
            total_tokens[scope] += int(row.get("total_tokens") or 0)
            total_calls[scope] += int(row.get("calls") or 0)
            per_run_tokens[scope].append(float(row.get("mean_tokens_per_run") or 0.0))

    grand_total = sum(total_tokens.values())
    rows = []
    for scope, t in sorted(total_tokens.items(), key=lambda kv: kv[1], reverse=True):
        rows.append(
            {
                "scope": scope,
                "total_tokens": t,
                "calls": int(total_calls.get(scope, 0)),
                "mean_tokens_per_run_across_tasks": _safe_mean(per_run_tokens.get(scope, [])),
                "share_of_tokens": (t / grand_total) if grand_total else 0.0,
            }
        )

    # scenario-role matrix: convenient for plotting grouped bars
    scenarios = sorted({str(r.get("scenario")) for r in records})
    role_set = set()
    for rec in records:
        for row in rec.get("roles", []):
            role_set.add(str(row.get("scope")))
    roles_sorted = sorted(role_set)

    matrix: Dict[str, Dict[str, int]] = {}
    for scenario in scenarios:
        rec = next((x for x in records if x.get("scenario") == scenario), None)
        role_map = {str(r.get("scope")): int(r.get("total_tokens") or 0) for r in (rec.get("roles", []) if rec else [])}
        matrix[scenario] = {role: role_map.get(role, 0) for role in roles_sorted}

    return {
        "grand_total_tokens": grand_total,
        "roles": rows,
        "scenario_role_matrix": matrix,
        "scenarios": scenarios,
        "role_order": roles_sorted,
    }


def _write_markdown(records: List[Dict[str, Any]], overall: Dict[str, Any], out_path: str) -> None:
    lines: List[str] = []
    lines.append("# Multi-Agent Role Cost Analysis")
    lines.append("")

    lines.append("## Overall Role Token Share")
    lines.append("")
    lines.append("| Role | Total Tokens | Calls | Share |")
    lines.append("|---|---:|---:|---:|")
    for row in overall.get("roles", []):
        lines.append(
            f"| {row['scope']} | {row['total_tokens']} | {row['calls']} | {row['share_of_tokens']:.3f} |"
        )

    for rec in records:
        lines.append("")
        lines.append(f"## {os.path.basename(rec['file'])}")
        lines.append("")
        lines.append(
            f"- runs={rec['runs']}, ok={rec['ok_runs']}, error={rec['error_runs']}, "
            f"mean_total_tokens_per_run={rec['mean_total_tokens_per_run']:.1f}"
        )
        lines.append("")
        lines.append("| Role | Total Tokens | Calls | Mean Tokens/Run | Share |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in rec.get("roles", []):
            lines.append(
                f"| {row['scope']} | {row['total_tokens']} | {row['calls']} | {row['mean_tokens_per_run']:.1f} | {row['share_of_tokens']:.3f} |"
            )

    lines.append("")
    lines.append("## Scenario-Role Matrix (Total Tokens)")
    lines.append("")
    scenarios = overall.get("scenarios", [])
    roles = overall.get("role_order", [])
    if scenarios and roles:
        lines.append("| Scenario | " + " | ".join(roles) + " |")
        lines.append("|---|" + "|".join(["---:"] * len(roles)) + "|")
        mat = overall.get("scenario_role_matrix", {})
        for sc in scenarios:
            row = mat.get(sc, {})
            vals = [str(int(row.get(role, 0))) for role in roles]
            lines.append("| " + sc + " | " + " | ".join(vals) + " |")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default="./experiment")
    ap.add_argument("--output-json", default="./experiment/role_costs.json")
    ap.add_argument("--output-md", default="./experiment/role_costs.md")
    args = ap.parse_args()

    files = _load_multi_files(args.input_dir)
    records = [_analyze_file(p) for p in files]
    overall = _aggregate(records)

    out = {
        "input_dir": os.path.abspath(args.input_dir),
        "files": files,
        "records": records,
        "overall": overall,
    }

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    _write_markdown(records, overall, args.output_md)

    print(f"[*] Wrote role cost JSON: {args.output_json}")
    print(f"[*] Wrote role cost markdown: {args.output_md}")


if __name__ == "__main__":
    main()
