"""Analyze the matched mutation-selector ablation.

The main proposal-source comparison varies the whole search pipeline, so it
cannot say whether the *model* contributes anything at the one point where it
actually acts: choosing one entry from a deterministically enumerated catalog
of legal mutations.  This script compares three arms that share the grounded
initialization seed, the retained parent, the catalog, the beam width and the
per-case SUMO budget, and differ only in who picks:

``random_initial_llm``
    The LLM condition reported in the paper.  Read from the existing runs
    rather than re-executed, because the catalogs are byte-identical across
    arms (verified) and re-running would require the original model.

``random_initial_catalog_first``
    Take the enumerator's own highest-priority entry.

``random_initial_catalog_random``
    Draw uniformly from the same catalog.

Usage::

    python3 experiment/analyze_selector_ablation.py
    python3 experiment/analyze_selector_ablation.py --json out.json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import statistics
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Tuple

LLM_ARM = "random_initial_llm"
FIRST_ARM = "random_initial_catalog_first"
RANDOM_ARM = "random_initial_catalog_random"
ARMS = (LLM_ARM, FIRST_ARM, RANDOM_ARM)
ARM_LABELS = {
    LLM_ARM: "LLM selection",
    FIRST_ARM: "Catalog-first",
    RANDOM_ARM: "Catalog-random",
}

# Paper definition: a feasible selected plan with at least 1% Top-10 reduction.
SUCCESS_TOP10_THRESHOLD = 0.01

LLM_GLOB = "experiment/luna-five-city-legal-mutation-seed4*/*/run_*/"
ABLATION_GLOB = "experiment/selector-ablation/*/run_*/"

CASE_RE = re.compile(r"^(?P<city>.+?)_equal_scale_delay_seed_(?P<seed>\d+)$")


def _parse_case(case_id: str) -> Optional[Tuple[str, int]]:
    match = CASE_RE.match(case_id)
    if not match:
        return None
    return match.group("city"), int(match.group("seed"))


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _record_from_run_dir(run_dir: str) -> Optional[Dict[str, Any]]:
    """Extract the per-case outcome the paper reports, plus decision detail."""
    metrics = _load_json(os.path.join(run_dir, "07_proposal_source_metrics.json"))
    if not metrics:
        return None
    final = metrics.get("final_selection") or {}
    rates = metrics.get("rates") or {}
    counts = metrics.get("counts") or {}
    top10 = final.get("top10_relative_improvement")
    if top10 is None:
        return None
    no_op = bool(final.get("no_op"))
    record = {
        "proposal_source": metrics.get("proposal_source"),
        "top10_reduction": float(top10),
        "global_delay_reduction": float(
            final.get("global_delay_relative_improvement") or 0.0
        ),
        "selected_action": final.get("action"),
        "no_op": no_op,
        "success": (not no_op) and float(top10) >= SUCCESS_TOP10_THRESHOLD,
        "simulation_efficiency": float(rates.get("simulation_efficiency") or 0.0),
        "proposal_validation_rate": float(
            rates.get("proposal_validation_rate") or 0.0
        ),
        "sumo_evaluations": int(counts.get("sumo_evaluations") or 0),
        "decisions": _decisions_from_run_dir(run_dir),
        "attempt_status": _attempt_status_from_run_dir(run_dir),
    }
    return record


def _attempt_status_from_run_dir(run_dir: str) -> Dict[str, int]:
    """Count refinement attempts by outcome.

    A ``generation_error`` means the selector produced nothing usable and the
    reserved SUMO evaluation was lost.  The LLM arm incurs these; the
    deterministic selectors cannot, and reporting the two arms without this
    breakdown would overstate how comparable their realized budgets were.
    """
    debate = _load_json(os.path.join(run_dir, "03_debate.json"))
    counts: Dict[str, int] = defaultdict(int)
    if not debate:
        return dict(counts)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            refinements = node.get("refinements")
            if isinstance(refinements, list):
                for row in refinements:
                    if isinstance(row, Mapping):
                        counts[str(row.get("status") or "unknown")] += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(debate)
    return dict(counts)


def _decisions_from_run_dir(run_dir: str) -> List[Dict[str, Any]]:
    """Every catalog selection made in this run, won or lost."""
    debate = _load_json(os.path.join(run_dir, "03_debate.json"))
    if not debate:
        return []
    out: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            refinement = node.get("refinement")
            if isinstance(refinement, Mapping) and refinement.get(
                "mutation_catalog_ids"
            ):
                ids = list(refinement.get("mutation_catalog_ids") or [])
                selected = refinement.get("selected_mutation_id")
                out.append(
                    {
                        "parent_proposal_id": node.get("parent_proposal_id"),
                        "selected_mutation_id": selected,
                        "catalog_size": int(refinement.get("mutation_catalog_size") or 0),
                        "catalog_ids": ids,
                        "selected_position": (
                            ids.index(selected) + 1 if selected in ids else None
                        ),
                        "selector_kind": refinement.get("selector_kind") or "llm",
                        "changed_fields": refinement.get("changed_fields"),
                    }
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(debate)
    # Deduplicate: the same refinement appears under several keys.
    unique: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for row in out:
        unique[(row["parent_proposal_id"], row["selected_mutation_id"])] = row
    return list(unique.values())


def collect() -> Dict[str, Dict[Tuple[str, int], Dict[str, Any]]]:
    """Load every arm, keyed by (city, seed)."""
    arms: Dict[str, Dict[Tuple[str, int], Dict[str, Any]]] = {
        arm: {} for arm in ARMS
    }

    for run_dir in sorted(glob.glob(LLM_GLOB)):
        case_id = run_dir.rstrip("/").split(os.sep)[-2]
        key = _parse_case(case_id)
        if not key:
            continue
        record = _record_from_run_dir(run_dir)
        if record and record.get("proposal_source") == LLM_ARM:
            arms[LLM_ARM][key] = record

    for run_dir in sorted(glob.glob(ABLATION_GLOB)):
        case_id = run_dir.rstrip("/").split(os.sep)[-2]
        key = _parse_case(case_id)
        if not key:
            continue
        for arm in (FIRST_ARM, RANDOM_ARM):
            record = _record_from_run_dir(os.path.join(run_dir, arm))
            if record:
                arms[arm][key] = record

    return arms


def _pct(values: List[float]) -> str:
    if not values:
        return "n/a"
    mean = statistics.fmean(values) * 100
    if len(values) < 2:
        return f"{mean:.2f}"
    sd = statistics.stdev([v * 100 for v in values])
    return f"{mean:.2f} ± {sd:.2f}"


def summarize(arms, keys: List[Tuple[str, int]]) -> List[Dict[str, Any]]:
    rows = []
    for arm in ARMS:
        recs = [arms[arm][k] for k in keys if k in arms[arm]]
        if not recs:
            rows.append({"arm": arm, "n": 0})
            continue
        rows.append(
            {
                "arm": arm,
                "n": len(recs),
                "top10": _pct([r["top10_reduction"] for r in recs]),
                "top10_mean": statistics.fmean(r["top10_reduction"] for r in recs),
                "global": _pct([r["global_delay_reduction"] for r in recs]),
                "success": sum(r["success"] for r in recs),
                "success_rate": sum(r["success"] for r in recs) / len(recs),
                "sim_eff": statistics.fmean(r["simulation_efficiency"] for r in recs),
                "valid": statistics.fmean(r["proposal_validation_rate"] for r in recs),
                "evals": statistics.fmean(r["sumo_evaluations"] for r in recs),
            }
        )
    return rows


def paired_tests(arms, keys: List[Tuple[str, int]]) -> List[Dict[str, Any]]:
    try:
        from scipy.stats import wilcoxon
    except ImportError:
        return []
    out = []
    for arm in (FIRST_ARM, RANDOM_ARM):
        pairs = [
            (arms[LLM_ARM][k]["top10_reduction"], arms[arm][k]["top10_reduction"])
            for k in keys
            if k in arms[LLM_ARM] and k in arms[arm]
        ]
        if len(pairs) < 3:
            continue
        llm = [a for a, _ in pairs]
        other = [b for _, b in pairs]
        diffs = [a - b for a, b in pairs]
        wins = sum(1 for d in diffs if d > 1e-12)
        losses = sum(1 for d in diffs if d < -1e-12)
        ties = len(diffs) - wins - losses
        row = {
            "comparison": f"{ARM_LABELS[LLM_ARM]} vs {ARM_LABELS[arm]}",
            "n": len(pairs),
            "llm_mean": statistics.fmean(llm),
            "other_mean": statistics.fmean(other),
            "mean_diff": statistics.fmean(diffs),
            "wins": wins,
            "losses": losses,
            "ties": ties,
        }
        if any(abs(d) > 1e-12 for d in diffs):
            try:
                stat, p = wilcoxon(llm, other, zero_method="wilcox")
                row["wilcoxon_stat"] = float(stat)
                row["wilcoxon_p"] = float(p)
            except ValueError as exc:
                row["wilcoxon_error"] = str(exc)
        else:
            row["wilcoxon_error"] = "all paired differences are zero"
        out.append(row)
    return out


def decision_view(arms, keys: List[Tuple[str, int]]) -> Dict[str, Any]:
    """Selection behaviour at the decision level, which is where the LLM acts."""
    out: Dict[str, Any] = {}
    for arm in ARMS:
        positions: List[int] = []
        sizes: List[int] = []
        for key in keys:
            record = arms[arm].get(key)
            if not record:
                continue
            for decision in record.get("decisions") or []:
                if decision.get("selected_position"):
                    positions.append(int(decision["selected_position"]))
                    sizes.append(int(decision["catalog_size"]))
        if not positions:
            out[arm] = {"decisions": 0}
            continue
        # Relative position in the enumerator's ordering, 0 = first entry.
        rel = [
            (p - 1) / (s - 1) if s > 1 else 0.0 for p, s in zip(positions, sizes)
        ]
        status: Dict[str, int] = defaultdict(int)
        for key in keys:
            record = arms[arm].get(key)
            if not record:
                continue
            for name, count in (record.get("attempt_status") or {}).items():
                status[name] += count
        out[arm] = {
            "decisions": len(positions),
            "mean_catalog_size": statistics.fmean(sizes),
            "mean_position": statistics.fmean(positions),
            "mean_relative_position": statistics.fmean(rel),
            "picked_first_entry": sum(1 for p in positions if p == 1),
            "attempt_status": dict(status),
            "generation_errors": int(status.get("generation_error", 0)),
            "attempts_total": int(sum(status.values())),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="Also write the full result as JSON.")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Exit non-zero unless all three arms cover the same 15 cases.",
    )
    args = parser.parse_args()

    arms = collect()
    complete = [
        key
        for key in sorted(set().union(*(set(a) for a in arms.values())) if any(arms.values()) else [])
        if all(key in arms[arm] for arm in ARMS)
    ]

    print("=== Arm coverage ===")
    for arm in ARMS:
        print(f"  {ARM_LABELS[arm]:16s} {len(arms[arm]):2d} cases")
    print(f"  cases with all three arms: {len(complete)}")
    if not complete:
        print("\nNo case is covered by all three arms yet; nothing to compare.")
        raise SystemExit(1 if args.require_complete else 0)

    print("\n=== Matched selector comparison ===")
    header = f"  {'Arm':16s} {'n':>3s} {'Top-10 (%)':>17s} {'Global (%)':>17s} {'Success':>9s} {'Sim.eff':>8s} {'Evals':>6s}"
    print(header)
    rows = summarize(arms, complete)
    for row in rows:
        if not row.get("n"):
            print(f"  {ARM_LABELS[row['arm']]:16s}   0   (no data)")
            continue
        print(
            f"  {ARM_LABELS[row['arm']]:16s} {row['n']:3d} {row['top10']:>17s} "
            f"{row['global']:>17s} {row['success']:3d}/{row['n']:<5d} "
            f"{row['sim_eff'] * 100:7.1f}% {row['evals']:6.1f}"
        )

    print("\n=== Paired tests on Top-10 reduction ===")
    tests = paired_tests(arms, complete)
    if not tests:
        print("  not enough paired cases yet")
    for test in tests:
        print(f"  {test['comparison']}  (n={test['n']})")
        print(
            f"    mean {test['llm_mean'] * 100:.2f}% vs {test['other_mean'] * 100:.2f}%"
            f"   difference {test['mean_diff'] * 100:+.2f} pp"
        )
        print(
            f"    LLM better / worse / tied: {test['wins']} / {test['losses']} / {test['ties']}"
        )
        if "wilcoxon_p" in test:
            print(
                f"    Wilcoxon signed-rank: W={test['wilcoxon_stat']:.1f}, p={test['wilcoxon_p']:.4f}"
            )
        else:
            print(f"    Wilcoxon: {test.get('wilcoxon_error')}")

    print("\n=== Decision-level selection behaviour ===")
    decisions = decision_view(arms, complete)
    print(
        f"  {'Arm':16s} {'decisions':>9s} {'catalog':>8s} {'position':>9s} {'relative':>9s} {'chose 1st':>10s}"
    )
    for arm in ARMS:
        info = decisions.get(arm) or {}
        if not info.get("decisions"):
            print(f"  {ARM_LABELS[arm]:16s} {'0':>9s}")
            continue
        print(
            f"  {ARM_LABELS[arm]:16s} {info['decisions']:9d} "
            f"{info['mean_catalog_size']:8.1f} {info['mean_position']:9.2f} "
            f"{info['mean_relative_position']:9.3f} {info['picked_first_entry']:10d}"
        )
    print(
        "\n  'relative' is the pick's position in the enumerator's ordering, "
        "0 = first entry, 1 = last."
    )

    print("\n=== Refinement attempt outcomes (realized budget) ===")
    print(f"  {'Arm':16s} {'attempts':>8s} {'gen.errors':>11s}  status breakdown")
    for arm in ARMS:
        info = decisions.get(arm) or {}
        if not info.get("attempts_total"):
            print(f"  {ARM_LABELS[arm]:16s} {'0':>8s}")
            continue
        breakdown = ", ".join(
            f"{k}={v}" for k, v in sorted((info.get("attempt_status") or {}).items())
        )
        print(
            f"  {ARM_LABELS[arm]:16s} {info['attempts_total']:8d} "
            f"{info['generation_errors']:11d}  {breakdown}"
        )
    print(
        "\n  A generation_error means the selector returned nothing usable and the "
        "reserved\n  SUMO evaluation was lost. Only the LLM arm can incur these, so "
        "its realized\n  budget is smaller than the matched cap. Report this "
        "alongside the outcome table."
    )

    if args.json:
        payload = {
            "cases": [f"{c}_seed_{s}" for c, s in complete],
            "summary": rows,
            "paired_tests": tests,
            "decisions": decisions,
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        print(f"\nWrote {args.json}")

    if args.require_complete and len(complete) != 15:
        raise SystemExit(f"expected 15 complete cases, found {len(complete)}")


if __name__ == "__main__":
    main()
