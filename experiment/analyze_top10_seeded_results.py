#!/usr/bin/env python3
"""Summarize the seeded Top-10 optimization experiment for the paper.

The script treats cities with the largest complete seed count as the balanced
primary set.  Cities with fewer valid seeds (currently Manhattan) are retained
as supplementary results but are not mixed into the primary aggregate.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


CITY_LABELS = {
    "albany": "Albany",
    "troy": "Troy",
    "ann_arbor": "Ann Arbor",
    "los_angeles": "Los Angeles",
    "manhattan": "Manhattan",
}

ACTION_LABELS = {
    "no_op": "No-op",
    "tls_optimize_and_apply": "TLS",
    "reroute_flows": "Rerouting",
    "set_corridor_speed": "Corridor speed",
    "bundle": "Bundle",
    "remove_reverse_edge": "Remove edge",
}

CITY_CODES = {
    "albany": "ALB",
    "troy": "TRY",
    "ann_arbor": "ARB",
    "los_angeles": "LAX",
    "manhattan": "MAN",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate tables and figures from a seeded Top-10 run manifest."
    )
    parser.add_argument(
        "--manifest",
        default="experiment/top10-seeded-main/run_manifest.json",
    )
    parser.add_argument("--table-dir", default="paper/generated")
    parser.add_argument("--figure-dir", default="paper/figures")
    return parser.parse_args()


def city_key(case_id: str) -> str:
    marker = "_equal_scale"
    return case_id.split(marker, 1)[0] if marker in case_id else case_id


def percent_reduction(current: float, baseline: float) -> float:
    return (1.0 - current / baseline) * 100.0 if baseline else 0.0


def clean_zero(value: float) -> float:
    return 0.0 if abs(value) < 1e-12 else value


def action_label(row: Mapping[str, Any]) -> str:
    if row.get("baseline_retained"):
        return "No-op"
    action = str((row.get("selected_action") or {}).get("action") or "unknown")
    return ACTION_LABELS.get(action, action.replace("_", " ").title())


def candidate_family(candidate: Mapping[str, Any]) -> str:
    proposal_id = str(candidate.get("proposal_id") or "")
    action = str(candidate.get("action") or "")
    if action == "bundle":
        return "Bundle"
    if action == "tls_optimize_and_apply":
        return "TLS"
    if action == "set_corridor_speed":
        return "Corridor speed"
    if action == "reroute_flows" and "edges_1" in proposal_id:
        return "Routing (1 edge)"
    if action == "reroute_flows" and "edges_3" in proposal_id:
        return "Routing (3 edges)"
    if action == "reroute_flows":
        return "Rerouting"
    return ACTION_LABELS.get(action, action.replace("_", " ").title())


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    scope = row["scope_objective"]
    baseline = row["baseline_kpi"]
    optimized = row["optimized_kpi"]
    return {
        "case_id": row["case_id"],
        "city_key": city_key(row["case_id"]),
        "city": CITY_LABELS.get(city_key(row["case_id"]), city_key(row["case_id"])),
        "seed": int(row["seed"]),
        "action": action_label(row),
        "optimization_applied": bool(row.get("optimization_applied")),
        "top10_reduction_pct": clean_zero(float(scope["top10_relative_improvement"]) * 100.0),
        "global_delay_reduction_pct": clean_zero(-float(scope["global_delay_relative_change"]) * 100.0),
        "neighbor_reduction_pct": clean_zero(-float(scope["neighbor_delay_relative_change"]) * 100.0),
        "co2_reduction_pct": percent_reduction(float(optimized["co2"]), float(baseline["co2"])),
        "speed_improvement_pct": (
            (float(optimized["mean_speed"]) / float(baseline["mean_speed"]) - 1.0) * 100.0
            if float(baseline["mean_speed"])
            else 0.0
        ),
        "baseline_top10_loss_s": float(scope["baseline"]["top10_total_time_loss_s"]),
        "optimized_top10_loss_s": float(scope["current"]["top10_total_time_loss_s"]),
        "baseline_neighbor_loss_s": float(scope["baseline"]["neighbor_total_time_loss_s"]),
        "optimized_neighbor_loss_s": float(scope["current"]["neighbor_total_time_loss_s"]),
        "baseline_mean_delay_s": float(baseline["mean_delay"]),
        "optimized_mean_delay_s": float(optimized["mean_delay"]),
        "baseline_completed_trips": int(baseline["completed_trips"]),
        "optimized_completed_trips": int(optimized["completed_trips"]),
        "baseline_co2": float(baseline["co2"]),
        "optimized_co2": float(optimized["co2"]),
        "optimized_throughput": float(optimized["throughput"]),
        "optimized_teleports": int(optimized["teleport_count"]),
    }


def mean_sd(values: Iterable[float]) -> tuple[float, float]:
    data = list(values)
    return statistics.mean(data), statistics.stdev(data) if len(data) > 1 else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [
        "top10_reduction_pct",
        "global_delay_reduction_pct",
        "neighbor_reduction_pct",
        "co2_reduction_pct",
        "speed_improvement_pct",
    ]
    result: dict[str, Any] = {"run_count": len(rows)}
    for metric in metrics:
        avg, sd = mean_sd(float(row[metric]) for row in rows)
        result[metric] = {"mean": avg, "sample_sd": sd}
    result["applied_count"] = sum(row["optimization_applied"] for row in rows)
    result["pooled"] = {
        "top10_reduction_pct": percent_reduction(
            sum(row["optimized_top10_loss_s"] for row in rows),
            sum(row["baseline_top10_loss_s"] for row in rows),
        ),
        "neighbor_reduction_pct": percent_reduction(
            sum(row["optimized_neighbor_loss_s"] for row in rows),
            sum(row["baseline_neighbor_loss_s"] for row in rows),
        ),
        "global_delay_reduction_pct": percent_reduction(
            sum(row["optimized_mean_delay_s"] * row["optimized_completed_trips"] for row in rows),
            sum(row["baseline_mean_delay_s"] * row["baseline_completed_trips"] for row in rows),
        ),
        "co2_reduction_pct": percent_reduction(
            sum(row["optimized_co2"] for row in rows),
            sum(row["baseline_co2"] for row in rows),
        ),
    }
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "city",
        "seed",
        "action",
        "optimization_applied",
        "top10_reduction_pct",
        "global_delay_reduction_pct",
        "neighbor_reduction_pct",
        "co2_reduction_pct",
        "speed_improvement_pct",
        "optimized_throughput",
        "optimized_teleports",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tex_stat(summary: Mapping[str, Any], metric: str) -> str:
    value = summary[metric]
    return f"{value['mean']:.2f} $\\pm$ {value['sample_sd']:.2f}"


def write_latex_table(
    path: Path,
    city_summaries: Mapping[str, Mapping[str, Any]],
    overall: Mapping[str, Any],
    city_order: list[str],
) -> None:
    lines = [
        "% Generated by experiment/analyze_top10_seeded_results.py; do not edit by hand.",
        "\\begin{table*}[!ht]",
        "  \\caption{Optimization performance across three demand seeds. Values are mean $\\pm$ sample standard deviation in percentage points; positive values denote reductions. The primary aggregate includes only the four cities with all three valid seeds.}",
        "  \\label{tab:top10-seeded-results}",
        "  \\begin{center}",
        "    \\begin{tabular}{lrrrr}",
        "      \\hline",
        "      City & Top-10 time loss & Global delay & Neighbor time loss & Applied \\\\",
        "      \\hline",
    ]
    for key in city_order:
        summary = city_summaries[key]
        lines.append(
            "      "
            + f"{CITY_LABELS.get(key, key)} & "
            + f"{tex_stat(summary, 'top10_reduction_pct')} & "
            + f"{tex_stat(summary, 'global_delay_reduction_pct')} & "
            + f"{tex_stat(summary, 'neighbor_reduction_pct')} & "
            + f"{summary['applied_count']}/{summary['run_count']} \\\\" 
        )
    lines.extend(
        [
            "      \\hline",
            "      "
            + f"Overall & {tex_stat(overall, 'top10_reduction_pct')} & "
            + f"{tex_stat(overall, 'global_delay_reduction_pct')} & "
            + f"{tex_stat(overall, 'neighbor_reduction_pct')} & "
            + f"{overall['applied_count']}/{overall['run_count']} \\\\",
            "      \\hline",
            "    \\end{tabular}",
            "  \\end{center}",
            "\\end{table*}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def configure_matplotlib() -> Any:
    cache = Path(os.environ.setdefault("MPLCONFIGDIR", "/tmp/sumo-matplotlib-cache"))
    cache.mkdir(parents=True, exist_ok=True)
    xdg_cache = Path(os.environ.setdefault("XDG_CACHE_HOME", "/tmp/sumo-xdg-cache"))
    (xdg_cache / "fontconfig").mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.labelsize": 9,
            "axes.titlesize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "legend.frameon": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )
    return plt


def save_figure(fig: Any, base_path: Path) -> None:
    fig.savefig(base_path.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(base_path.with_suffix(".png"), dpi=300, bbox_inches="tight")


def plot_seeded_reductions(
    figure_dir: Path,
    rows: list[dict[str, Any]],
    city_order: list[str],
    seed_order: list[int],
) -> None:
    plt = configure_matplotlib()
    palette = ["#355C7D", "#2A9D8F", "#E9A03B", "#8C6BB1"]
    width = 0.22
    x = list(range(len(city_order)))
    fig, ax = plt.subplots(figsize=(7.1, 3.55))
    lookup = {(row["city_key"], row["seed"]): row for row in rows}
    offsets = [(index - (len(seed_order) - 1) / 2) * width for index in range(len(seed_order))]
    maximum_value = max(row["top10_reduction_pct"] for row in rows)
    for index, seed in enumerate(seed_order):
        values = [lookup[(city, seed)]["top10_reduction_pct"] for city in city_order]
        bars = ax.bar(
            [position + offsets[index] for position in x],
            values,
            width=width,
            color=palette[index],
            edgecolor="white",
            linewidth=0.5,
            label=f"Seed {seed}",
        )
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                max(value, 0.0) + 0.18,
                f"{value:.1f}",
                ha="center",
                va="bottom",
                fontsize=7,
                color=palette[index],
            )
    ax.axhline(0.0, color="#333333", linewidth=0.8)
    ax.set_xticks(x, [CITY_LABELS.get(city, city) for city in city_order])
    ax.set_ylabel("Top-10 time-loss reduction (%)")
    ax.set_xlabel("Road network")
    ax.set_ylim(0.0, maximum_value + 1.0)
    ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)
    ax.legend(
        ncol=len(seed_order),
        loc="lower center",
        bbox_to_anchor=(0.5, 1.01),
        borderaxespad=0.0,
    )
    fig.subplots_adjust(top=0.83)
    save_figure(fig, figure_dir / "top10_reduction_by_city_seed")
    plt.close(fig)


def plot_action_counts(figure_dir: Path, rows: list[dict[str, Any]]) -> None:
    plt = configure_matplotlib()
    preferred = ["TLS", "Rerouting", "Corridor speed", "Bundle", "No-op"]
    counts = Counter(row["action"] for row in rows)
    labels = [label for label in preferred if counts[label]]
    labels.extend(sorted(set(counts) - set(labels)))
    values = [counts[label] for label in labels]
    colors = ["#355C7D", "#2A9D8F", "#E9A03B", "#8C6BB1", "#8D99AE"]
    fig, ax = plt.subplots(figsize=(5.4, 3.2))
    bars = ax.bar(labels, values, color=colors[: len(labels)], width=0.68)
    ax.bar_label(bars, padding=2, fontsize=9)
    ax.set_ylabel("Number of selected runs")
    ax.set_xlabel("Final intervention")
    ax.set_ylim(0, max(values) + 1.2)
    ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
    ax.set_axisbelow(True)
    save_figure(fig, figure_dir / "intervention_selection_counts")
    plt.close(fig)


def plot_city_metric_comparison(
    figure_dir: Path,
    city_summaries: Mapping[str, Mapping[str, Any]],
    city_order: list[str],
) -> None:
    plt = configure_matplotlib()
    x = list(range(len(city_order)))
    labels = [CITY_LABELS.get(city, city) for city in city_order]
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.35))

    panels = [
        (
            axes[0],
            [
                ("top10_reduction_pct", "Top-10", "#355C7D"),
                ("neighbor_reduction_pct", "Neighbors", "#2A9D8F"),
            ],
            "Localized time-loss reduction (%)",
        ),
        (
            axes[1],
            [
                ("global_delay_reduction_pct", "Global delay", "#355C7D"),
                ("co2_reduction_pct", r"CO$_2$", "#E9A03B"),
                ("speed_improvement_pct", "Mean speed", "#8C6BB1"),
            ],
            "Network-wide improvement (%)",
        ),
    ]
    for ax, series, ylabel in panels:
        width = 0.72 / len(series)
        offsets = [(index - (len(series) - 1) / 2) * width for index in range(len(series))]
        for index, (metric, label, color) in enumerate(series):
            means = [city_summaries[city][metric]["mean"] for city in city_order]
            errors = [city_summaries[city][metric]["sample_sd"] for city in city_order]
            ax.bar(
                [position + offsets[index] for position in x],
                means,
                width=width,
                yerr=errors,
                capsize=2.5,
                color=color,
                edgecolor="white",
                linewidth=0.4,
                error_kw={"elinewidth": 0.8, "ecolor": "#444444"},
                label=label,
            )
        ax.axhline(0.0, color="#333333", linewidth=0.8)
        ax.set_xticks(x, labels, rotation=18, ha="right")
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", color="#D9D9D9", linewidth=0.6, alpha=0.8)
        ax.set_axisbelow(True)
        ax.legend(loc="upper left", fontsize=8)
    axes[0].set_title("(a) Localized performance", loc="left", fontweight="bold")
    axes[1].set_title("(b) Network-wide performance", loc="left", fontweight="bold")
    fig.tight_layout(w_pad=1.5)
    save_figure(fig, figure_dir / "city_metric_comparison")
    plt.close(fig)


def plot_local_global_tradeoff(figure_dir: Path, rows: list[dict[str, Any]]) -> None:
    plt = configure_matplotlib()
    colors = {
        "TLS": "#355C7D",
        "Rerouting": "#2A9D8F",
        "Corridor speed": "#E9A03B",
        "Bundle": "#8C6BB1",
        "No-op": "#8D99AE",
    }
    applied = [row for row in rows if row["action"] != "No-op"]
    xmax = max(row["global_delay_reduction_pct"] for row in rows) * 1.18 + 0.05
    ymax = max(row["top10_reduction_pct"] for row in rows) * 1.13 + 0.3
    fig, ax = plt.subplots(figsize=(6.7, 3.8))
    ax.fill_between([0.0, xmax], [1.0, 1.0], [ymax, ymax], color="#DDEFE3", alpha=0.65)
    ax.text(xmax * 0.97, ymax * 0.94, "Feasible improvement region", ha="right", color="#3A6B4A", fontsize=8)
    for action in ["TLS", "Rerouting", "Corridor speed", "Bundle"]:
        action_rows = [row for row in applied if row["action"] == action]
        if not action_rows:
            continue
        ax.scatter(
            [row["global_delay_reduction_pct"] for row in action_rows],
            [row["top10_reduction_pct"] for row in action_rows],
            s=46,
            color=colors[action],
            edgecolor="white",
            linewidth=0.7,
            label=action,
            zorder=3,
        )
        for row in action_rows:
            code = CITY_CODES.get(row["city_key"], row["city_key"][:3].upper())
            ax.annotate(
                f"{code}-{row['seed']}",
                (row["global_delay_reduction_pct"], row["top10_reduction_pct"]),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=6.5,
                color="#333333",
            )
    noop_count = sum(row["action"] == "No-op" for row in rows)
    if noop_count:
        ax.scatter([0.0], [0.0], s=62, marker="X", color=colors["No-op"], label=f"No-op ({noop_count})", zorder=4)
    ax.axvline(0.0, color="#555555", linestyle="--", linewidth=0.8)
    ax.axhline(1.0, color="#555555", linestyle="--", linewidth=0.8)
    ax.set_xlim(-0.12, xmax)
    ax.set_ylim(-0.55, ymax)
    ax.set_xlabel("Network-wide delay reduction (%)")
    ax.set_ylabel("Top-10 time-loss reduction (%)")
    ax.grid(color="#D9D9D9", linewidth=0.6, alpha=0.75)
    ax.set_axisbelow(True)
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=7.5)
    save_figure(fig, figure_dir / "local_global_tradeoff")
    plt.close(fig)


def candidate_constraint_rates(
    raw_rows: list[Mapping[str, Any]],
    primary_case_ids: set[str],
) -> dict[str, Any]:
    checks = [
        "top10_improvement",
        "global_delay",
        "neighbor_spillover",
        "throughput",
        "teleports",
    ]
    families = ["TLS", "Corridor speed", "Routing (1 edge)", "Routing (3 edges)", "Bundle"]
    totals: Counter[str] = Counter()
    passed: dict[str, Counter[str]] = defaultdict(Counter)
    for row in raw_rows:
        if row.get("case_id") not in primary_case_ids:
            continue
        for candidate in row.get("candidate_ranking") or []:
            family = candidate_family(candidate)
            if family not in families:
                continue
            totals[family] += 1
            assessment = candidate.get("selection_assessment") or {}
            constraint_checks = assessment.get("constraint_checks") or {}
            for check in checks:
                passed[family][check] += bool(constraint_checks.get(check))
            passed[family]["all"] += bool(candidate.get("selection_eligible"))
    rates = {
        family: {
            check: (100.0 * passed[family][check] / totals[family] if totals[family] else 0.0)
            for check in checks + ["all"]
        }
        for family in families
    }
    return {"families": families, "totals": dict(totals), "rates": rates}


def plot_candidate_constraint_rates(figure_dir: Path, report: Mapping[str, Any]) -> None:
    plt = configure_matplotlib()
    families = list(report["families"])
    checks = [
        "top10_improvement",
        "global_delay",
        "neighbor_spillover",
        "throughput",
        "teleports",
        "all",
    ]
    labels = [
        "Top-10\n$\\geq$1%",
        "Global\n$\\geq$0%",
        "Neighbor\n$\\geq$-5%",
        "Throughput",
        "Teleports",
        "All",
    ]
    matrix = [[report["rates"][family][check] for check in checks] for family in families]
    fig, ax = plt.subplots(figsize=(7.0, 3.45))
    image = ax.imshow(matrix, cmap="Blues", vmin=0.0, vmax=100.0, aspect="auto")
    ax.set_xticks(range(len(checks)), labels)
    ax.set_yticks(
        range(len(families)),
        [f"{family} (n={report['totals'][family]})" for family in families],
    )
    for row_index, values in enumerate(matrix):
        for column_index, value in enumerate(values):
            ax.text(
                column_index,
                row_index,
                f"{value:.0f}%",
                ha="center",
                va="center",
                fontsize=8,
                color="white" if value >= 55 else "#222222",
            )
    ax.set_xticks([index - 0.5 for index in range(1, len(checks))], minor=True)
    ax.set_yticks([index - 0.5 for index in range(1, len(families))], minor=True)
    ax.grid(which="minor", color="white", linewidth=1.2)
    ax.tick_params(which="minor", bottom=False, left=False)
    colorbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.025)
    colorbar.set_label("Candidates passing check (%)")
    fig.tight_layout()
    save_figure(fig, figure_dir / "candidate_constraint_pass_rates")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    table_dir = Path(args.table_dir)
    figure_dir = Path(args.figure_dir)
    table_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    raw_rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    valid_rows = [
        normalize_row(row)
        for row in raw_rows
        if row.get("status") == "ok" and (row.get("scope_objective") or {}).get("available")
    ]
    by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        by_city[row["city_key"]].append(row)

    max_seed_count = max(len({row["seed"] for row in rows}) for rows in by_city.values())
    primary_cities = [
        city for city in CITY_LABELS if city in by_city and len({row["seed"] for row in by_city[city]}) == max_seed_count
    ]
    common_seeds = sorted(
        set.intersection(*(set(row["seed"] for row in by_city[city]) for city in primary_cities))
    )
    primary_rows = [
        row for row in valid_rows if row["city_key"] in primary_cities and row["seed"] in common_seeds
    ]
    supplemental_rows = [row for row in valid_rows if row not in primary_rows]

    city_summaries = {city: aggregate(by_city[city]) for city in primary_cities}
    overall = aggregate(primary_rows)
    report = {
        "source_manifest": str(manifest_path),
        "primary_cities": primary_cities,
        "primary_seeds": common_seeds,
        "primary": overall,
        "by_city": city_summaries,
        "supplementary_runs": supplemental_rows,
        "primary_action_counts": dict(Counter(row["action"] for row in primary_rows)),
        "all_valid_action_counts": dict(Counter(row["action"] for row in valid_rows)),
    }
    constraint_report = candidate_constraint_rates(raw_rows, {row["case_id"] for row in primary_rows})
    report["candidate_constraint_pass_rates"] = constraint_report

    write_csv(table_dir / "top10_seeded_runs.csv", valid_rows)
    (table_dir / "top10_seeded_summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_latex_table(
        table_dir / "top10_seeded_results_table.tex",
        city_summaries,
        overall,
        primary_cities,
    )
    plot_seeded_reductions(figure_dir, primary_rows, primary_cities, common_seeds)
    plot_action_counts(figure_dir, primary_rows)
    plot_city_metric_comparison(figure_dir, city_summaries, primary_cities)
    plot_local_global_tradeoff(figure_dir, primary_rows)
    plot_candidate_constraint_rates(figure_dir, constraint_report)

    print(f"Primary design: {len(primary_cities)} cities x {len(common_seeds)} seeds = {len(primary_rows)} runs")
    print(
        "Top-10 reduction: "
        f"{overall['top10_reduction_pct']['mean']:.2f} +/- "
        f"{overall['top10_reduction_pct']['sample_sd']:.2f}%"
    )
    print(
        "Global delay reduction: "
        f"{overall['global_delay_reduction_pct']['mean']:.2f} +/- "
        f"{overall['global_delay_reduction_pct']['sample_sd']:.2f}%"
    )
    print(f"Supplementary valid runs: {len(supplemental_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
