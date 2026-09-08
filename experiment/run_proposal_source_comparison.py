#!/usr/bin/env python3
"""Budget-matched comparison of LLM, grounded-random, and heuristic proposals."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.run_optimization_only import (
    LLM_FEEDBACK_PROPOSAL_SOURCES,
    LocalToolBridge,
    _parse_routing_edge_counts,
    _resolved_proposal_attempt_budget,
    _write_json,
    load_cases,
    run_case,
)


DEFAULT_METHODS = [
    "random_initial_llm",
    "grounded_random",
    "deterministic_heuristic",
]
METHOD_LABELS = {
    "llm": "Specialist LLM",
    "random_initial_llm": "Random-initialized LLM feedback",
    "grounded_random": "Grounded random",
    "deterministic_heuristic": "Deterministic heuristic",
    "grounded_grid": "Restricted grid",
}


def _parse_methods(value: str) -> List[str]:
    allowed = set(METHOD_LABELS)
    methods: List[str] = []
    for item in str(value or "").split(","):
        method = item.strip()
        if not method:
            continue
        if method not in allowed:
            raise argparse.ArgumentTypeError(
                f"unknown method '{method}'; choose from {', '.join(sorted(allowed))}"
            )
        if method not in methods:
            methods.append(method)
    if not methods:
        raise argparse.ArgumentTypeError("provide at least one proposal method")
    return methods


def _mean(values: Iterable[Any]) -> float:
    numbers = [float(value) for value in values if value is not None]
    return statistics.fmean(numbers) if numbers else 0.0


def _sample_std(values: Iterable[Any]) -> float:
    numbers = [float(value) for value in values if value is not None]
    return statistics.stdev(numbers) if len(numbers) >= 2 else 0.0


def _safe_rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def result_row(
    result: Mapping[str, Any],
    *,
    case_id: str,
    repetition: int,
    method: str,
) -> Dict[str, Any]:
    metrics = dict(result.get("proposal_source_metrics") or {})
    counts = dict(metrics.get("counts") or {})
    rates = dict(metrics.get("rates") or {})
    final = dict(metrics.get("final_selection") or {})
    return {
        "case_id": case_id,
        "repetition": repetition,
        "proposal_source": method,
        "method_label": METHOD_LABELS.get(method, method),
        "status": result.get("status"),
        "raw_proposals": int(counts.get("raw_proposals") or 0),
        "validated_proposals": int(counts.get("validated_proposals") or 0),
        "proposal_noops": int(counts.get("proposal_noops") or 0),
        "sumo_evaluations": int(counts.get("sumo_evaluations") or 0),
        "successful_simulations": int(counts.get("successful_simulations") or 0),
        "safety_feasible_candidates": int(counts.get("safety_feasible_candidates") or 0),
        "improving_1pct_candidates": int(counts.get("improving_1pct_candidates") or 0),
        "all_constraints_passed_candidates": int(
            counts.get("all_constraints_passed_candidates") or 0
        ),
        "feasible_improving_candidates": int(
            counts.get("feasible_improving_candidates") or 0
        ),
        "proposal_validation_rate": float(rates.get("proposal_validation_rate") or 0.0),
        "proposal_noop_rate": float(rates.get("proposal_noop_rate") or 0.0),
        "feasible_plan_probability": float(
            rates.get("feasible_plan_probability_per_sumo_evaluation") or 0.0
        ),
        "improving_1pct_rate": float(
            rates.get("improving_1pct_rate_per_sumo_evaluation") or 0.0
        ),
        "all_constraints_pass_rate": float(rates.get("all_constraints_pass_rate") or 0.0),
        "simulation_efficiency": float(rates.get("simulation_efficiency") or 0.0),
        "sumo_budget_utilization": float(rates.get("sumo_budget_utilization") or 0.0),
        "final_noop": bool(final.get("no_op")),
        "final_action": final.get("action"),
        "final_top10_improvement": final.get("top10_relative_improvement"),
        "final_global_delay_improvement": final.get("global_delay_relative_improvement"),
        "elapsed_s": result.get("elapsed_s"),
    }


def aggregate_rows(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    aggregates: List[Dict[str, Any]] = []
    for method in METHOD_LABELS:
        method_rows = [row for row in rows if row.get("proposal_source") == method]
        if not method_rows:
            continue
        raw = sum(int(row.get("raw_proposals") or 0) for row in method_rows)
        valid = sum(int(row.get("validated_proposals") or 0) for row in method_rows)
        noops = sum(int(row.get("proposal_noops") or 0) for row in method_rows)
        evaluations = sum(int(row.get("sumo_evaluations") or 0) for row in method_rows)
        successful = sum(int(row.get("successful_simulations") or 0) for row in method_rows)
        feasible = sum(int(row.get("safety_feasible_candidates") or 0) for row in method_rows)
        improving = sum(int(row.get("improving_1pct_candidates") or 0) for row in method_rows)
        all_constraints = sum(
            int(row.get("all_constraints_passed_candidates") or 0) for row in method_rows
        )
        feasible_improving = sum(
            int(row.get("feasible_improving_candidates") or 0) for row in method_rows
        )
        top10_values = [row.get("final_top10_improvement") for row in method_rows]
        global_values = [row.get("final_global_delay_improvement") for row in method_rows]
        aggregates.append(
            {
                "proposal_source": method,
                "method_label": METHOD_LABELS[method],
                "run_count": len(method_rows),
                "raw_proposals": raw,
                "validated_proposals": valid,
                "sumo_evaluations": evaluations,
                "proposal_validation_rate": _safe_rate(valid, raw),
                "proposal_noop_rate": _safe_rate(noops, raw),
                "simulation_success_rate": _safe_rate(successful, evaluations),
                "feasible_plan_probability": _safe_rate(feasible, evaluations),
                "improving_1pct_rate": _safe_rate(improving, evaluations),
                "all_constraints_pass_rate": _safe_rate(all_constraints, evaluations),
                "simulation_efficiency": _safe_rate(feasible_improving, evaluations),
                "final_noop_rate": _mean(1.0 if row.get("final_noop") else 0.0 for row in method_rows),
                "final_top10_improvement_mean": _mean(top10_values),
                "final_top10_improvement_std": _sample_std(top10_values),
                "final_global_delay_improvement_mean": _mean(global_values),
                "final_global_delay_improvement_std": _sample_std(global_values),
                "sumo_budget_utilization_mean": _mean(
                    row.get("sumo_budget_utilization") for row in method_rows
                ),
                "elapsed_s_mean": _mean(row.get("elapsed_s") for row in method_rows),
            }
        )
    return aggregates


def _write_csv(path: Path, rows: List[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _latex_escape(value: str) -> str:
    return value.replace("&", r"\&").replace("_", r"\_")


def write_latex_table(path: Path, aggregates: List[Mapping[str, Any]]) -> None:
    lines = [
        r"\begin{table*}[!ht]",
        r"    \caption{Budget-matched proposal-source comparison. All rates are computed from the same per-case SUMO evaluation budget.}",
        r"    \label{tab:proposal-source-comparison}",
        r"    \begin{center}",
        r"    \begin{tabular}{lrrrrrr}",
        r"        \hline",
        r"        Method & Valid (\%) & $\geq1\%$ (\%) & Constraints (\%) & Sim. eff. (\%) & Top-10 (\%) & Global (\%) \\",
        r"        \hline",
    ]
    for row in aggregates:
        lines.append(
            "        "
            + _latex_escape(str(row["method_label"]))
            + " & "
            + f"{100 * float(row['proposal_validation_rate']):.1f}"
            + " & "
            + f"{100 * float(row['improving_1pct_rate']):.1f}"
            + " & "
            + f"{100 * float(row['all_constraints_pass_rate']):.1f}"
            + " & "
            + f"{100 * float(row['simulation_efficiency']):.1f}"
            + " & "
            + f"{100 * float(row['final_top10_improvement_mean']):.2f}"
            + " & "
            + f"{100 * float(row['final_global_delay_improvement_mean']):.2f}"
            + r" \\"
        )
    lines.extend(
        [
            r"        \hline",
            r"    \end{tabular}",
            r"    \end{center}",
            r"\end{table*}",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare proposal sources under an identical SUMO evaluation budget"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cfg", help="Existing SUMO .sumocfg")
    source.add_argument("--manifest", help="JSONL file with id, cfg, goal, profile, and seed")
    parser.add_argument("--case-id", default="scenario")
    parser.add_argument("--goal", default="reduce traffic delay")
    parser.add_argument("--profile", choices=["balanced", "delay", "throughput", "emissions"])
    parser.add_argument("--methods", type=_parse_methods, default=DEFAULT_METHODS)
    parser.add_argument("--include-grid", action="store_true")
    parser.add_argument("--sumo-budget", type=int, default=3)
    parser.add_argument(
        "--proposal-attempt-budget",
        type=int,
        help=(
            "Initial raw-proposal budget for every method. By default, feedback "
            "methods use one proposal per active specialist and one-shot baselines "
            "use the full SUMO budget."
        ),
    )
    parser.add_argument("--search-rounds", type=int, default=1)
    parser.add_argument("--feedback-beam-width", type=int, default=2)
    parser.add_argument("--proposal-seed", type=int)
    parser.add_argument("--grid-edge-pool-size", type=int, default=3)
    parser.add_argument("--optimizer", choices=["all", "signal", "network", "routing"], default="all")
    parser.add_argument("--objective-scope", choices=["global", "top10"], default="top10")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--subgraph-seed-n", type=int, default=5)
    parser.add_argument("--subgraph-hops", type=int, default=1)
    parser.add_argument("--subgraph-max-edges", type=int, default=80)
    parser.add_argument("--bundle-top-k", type=int, default=0)
    parser.add_argument("--max-bundle-evals", type=int, default=0)
    parser.add_argument("--routing-edge-counts", type=_parse_routing_edge_counts)
    parser.add_argument(
        "--routing-reroute-fraction",
        type=float,
        choices=[0.25, 0.5, 0.75, 1.0],
    )
    parser.add_argument(
        "--output-dir",
        default="experiment/proposal-source-comparison",
    )
    parser.add_argument(
        "--paper-table",
        default=str(ROOT / "paper" / "generated" / "proposal_source_comparison_table.tex"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.sumo_budget <= 0:
        raise ValueError("SUMO budget must be positive")
    if args.proposal_attempt_budget is not None and args.proposal_attempt_budget <= 0:
        raise ValueError("Proposal attempt budget must be positive")
    if args.search_rounds <= 0 or args.feedback_beam_width <= 0:
        raise ValueError("Search rounds and feedback beam width must be positive")
    methods = list(args.methods)
    if args.include_grid and "grounded_grid" not in methods:
        methods.append("grounded_grid")
    llm_feedback_requested = bool(
        set(methods) & LLM_FEEDBACK_PROPOSAL_SOURCES
    ) and args.search_rounds > 1
    if ("llm" in methods or llm_feedback_requested) and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is required when a selected method uses LLM generation or feedback"
        )

    cases = load_cases(args)
    output_root = Path(args.output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    bridge = LocalToolBridge()
    manifest: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    for case in cases:
        for repetition in range(1, max(1, int(args.runs)) + 1):
            for method in methods:
                args.proposal_source = method
                args._repetition = repetition
                run_dir = output_root / case.case_id / f"run_{repetition:03d}" / method
                print(
                    f"\n=== {case.case_id} / run {repetition} / "
                    f"{METHOD_LABELS.get(method, method)} ==="
                )
                try:
                    result = run_case(bridge, case, run_dir, args)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    result = {
                        "status": "error",
                        "case_id": case.case_id,
                        "repetition": repetition,
                        "proposal_source": method,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                    _write_json(run_dir / "error.json", result)
                    print(f"[!] failed: {exc}", file=sys.stderr)
                record = {
                    "case_id": case.case_id,
                    "repetition": repetition,
                    "proposal_source": method,
                    **result,
                }
                manifest.append(record)
                if result.get("status") == "ok":
                    rows.append(
                        result_row(
                            result,
                            case_id=case.case_id,
                            repetition=repetition,
                            method=method,
                        )
                    )
                _write_json(output_root / "run_manifest.json", manifest)

    aggregates = aggregate_rows(rows)
    summary = {
        "schema_version": "proposal_source_comparison_v1",
        "methods": methods,
        "sumo_budget_per_case_method": args.sumo_budget,
        "proposal_attempt_budget_per_case_method": {
            method: _resolved_proposal_attempt_budget(args, method)
            for method in methods
        },
        "search_rounds": args.search_rounds,
        "feedback_beam_width": args.feedback_beam_width,
        "objective_scope": args.objective_scope,
        "definitions": {
            "simulation_efficiency": (
                "number of candidates that improve the primary delay metric by at least "
                "1% and pass all safety constraints, divided by candidate SUMO evaluations"
            ),
            "budget_matching": "The baseline run is excluded; every method receives the same candidate SUMO cap.",
        },
        "runs": rows,
        "aggregate": aggregates,
    }
    _write_json(output_root / "summary.json", summary)
    _write_csv(output_root / "runs.csv", rows)
    _write_csv(output_root / "aggregate.csv", aggregates)
    write_latex_table(Path(args.paper_table).expanduser().resolve(), aggregates)

    print(f"\nSummary: {output_root / 'summary.json'}")
    print(f"CSV: {output_root / 'aggregate.csv'}")
    print(f"LaTeX table: {Path(args.paper_table).expanduser().resolve()}")
    failures = sum(1 for item in manifest if item.get("status") != "ok")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
