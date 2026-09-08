#!/usr/bin/env python3
"""Summarize and plot the three-seed proposal-source comparison."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any


METHODS = ("random_initial_llm", "grounded_random", "deterministic_heuristic")
METHOD_LABELS = {
    "random_initial_llm": "LLM feedback",
    "grounded_random": "Grounded random",
    "deterministic_heuristic": "Heuristic",
}
COLORS = {
    "random_initial_llm": "#3F6FA8",
    "grounded_random": "#E59D2F",
    "deterministic_heuristic": "#4C9B88",
}
CITY_ORDER = ("albany", "troy", "ann_arbor", "los_angeles", "manhattan")
CITY_LABELS = {
    "albany": "Albany",
    "troy": "Troy",
    "ann_arbor": "Ann Arbor",
    "los_angeles": "Los Angeles",
    "manhattan": "Manhattan",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", default="experiment")
    parser.add_argument("--seeds", default="42,43,44")
    parser.add_argument(
        "--output-prefix", default="paper/figures/proposal_source_comparison"
    )
    parser.add_argument(
        "--action-output-prefix",
        default="paper/figures/proposal_source_action_distribution",
    )
    parser.add_argument(
        "--table", default="paper/generated/proposal_source_comparison_table.tex"
    )
    parser.add_argument(
        "--city-table", default="paper/generated/five_city_llm_results_table.tex"
    )
    parser.add_argument(
        "--summary", default="paper/generated/proposal_source_comparison_summary.json"
    )
    parser.add_argument(
        "--csv", default="paper/generated/proposal_source_comparison_runs.csv"
    )
    return parser.parse_args()


def city_key(case_id: str) -> str:
    return case_id.split("_equal_scale", 1)[0]


def result_row(record: dict[str, Any], method: str, seed: int) -> dict[str, Any]:
    metrics = dict(record.get("proposal_source_metrics") or {})
    counts = dict(metrics.get("counts") or {})
    rates = dict(metrics.get("rates") or {})
    final = dict(metrics.get("final_selection") or {})
    top10 = float(final.get("top10_relative_improvement") or 0.0)
    return {
        "seed": seed,
        "city": city_key(str(record["case_id"])),
        "method": method,
        "selected_action": str(final.get("action") or "no_op"),
        "selected_proposal_id": str(final.get("proposal_id") or "baseline_noop"),
        "top10_reduction": top10,
        "global_delay_reduction": float(
            final.get("global_delay_relative_improvement") or 0.0
        ),
        "success": bool(not final.get("no_op") and top10 >= 0.01 - 1e-12),
        "raw_proposals": int(counts.get("raw_proposals") or 0),
        "validated_proposals": int(counts.get("validated_proposals") or 0),
        "sumo_evaluations": int(counts.get("sumo_evaluations") or 0),
        "successful_simulations": int(counts.get("successful_simulations") or 0),
        "feasible_improving_candidates": int(
            counts.get("feasible_improving_candidates") or 0
        ),
        "simulation_efficiency": float(rates.get("simulation_efficiency") or 0.0),
    }


def load_rows(experiment_dir: Path, seeds: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seed in seeds:
        llm_path = (
            experiment_dir
            / f"luna-five-city-legal-mutation-seed{seed}"
            / "run_manifest.json"
        )
        for record in json.loads(llm_path.read_text(encoding="utf-8")):
            if record.get("status") == "ok":
                rows.append(result_row(record, "random_initial_llm", seed))

        baseline_path = (
            experiment_dir
            / f"luna-vs-baselines-legal-mutation-seed{seed}"
            / "run_manifest.json"
        )
        for record in json.loads(baseline_path.read_text(encoding="utf-8")):
            if record.get("status") != "ok":
                continue
            method = str(record.get("proposal_source"))
            if method in METHODS[1:]:
                rows.append(result_row(record, method, seed))
    expected = len(seeds) * len(CITY_ORDER) * len(METHODS)
    if len(rows) != expected:
        raise ValueError(f"Expected {expected} successful rows, found {len(rows)}")
    return rows


def safe_rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    city_means: dict[str, dict[str, float]] = {}
    for method in METHODS:
        method_rows = [row for row in rows if row["method"] == method]
        city_means[method] = {
            city: statistics.fmean(
                row["top10_reduction"]
                for row in method_rows
                if row["city"] == city
            )
            for city in CITY_ORDER
        }
        raw = sum(row["raw_proposals"] for row in method_rows)
        valid = sum(row["validated_proposals"] for row in method_rows)
        evaluations = sum(row["sumo_evaluations"] for row in method_rows)
        feasible = sum(row["feasible_improving_candidates"] for row in method_rows)
        top10_values = [row["top10_reduction"] for row in method_rows]
        global_values = [row["global_delay_reduction"] for row in method_rows]
        result.append(
            {
                "method": method,
                "label": METHOD_LABELS[method],
                "runs": len(method_rows),
                "validation_rate": safe_rate(valid, raw),
                "success_rate": statistics.fmean(
                    1.0 if row["success"] else 0.0 for row in method_rows
                ),
                "simulation_efficiency": safe_rate(feasible, evaluations),
                "top10_mean": statistics.fmean(top10_values),
                "top10_std": statistics.stdev(top10_values),
                "global_mean": statistics.fmean(global_values),
                "global_std": statistics.stdev(global_values),
                "sumo_evaluations": evaluations,
            }
        )
    for row in result:
        method = str(row["method"])
        row["best_networks"] = sum(
            city_means[method][city]
            == max(city_means[candidate][city] for candidate in METHODS)
            for city in CITY_ORDER
        )
    return result


def write_table(path: Path, summary: list[dict[str, Any]]) -> None:
    best = {
        "validation_rate": max(row["validation_rate"] for row in summary),
        "success_rate": max(row["success_rate"] for row in summary),
        "best_networks": max(row["best_networks"] for row in summary),
        "simulation_efficiency": max(row["simulation_efficiency"] for row in summary),
        "top10_mean": max(row["top10_mean"] for row in summary),
        "global_mean": max(row["global_mean"] for row in summary),
    }

    def cell(row: dict[str, Any], key: str, value: str) -> str:
        return rf"\textbf{{{value}}}" if row[key] == best[key] else value

    lines = [
        r"% Generated by experiment/plot_proposal_source_comparison.py; do not edit by hand.",
        r"\begin{table*}[!ht]",
        r"  \caption{Budget-matched proposal-source comparison over five networks and three demand seeds. Success denotes a feasible selected plan with at least 1\% Top-10 reduction.}",
        r"  \label{tab:proposal-source-comparison}",
        r"  \begin{center}",
        r"    \small",
        r"    \setlength{\tabcolsep}{4pt}",
        r"    \resizebox{\textwidth}{!}{%",
        r"    \begin{tabular}{lrrrrrr}",
        r"      \hline",
        r"      Method & Valid (\%) & Success (\%) & Best nets. & Sim. eff. (\%) & Top-10 (\%) & Global (\%) \\",
        r"      \hline",
    ]
    for row in summary:
        values = [
            str(row["label"]),
            cell(row, "validation_rate", f"{100 * row['validation_rate']:.1f}"),
            cell(row, "success_rate", f"{100 * row['success_rate']:.1f}"),
            cell(row, "best_networks", f"{row['best_networks']}/5"),
            cell(row, "simulation_efficiency", f"{100 * row['simulation_efficiency']:.1f}"),
            cell(row, "top10_mean", f"{100 * row['top10_mean']:.2f}"),
            cell(row, "global_mean", f"{100 * row['global_mean']:.2f}"),
        ]
        lines.append("      " + " & ".join(values) + r" \\")
    lines.extend(
        [
            r"      \hline",
            r"    \end{tabular}",
            r"    }",
            r"  \end{center}",
            r"\end{table*}",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_city_table(path: Path, rows: list[dict[str, Any]]) -> None:
    llm_rows = [row for row in rows if row["method"] == "random_initial_llm"]
    lines = [
        r"% Generated by experiment/plot_proposal_source_comparison.py; do not edit by hand.",
        r"\begin{table}[!ht]",
        r"  \caption{Five-network performance of the LLM-feedback optimizer over three demand seeds. Reductions are mean $\pm$ sample standard deviation in percentage points and include no-op outcomes.}",
        r"  \label{tab:five-city-llm-results}",
        r"  \begin{center}",
        r"    \small",
        r"    \setlength{\tabcolsep}{4pt}",
        r"    \resizebox{\linewidth}{!}{%",
        r"    \begin{tabular}{lrrrr}",
        r"      \hline",
        r"      Network & Top-10 (\%) & Global delay (\%) & Sim. eff. (\%) & Success \\",
        r"      \hline",
    ]
    for city in CITY_ORDER:
        city_rows = [row for row in llm_rows if row["city"] == city]
        top10 = [100 * row["top10_reduction"] for row in city_rows]
        global_delay = [100 * row["global_delay_reduction"] for row in city_rows]
        evaluations = sum(row["sumo_evaluations"] for row in city_rows)
        feasible = sum(row["feasible_improving_candidates"] for row in city_rows)
        success = sum(bool(row["success"]) for row in city_rows)
        lines.append(
            "      "
            + f"{CITY_LABELS[city]} & "
            + f"{statistics.fmean(top10):.2f} $\\pm$ {statistics.stdev(top10):.2f} & "
            + f"{statistics.fmean(global_delay):.2f} $\\pm$ {statistics.stdev(global_delay):.2f} & "
            + f"{100 * safe_rate(feasible, evaluations):.1f} & {success}/3 \\\\"
        )
    all_top10 = [100 * row["top10_reduction"] for row in llm_rows]
    all_global = [100 * row["global_delay_reduction"] for row in llm_rows]
    all_evaluations = sum(row["sumo_evaluations"] for row in llm_rows)
    all_feasible = sum(row["feasible_improving_candidates"] for row in llm_rows)
    all_success = sum(bool(row["success"]) for row in llm_rows)
    lines.extend(
        [
            r"      \hline",
            "      Overall & "
            + f"{statistics.fmean(all_top10):.2f} $\\pm$ {statistics.stdev(all_top10):.2f} & "
            + f"{statistics.fmean(all_global):.2f} $\\pm$ {statistics.stdev(all_global):.2f} & "
            + f"{100 * safe_rate(all_feasible, all_evaluations):.1f} & "
            + f"{all_success}/{len(llm_rows)} \\\\",
            r"      \hline",
            r"    \end{tabular}",
            r"    }",
            r"  \end{center}",
            r"\end{table}",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot(
    rows: list[dict[str, Any]], summary: list[dict[str, Any]], output_prefix: Path
) -> None:
    os.environ.setdefault(
        "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "sumo_matplotlib")
    )
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig, axes = plt.subplots(
        1, 2, figsize=(10.2, 3.25), gridspec_kw={"width_ratios": [1.8, 1.0]}
    )
    x = np.arange(len(CITY_ORDER))
    width = 0.24
    for index, method in enumerate(METHODS):
        means = []
        errors = []
        for city in CITY_ORDER:
            values = [
                100 * row["top10_reduction"]
                for row in rows
                if row["method"] == method and row["city"] == city
            ]
            means.append(statistics.fmean(values))
            errors.append(statistics.stdev(values))
        axes[0].bar(
            x + (index - 1) * width,
            means,
            width,
            yerr=errors,
            capsize=2.5,
            label=METHOD_LABELS[method],
            color=COLORS[method],
            edgecolor="white",
            linewidth=0.5,
            error_kw={"elinewidth": 0.8, "ecolor": "#555555"},
        )
    axes[0].set_title("(a) Performance across road networks")
    axes[0].set_ylabel("Top-10 time-loss reduction (%)")
    axes[0].set_xticks(x, [CITY_LABELS[city] for city in CITY_ORDER], rotation=12)
    axes[0].axhline(0, color="#333333", linewidth=0.7)
    axes[0].grid(axis="y", color="#DDDDDD", linewidth=0.6)
    axes[0].legend(frameon=False, ncol=3, fontsize=8, loc="upper left")

    sx = np.arange(len(METHODS))
    bar_width = 0.34
    success = [
        100 * next(row["success_rate"] for row in summary if row["method"] == method)
        for method in METHODS
    ]
    efficiency = [
        100
        * next(
            row["simulation_efficiency"]
            for row in summary
            if row["method"] == method
        )
        for method in METHODS
    ]
    success_bars = axes[1].bar(
        sx - bar_width / 2,
        success,
        bar_width,
        color=[COLORS[method] for method in METHODS],
        label="Optimization success",
    )
    efficiency_bars = axes[1].bar(
        sx + bar_width / 2,
        efficiency,
        bar_width,
        color=[COLORS[method] for method in METHODS],
        alpha=0.48,
        hatch="//",
        label="Simulation efficiency",
    )
    axes[1].bar_label(success_bars, fmt="%.1f", padding=2, fontsize=8)
    axes[1].bar_label(efficiency_bars, fmt="%.1f", padding=2, fontsize=8)
    axes[1].set_title("(b) Reliability under a fixed budget")
    axes[1].set_ylabel("Rate (%)")
    axes[1].set_xticks(sx, ["LLM", "Random", "Heuristic"])
    axes[1].set_ylim(0, 100)
    axes[1].grid(axis="y", color="#DDDDDD", linewidth=0.6)
    axes[1].legend(frameon=False, fontsize=8, loc="upper right")

    fig.tight_layout(w_pad=1.5)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_prefix.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(output_prefix.with_suffix(".png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_action_distribution(rows: list[dict[str, Any]], output_prefix: Path) -> None:
    os.environ.setdefault(
        "MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "sumo_matplotlib")
    )
    import matplotlib.pyplot as plt
    import numpy as np

    action_order = (
        "tls_optimize_and_apply",
        "set_corridor_speed",
        "bundle",
        "no_op",
    )
    action_labels = {
        "tls_optimize_and_apply": "TLS",
        "set_corridor_speed": "Corridor speed",
        "bundle": "Bundle",
        "no_op": "No-op",
    }
    action_colors = {
        "tls_optimize_and_apply": "#3F6FA8",
        "set_corridor_speed": "#E59D2F",
        "bundle": "#8064A2",
        "no_op": "#A7ADB4",
    }
    fig, axes = plt.subplots(
        1, 2, figsize=(9.6, 3.05), gridspec_kw={"width_ratios": [1.35, 1.0]}
    )

    y = np.arange(len(METHODS))
    left = np.zeros(len(METHODS))
    for action in action_order:
        values = np.array(
            [
                sum(
                    row["selected_action"] == action
                    for row in rows
                    if row["method"] == method
                )
                for method in METHODS
            ]
        )
        bars = axes[0].barh(
            y,
            values,
            left=left,
            color=action_colors[action],
            edgecolor="white",
            linewidth=0.6,
            label=action_labels[action],
        )
        for bar, value in zip(bars, values):
            if value:
                axes[0].text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_y() + bar.get_height() / 2,
                    str(int(value)),
                    ha="center",
                    va="center",
                    color="white" if action != "no_op" else "#333333",
                    fontsize=8,
                    fontweight="bold",
                )
        left += values
    axes[0].set_title("(a) Final intervention composition")
    axes[0].set_xlabel("Selected cases")
    axes[0].set_yticks(y, [METHOD_LABELS[method] for method in METHODS])
    axes[0].set_xlim(0, 15)
    axes[0].set_xticks(range(0, 16, 3))
    axes[0].invert_yaxis()
    axes[0].grid(axis="x", color="#DDDDDD", linewidth=0.6)
    axes[0].legend(
        frameon=False,
        ncol=4,
        fontsize=8,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.19),
    )

    llm_rows = [row for row in rows if row["method"] == "random_initial_llm"]
    stages = (
        "Initial single",
        "Feedback-refined single",
        "Bundle",
        "No-op",
    )
    stage_colors = ("#9BB7D4", "#3F6FA8", "#8064A2", "#A7ADB4")
    stage_counts = []
    for stage in stages:
        count = 0
        for row in llm_rows:
            action = row["selected_action"]
            proposal_id = row["selected_proposal_id"]
            observed_stage = (
                "No-op"
                if action == "no_op"
                else "Bundle"
                if action == "bundle"
                else "Feedback-refined single"
                if "__refine_" in proposal_id
                else "Initial single"
            )
            count += observed_stage == stage
        stage_counts.append(count)
    stage_x = np.arange(len(stages))
    bars = axes[1].bar(stage_x, stage_counts, color=stage_colors, width=0.68)
    axes[1].bar_label(bars, padding=2, fontsize=8)
    axes[1].set_title("(b) Origin of selected LLM plans")
    axes[1].set_ylabel("Cases")
    axes[1].set_xticks(
        stage_x,
        ["Initial", "Feedback\nrefinement", "Bundle", "No-op"],
    )
    axes[1].set_ylim(0, 7)
    axes[1].grid(axis="y", color="#DDDDDD", linewidth=0.6)

    fig.tight_layout(w_pad=1.8)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_prefix.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(output_prefix.with_suffix(".png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    seeds = [int(value) for value in args.seeds.split(",") if value.strip()]
    rows = load_rows(Path(args.experiment_dir), seeds)
    summary = aggregate(rows)
    write_table(Path(args.table), summary)
    write_city_table(Path(args.city_table), rows)
    write_csv(Path(args.csv), rows)
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {"seeds": seeds, "runs": len(rows), "aggregate": summary}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    plot(rows, summary, Path(args.output_prefix))
    plot_action_distribution(rows, Path(args.action_output_prefix))
    print(summary_path)
    print(Path(args.table))
    print(Path(args.city_table))
    print(Path(args.output_prefix).with_suffix(".pdf"))
    print(Path(args.action_output_prefix).with_suffix(".pdf"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
