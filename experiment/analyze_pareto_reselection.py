#!/usr/bin/env python3
"""Offline Pareto reselection over already simulated optimizer candidates.

This experiment does not call an LLM or SUMO.  It reconstructs every successful
candidate's KPI vector, applies a common safety envelope, computes the 4-D
Pareto front, and selects profile-specific compromise solutions.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from analyze_top10_seeded_results import (
    CITY_LABELS,
    candidate_family,
    configure_matplotlib,
    save_figure,
)


OBJECTIVES = ("top10", "global_delay", "co2", "speed")
PROFILE_WEIGHTS: dict[str, dict[str, float]] = {
    "pareto_delay": {"top10": 0.55, "global_delay": 0.30, "co2": 0.10, "speed": 0.05},
    "pareto_balanced": {"top10": 0.25, "global_delay": 0.25, "co2": 0.25, "speed": 0.25},
    "pareto_emissions": {"top10": 0.10, "global_delay": 0.15, "co2": 0.65, "speed": 0.10},
    "pareto_mobility": {"top10": 0.10, "global_delay": 0.25, "co2": 0.10, "speed": 0.55},
}
POLICY_LABELS = {
    "current_top10": "Current\nTop-10",
    "legacy_weighted": "Legacy\nweighted $J$",
    "pareto_delay": "Pareto\ndelay",
    "pareto_balanced": "Pareto\nbalanced",
    "pareto_emissions": "Pareto\nemissions",
    "pareto_mobility": "Pareto\nmobility",
}
POLICY_SHORT_LABELS = {
    "current_top10": "Current",
    "legacy_weighted": "Legacy $J$",
    "pareto_delay": "P-Delay",
    "pareto_balanced": "P-Balanced",
    "pareto_emissions": "P-Emissions",
    "pareto_mobility": "P-Mobility",
}
CITY_ORDER = ("albany", "troy", "ann_arbor", "los_angeles", "manhattan")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline Pareto reselection experiment")
    parser.add_argument(
        "--manifest",
        default="experiment/top10-seeded-main/run_manifest.json",
    )
    parser.add_argument("--experiment-root", default="experiment/top10-seeded-main")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="MANIFEST::ROOT",
        help=(
            "Repeatable manifest and experiment-root pair. When supplied, "
            "these inputs replace --manifest and --experiment-root."
        ),
    )
    parser.add_argument("--table-dir", default="paper/generated")
    parser.add_argument("--figure-dir", default="paper/figures")
    return parser.parse_args()


def city_key(case_id: str) -> str:
    return case_id.split("_equal_scale", 1)[0]


def reduction(current: float, baseline: float) -> float:
    return (1.0 - current / baseline) * 100.0 if baseline else 0.0


def improvement(current: float, baseline: float) -> float:
    return (current / baseline - 1.0) * 100.0 if baseline else 0.0


def scope_loss(kpi: Mapping[str, Any], section: str) -> float:
    return float(kpi["focus_scope"][section]["total_time_loss_s"])


def candidate_record(
    case_id: str,
    seed: int,
    proposal_id: str,
    action: str,
    kpi: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    baseline_top10 = scope_loss(baseline, "target")
    baseline_neighbor = scope_loss(baseline, "neighbors")
    current_top10 = scope_loss(kpi, "target")
    current_neighbor = scope_loss(kpi, "neighbors")
    return {
        "case_id": case_id,
        "city_key": city_key(case_id),
        "city": CITY_LABELS.get(city_key(case_id), city_key(case_id)),
        "seed": seed,
        "proposal_id": proposal_id,
        "action": action,
        "top10": reduction(current_top10, baseline_top10),
        "global_delay": reduction(float(kpi["mean_delay"]), float(baseline["mean_delay"])),
        "neighbor": reduction(current_neighbor, baseline_neighbor),
        "co2": reduction(float(kpi["co2"]), float(baseline["co2"])),
        "speed": improvement(float(kpi["mean_speed"]), float(baseline["mean_speed"])),
        "throughput_change": float(kpi["throughput"]) - float(baseline["throughput"]),
        "teleport_change": int(kpi["teleport_count"]) - int(baseline["teleport_count"]),
        "completed_trip_change": int(kpi["completed_trips"]) - int(baseline["completed_trips"]),
        "delay_ratio": float(kpi["mean_delay"]) / float(baseline["mean_delay"]),
        "co2_ratio": float(kpi["co2"]) / float(baseline["co2"]),
        "throughput_ratio": (
            float(kpi["throughput"]) / float(baseline["throughput"])
            if float(baseline["throughput"])
            else 1.0
        ),
        "speed_ratio": float(kpi["mean_speed"]) / float(baseline["mean_speed"]),
        "is_baseline": proposal_id == "baseline_noop",
    }


def load_candidates(
    row: Mapping[str, Any],
    experiment_root: Path,
) -> list[dict[str, Any]]:
    case_id = str(row["case_id"])
    seed = int(row["seed"])
    baseline = row["baseline_kpi"]
    row_root = Path(str(row.get("_experiment_root") or experiment_root))
    run_dir = row_root / case_id / f"run_{int(row['repetition']):03d}"
    candidates = [
        candidate_record(
            case_id,
            seed,
            "baseline_noop",
            "No-op",
            baseline,
            baseline,
        )
    ]
    for ranking in row.get("candidate_ranking") or []:
        proposal_id = str(ranking.get("proposal_id") or "")
        if proposal_id == "baseline_noop" or ranking.get("status") != "ok":
            continue
        root_name = "bundles" if ranking.get("action") == "bundle" else "candidates"
        result_path = run_dir / root_name / proposal_id / "05_optimized.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        candidates.append(
            candidate_record(
                case_id,
                seed,
                proposal_id,
                candidate_family(ranking),
                result["kpi"],
                baseline,
            )
        )
    return candidates


def safety_feasible(candidate: Mapping[str, Any], tolerance: float = 1e-9) -> bool:
    """Common envelope; Top-10 is an objective rather than a hard threshold."""
    return (
        float(candidate["global_delay"]) >= -tolerance
        and float(candidate["neighbor"]) >= -5.0 - tolerance
        and float(candidate["throughput_change"]) >= -tolerance
        and int(candidate["teleport_change"]) <= 0
        and int(candidate["completed_trip_change"]) >= 0
    )


def dominates(left: Mapping[str, Any], right: Mapping[str, Any], tolerance: float = 1e-9) -> bool:
    no_worse = all(float(left[key]) >= float(right[key]) - tolerance for key in OBJECTIVES)
    strictly_better = any(float(left[key]) > float(right[key]) + tolerance for key in OBJECTIVES)
    return no_worse and strictly_better


def pareto_front(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if not any(
            other is not candidate and dominates(other, candidate)
            for other in candidates
        )
    ]


def normalized_values(candidates: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    bounds = {
        objective: (
            min(float(candidate[objective]) for candidate in candidates),
            max(float(candidate[objective]) for candidate in candidates),
        )
        for objective in OBJECTIVES
    }
    result: dict[str, dict[str, float]] = {}
    for candidate in candidates:
        values: dict[str, float] = {}
        for objective, (minimum, maximum) in bounds.items():
            if maximum - minimum <= 1e-12:
                values[objective] = 1.0
            else:
                values[objective] = (float(candidate[objective]) - minimum) / (maximum - minimum)
        result[str(candidate["proposal_id"])] = values
    return result


def select_pareto_compromise(
    feasible: list[dict[str, Any]],
    front: list[dict[str, Any]],
    weights: Mapping[str, float],
) -> dict[str, Any]:
    normalized = normalized_values(feasible)

    def key(candidate: Mapping[str, Any]) -> tuple[float, float, int, str]:
        values = normalized[str(candidate["proposal_id"])]
        deviations = [float(weights[obj]) * (1.0 - values[obj]) for obj in OBJECTIVES]
        chebyshev = max(deviations)
        augmentation = 1e-3 * sum(deviations)
        weighted_gain = sum(float(weights[obj]) * values[obj] for obj in OBJECTIVES)
        return (
            chebyshev + augmentation,
            -weighted_gain,
            0 if candidate["is_baseline"] else 1,
            str(candidate["proposal_id"]),
        )

    return min(front, key=key)


def legacy_objective(candidate: Mapping[str, Any]) -> float:
    return (
        0.50 * float(candidate["delay_ratio"])
        + 0.10 * float(candidate["co2_ratio"])
        - 0.25 * float(candidate["throughput_ratio"])
        - 0.15 * float(candidate["speed_ratio"])
    )


def current_selection(row: Mapping[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    proposal_id = str((row.get("selected_proposal") or {}).get("proposal_id") or "baseline_noop")
    return next(candidate for candidate in candidates if candidate["proposal_id"] == proposal_id)


def select_policies(
    row: Mapping[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    feasible = [candidate for candidate in candidates if safety_feasible(candidate)]
    front = pareto_front(feasible)
    selected = {
        "current_top10": current_selection(row, candidates),
        "legacy_weighted": min(
            feasible,
            key=lambda candidate: (
                legacy_objective(candidate),
                0 if candidate["is_baseline"] else 1,
                str(candidate["proposal_id"]),
            ),
        ),
    }
    for profile, weights in PROFILE_WEIGHTS.items():
        selected[profile] = select_pareto_compromise(feasible, front, weights)
    return selected, front


def mean_sd(values: Iterable[float]) -> tuple[float, float]:
    data = list(values)
    return statistics.mean(data), statistics.stdev(data) if len(data) > 1 else 0.0


def summarize_policy(
    policy: str,
    selected_rows: list[dict[str, Any]],
    current_by_case: Mapping[str, str],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "policy": policy,
        "run_count": len(selected_rows),
        "no_op_count": sum(row["is_baseline"] for row in selected_rows),
        "changed_from_current": sum(
            row["proposal_id"] != current_by_case[row["case_id"]] for row in selected_rows
        ),
        "action_counts": dict(Counter(str(row["action"]) for row in selected_rows)),
    }
    for metric in ("top10", "global_delay", "neighbor", "co2", "speed"):
        mean, sd = mean_sd(float(row[metric]) for row in selected_rows)
        summary[metric] = {"mean": mean, "sample_sd": sd}
    return summary


def write_run_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "city",
        "seed",
        "policy",
        "proposal_id",
        "action",
        "is_baseline",
        "on_pareto_front",
        "pareto_front_size",
        "safety_feasible_count",
        "top10",
        "global_delay",
        "neighbor",
        "co2",
        "speed",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tex_stat(summary: Mapping[str, Any], metric: str) -> str:
    return f"{summary[metric]['mean']:.2f} $\\pm$ {summary[metric]['sample_sd']:.2f}"


def write_latex_table(path: Path, summaries: Mapping[str, Mapping[str, Any]], policies: list[str]) -> None:
    names = {
        "current_top10": "Current Top-10",
        "legacy_weighted": "Legacy weighted $J$",
        "pareto_delay": "Pareto delay",
        "pareto_balanced": "Pareto balanced",
        "pareto_emissions": "Pareto emissions",
        "pareto_mobility": "Pareto mobility",
    }
    lines = [
        "% Generated by experiment/analyze_pareto_reselection.py; do not edit by hand.",
        "\\begin{table*}[!ht]",
        "  \\caption{Offline selection-policy comparison over the same simulation-grounded candidate pools. Values are mean $\\pm$ sample standard deviation in percentage points; positive values denote improvement.}",
        "  \\label{tab:pareto-policy-comparison}",
        "  \\begin{center}",
        "    \\begin{tabular}{lrrrrrr}",
        "      \\hline",
        "      Policy & Top-10 & Global delay & $\\mathrm{CO}_2$ & Speed & No-op & Changed \\\\",
        "      \\hline",
    ]
    for policy in policies:
        summary = summaries[policy]
        lines.append(
            "      "
            + f"{names[policy]} & {tex_stat(summary, 'top10')} & "
            + f"{tex_stat(summary, 'global_delay')} & {tex_stat(summary, 'co2')} & "
            + f"{tex_stat(summary, 'speed')} & "
            + f"{summary['no_op_count']}/{summary['run_count']} & "
            + f"{summary['changed_from_current']}/{summary['run_count']} \\\\"
        )
    lines.extend(
        [
            "      \\hline",
            "    \\end{tabular}",
            "  \\end{center}",
            "\\end{table*}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def plot_policy_metrics(
    figure_dir: Path,
    summaries: Mapping[str, Mapping[str, Any]],
    policies: list[str],
) -> None:
    plt = configure_matplotlib()
    x = list(range(len(policies)))
    labels = [POLICY_SHORT_LABELS[policy] for policy in policies]
    fig, axes = plt.subplots(1, 2, figsize=(7.7, 3.45))
    panels = [
        (
            axes[0],
            [("top10", "Top-10", "#355C7D"), ("global_delay", "Global delay", "#2A9D8F")],
            "Time-loss reduction (%)",
            "(a) Traffic performance",
        ),
        (
            axes[1],
            [("co2", r"CO$_2$", "#E9A03B"), ("speed", "Mean speed", "#8C6BB1")],
            "Improvement (%)",
            "(b) Environmental and mobility outcomes",
        ),
    ]
    for ax, metrics, ylabel, title in panels:
        width = 0.34
        for index, (metric, label, color) in enumerate(metrics):
            offset = (index - 0.5) * width
            means = [summaries[policy][metric]["mean"] for policy in policies]
            errors = [summaries[policy][metric]["sample_sd"] for policy in policies]
            ax.bar(
                [position + offset for position in x],
                means,
                width=width,
                yerr=errors,
                capsize=2.2,
                color=color,
                edgecolor="white",
                linewidth=0.4,
                error_kw={"elinewidth": 0.8, "ecolor": "#444444"},
                label=label,
            )
        ax.axhline(0.0, color="#333333", linewidth=0.8)
        ax.set_xticks(x, labels, rotation=22, ha="right", fontsize=7.5)
        ax.set_ylabel(ylabel)
        ax.set_title(title, loc="left", fontweight="bold")
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
        ax.set_axisbelow(True)
        ax.legend(loc="upper left", fontsize=7.5)
    fig.tight_layout(w_pad=1.4)
    save_figure(fig, figure_dir / "pareto_policy_metric_comparison")
    plt.close(fig)


def plot_policy_actions(
    figure_dir: Path,
    selections: Mapping[str, list[dict[str, Any]]],
    policies: list[str],
) -> None:
    plt = configure_matplotlib()
    actions = ["TLS", "Corridor speed", "Routing (1 edge)", "Routing (3 edges)", "Bundle", "No-op"]
    colors = ["#355C7D", "#E9A03B", "#43AA8B", "#2A9D8F", "#8C6BB1", "#8D99AE"]
    x = list(range(len(policies)))
    bottoms = [0] * len(policies)
    fig, ax = plt.subplots(figsize=(7.0, 3.55))
    for action, color in zip(actions, colors):
        values = [sum(row["action"] == action for row in selections[policy]) for policy in policies]
        ax.bar(x, values, bottom=bottoms, color=color, width=0.68, label=action)
        bottoms = [bottom + value for bottom, value in zip(bottoms, values)]
    ax.set_xticks(x, [POLICY_LABELS[policy] for policy in policies], fontsize=8)
    ax.set_ylabel("Selected runs")
    run_count = max((len(selections[policy]) for policy in policies), default=0)
    ax.set_ylim(0, run_count)
    tick_step = 2 if run_count <= 20 else 5
    ax.set_yticks(range(0, run_count + 1, tick_step))
    ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=7.5)
    save_figure(fig, figure_dir / "pareto_policy_action_comparison")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    experiment_root = Path(args.experiment_root)
    table_dir = Path(args.table_dir)
    figure_dir = Path(args.figure_dir)
    table_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    if args.input:
        manifest = []
        source_manifests = []
        for specification in args.input:
            if "::" not in specification:
                raise ValueError("--input must use MANIFEST::ROOT")
            manifest_value, root_value = specification.split("::", 1)
            input_manifest = Path(manifest_value)
            input_root = Path(root_value)
            source_manifests.append(str(input_manifest))
            for item in json.loads(input_manifest.read_text(encoding="utf-8")):
                row = dict(item)
                row["_experiment_root"] = str(input_root)
                manifest.append(row)
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_manifests = [str(manifest_path)]
    primary_rows = [
        row
        for row in manifest
        if row.get("status") == "ok" and city_key(str(row["case_id"])) in CITY_ORDER
    ]
    primary_rows.sort(key=lambda row: (CITY_ORDER.index(city_key(str(row["case_id"]))), int(row["seed"])))
    policies = ["current_top10", "legacy_weighted", *PROFILE_WEIGHTS.keys()]
    selections: dict[str, list[dict[str, Any]]] = {policy: [] for policy in policies}
    run_rows: list[dict[str, Any]] = []
    front_sizes: list[int] = []
    current_pareto_count = 0

    for row in primary_rows:
        candidates = load_candidates(row, experiment_root)
        selected, front = select_policies(row, candidates)
        feasible = [candidate for candidate in candidates if safety_feasible(candidate)]
        front_ids = {candidate["proposal_id"] for candidate in front}
        front_sizes.append(len(front))
        if selected["current_top10"]["proposal_id"] in front_ids:
            current_pareto_count += 1
        for policy in policies:
            candidate = dict(selected[policy])
            candidate.update(
                {
                    "policy": policy,
                    "on_pareto_front": candidate["proposal_id"] in front_ids,
                    "pareto_front_size": len(front),
                    "safety_feasible_count": len(feasible),
                }
            )
            selections[policy].append(candidate)
            run_rows.append(candidate)

    current_by_case = {
        row["case_id"]: row["proposal_id"] for row in selections["current_top10"]
    }
    summaries = {
        policy: summarize_policy(policy, selections[policy], current_by_case)
        for policy in policies
    }
    report = {
        "source_manifest": source_manifests,
        "design": {
            "runs": len(primary_rows),
            "cities": list(CITY_ORDER),
            "seeds": sorted({int(row["seed"]) for row in primary_rows}),
            "objectives_maximized": list(OBJECTIVES),
            "safety_envelope": {
                "global_delay_reduction_pct": ">= 0",
                "neighbor_reduction_pct": ">= -5",
                "throughput_change": ">= 0",
                "teleport_change": "<= 0",
                "completed_trip_change": ">= 0",
            },
            "profile_weights": PROFILE_WEIGHTS,
        },
        "mean_pareto_front_size": statistics.mean(front_sizes),
        "pareto_front_size_range": [min(front_sizes), max(front_sizes)],
        "current_selection_on_pareto_front": current_pareto_count,
        "summaries": summaries,
    }
    (table_dir / "pareto_reselection_summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_run_csv(table_dir / "pareto_reselection_runs.csv", run_rows)
    write_latex_table(table_dir / "pareto_reselection_table.tex", summaries, policies)
    plot_policy_metrics(figure_dir, summaries, policies)
    plot_policy_actions(figure_dir, selections, policies)

    print(f"Runs: {len(primary_rows)}")
    print(
        f"Pareto front size: mean {statistics.mean(front_sizes):.2f}, "
        f"range {min(front_sizes)}-{max(front_sizes)}"
    )
    print(f"Current selections on Pareto front: {current_pareto_count}/{len(primary_rows)}")
    for policy in policies:
        summary = summaries[policy]
        print(
            f"{policy:18s} top10={summary['top10']['mean']:6.2f}% "
            f"global={summary['global_delay']['mean']:5.2f}% "
            f"co2={summary['co2']['mean']:5.2f}% speed={summary['speed']['mean']:5.2f}% "
            f"noop={summary['no_op_count']:2d} changed={summary['changed_from_current']:2d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
