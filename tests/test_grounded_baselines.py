import json
from pathlib import Path

import pytest

from agents.grounded_baselines import (
    REROUTE_FRACTIONS,
    SPEED_FACTORS,
    build_legal_candidate_pool,
    generate_deterministic_heuristic,
    generate_grounded_baseline,
    generate_grounded_random,
    nearest_tls_ids,
    objective_edge_rows,
)
from experiment.run_optimization_only import _proposal_source_metrics, _top10_scope_objective
from experiment.run_proposal_source_comparison import (
    aggregate_rows,
    build_parser as build_comparison_parser,
)


def _artifacts(tmp_path: Path):
    net = tmp_path / "network.net.xml"
    route = tmp_path / "routes.rou.xml"
    cfg = tmp_path / "sim.sumocfg"
    edges = "\n".join(
        f'<edge id="e{i}" name="Main Road" from="j{i-1}" to="j{i}">'
        f'<lane id="e{i}_0" speed="10" length="100"/></edge>'
        for i in range(1, 6)
    )
    net.write_text(
        (
            "<net>"
            + edges
            + '<tlLogic id="j2" type="static" programID="0" offset="0">'
            + '<phase duration="30" state="Gr"/>'
            + '<phase duration="3" state="yr"/>'
            + '<phase duration="30" state="rG"/>'
            + '<phase duration="3" state="ry"/>'
            + "</tlLogic>"
            + '<connection from="e1" to="e2" fromLane="0" toLane="0" tl="j2" linkIndex="0"/>'
            + '<connection from="e2" to="e3" fromLane="0" toLane="0" tl="j2" linkIndex="1"/>'
            + "</net>"
        ),
        encoding="utf-8",
    )
    route.write_text(
        '<routes><vehicle id="v0" depart="0"><route edges="e1 e2 e3 e4 e5"/></vehicle></routes>',
        encoding="utf-8",
    )
    cfg.write_text(
        (
            "<configuration><input>"
            f'<net-file value="{net}"/><route-files value="{route}"/>'
            "</input></configuration>"
        ),
        encoding="utf-8",
    )
    return {"net": str(net), "routes": str(route), "cfg": str(cfg)}


def _diagnosis():
    rows = [
        {
            "edge_id": f"e{i}",
            "congestion_score": 1.0 - i / 10,
            "from_junction": f"j{i-1}",
            "to_junction": f"j{i}",
        }
        for i in range(1, 6)
    ]
    return {
        "top_congested_edges": rows,
        "bottlenecks": rows,
        "bottleneck_subgraph": {
            "tls_ids": ["j2", "j4"],
            "edges": rows,
        },
    }


def _semantic(proposals):
    result = []
    for proposal in proposals:
        args = dict(proposal["args"])
        for key in ("net_path", "route_path", "cfg_path"):
            args.pop(key, None)
        result.append((proposal["action"], json.dumps(args, sort_keys=True)))
    return result


def test_nearest_tls_is_ranked_by_subgraph_hops():
    assert nearest_tls_ids(_diagnosis(), "e1") == ["j2", "j4"]


def test_grounded_random_is_reproducible_and_uses_discrete_parameters(tmp_path):
    artifacts = _artifacts(tmp_path)
    first = generate_grounded_random(
        _diagnosis(), artifacts, attempt_budget=9, seed=17, optimizer="all"
    )
    second = generate_grounded_random(
        _diagnosis(), artifacts, attempt_budget=9, seed=17, optimizer="all"
    )

    assert _semantic(first) == _semantic(second)
    assert all(proposal["action"] for proposal in first)
    assert all(proposal["validation"]["ok"] for proposal in first)
    assert all(proposal["grounding"]["legal_candidate_pool"] for proposal in first)
    for proposal in first:
        if proposal["action"] == "set_corridor_speed":
            assert proposal["args"]["speed_factor"] in SPEED_FACTORS
        if proposal["action"] == "reroute_flows":
            assert proposal["args"]["reroute_fraction"] in REROUTE_FRACTIONS
            assert 1 <= len(proposal["args"]["avoid_edges"]) <= 3
            assert set(proposal["args"]["avoid_edges"]) <= {"e1", "e2", "e3", "e4", "e5"}


def test_legal_pool_excludes_short_and_validator_rejected_targets(tmp_path):
    artifacts = _artifacts(tmp_path)
    diagnosis = _diagnosis()
    diagnosis["top_congested_edges"] = [
        {**diagnosis["top_congested_edges"][0], "length_m": 9.9},
        {**diagnosis["top_congested_edges"][1], "length_m": 10.0},
    ]

    assert [row["edge_id"] for row in objective_edge_rows(diagnosis)] == ["e2"]

    pool = build_legal_candidate_pool(diagnosis, artifacts)
    flattened = [spec for specs in pool.values() for spec in specs]

    assert flattened
    assert all(spec["validation"]["ok"] for spec in flattened)
    assert all("e1" not in json.dumps(spec["args"]) for spec in flattened)
    # The fixture has no reverse edge, so one-way targets must be removed before
    # proposal sampling instead of consuming a random-initialization slot.
    assert not any(spec["action"] == "make_road_one_way" for spec in flattened)


def test_random_initialized_llm_shares_random_baseline_prefix(tmp_path):
    artifacts = _artifacts(tmp_path)
    hybrid = generate_grounded_baseline(
        "random_initial_llm",
        _diagnosis(),
        artifacts,
        attempt_budget=3,
        seed=44,
        optimizer="all",
    )
    random_baseline = generate_grounded_baseline(
        "grounded_random",
        _diagnosis(),
        artifacts,
        attempt_budget=6,
        seed=44,
        optimizer="all",
    )

    assert _semantic(hybrid["proposals"]) == _semantic(random_baseline["proposals"][:3])
    assert [proposal["agent"] for proposal in hybrid["proposals"]] == [
        "signal_optimizer",
        "network_optimizer",
        "demand_routing_optimizer",
    ]
    assert all(
        proposal["proposal_source"] == "random_initial_llm"
        and proposal["initialization_source"] == "grounded_random"
        and proposal["proposal_id"].startswith("random_initial_llm_")
        for proposal in hybrid["proposals"]
    )


def test_deterministic_heuristic_starts_with_nearest_tls_top_edge_and_top_routing(tmp_path):
    proposals = generate_deterministic_heuristic(
        _diagnosis(),
        _artifacts(tmp_path),
        attempt_budget=3,
        optimizer="all",
    )

    assert [proposal["agent"] for proposal in proposals] == [
        "signal_optimizer",
        "network_optimizer",
        "demand_routing_optimizer",
    ]
    assert proposals[0]["args"]["tl_id"] == "j2"
    assert proposals[1]["args"]["edge_id"] == "e1"
    assert proposals[1]["args"]["speed_factor"] == 0.8
    assert proposals[2]["args"]["avoid_edges"] == ["e1"]
    assert proposals[2]["args"]["reroute_fraction"] == 0.5


def _kpi(target, neighbor, delay):
    return {
        "mean_delay": delay,
        "throughput": 1.0,
        "teleport_count": 0,
        "focus_scope": {
            "target": {"total_time_loss_s": target},
            "neighbors": {"total_time_loss_s": neighbor},
        },
    }


def test_simulation_efficiency_counts_only_feasible_one_percent_improvements():
    baseline = _kpi(100.0, 200.0, 50.0)
    improved_kpi = _kpi(90.0, 202.0, 49.0)
    unsafe_kpi = _kpi(80.0, 230.0, 49.0)
    candidates = []
    for index, current in enumerate((improved_kpi, unsafe_kpi), 1):
        candidates.append(
            {
                "status": "ok",
                "sumo_evaluated": True,
                "proposal": {"proposal_id": f"p{index}", "action": "x"},
                "optimized": {"kpi": current},
                "modification": {},
                "scope_objective": _top10_scope_objective(baseline, current),
            }
        )
    raw = [
        {"action": "x", "validation": {"ok": True}},
        {"action": "x", "validation": {"ok": True}},
    ]
    best = {**candidates[0], "is_baseline": False}

    metrics = _proposal_source_metrics(
        raw,
        candidates,
        baseline,
        best,
        proposal_source="test",
        sumo_budget=2,
        objective_scope="top10",
    )

    assert metrics["counts"]["sumo_evaluations"] == 2
    assert metrics["counts"]["feasible_improving_candidates"] == 1
    assert metrics["rates"]["simulation_efficiency"] == pytest.approx(0.5)
    assert metrics["rates"]["improving_1pct_rate_per_sumo_evaluation"] == 1.0
    assert metrics["rates"]["all_constraints_pass_rate"] == pytest.approx(0.5)


def test_aggregate_uses_micro_averaged_simulation_efficiency():
    rows = [
        {
            "proposal_source": "llm",
            "raw_proposals": 3,
            "validated_proposals": 3,
            "proposal_noops": 0,
            "sumo_evaluations": 3,
            "successful_simulations": 3,
            "safety_feasible_candidates": 2,
            "improving_1pct_candidates": 2,
            "all_constraints_passed_candidates": 1,
            "feasible_improving_candidates": 1,
            "final_noop": False,
            "final_top10_improvement": 0.1,
            "final_global_delay_improvement": 0.01,
            "sumo_budget_utilization": 1.0,
            "elapsed_s": 1,
        },
        {
            "proposal_source": "llm",
            "raw_proposals": 1,
            "validated_proposals": 0,
            "proposal_noops": 1,
            "sumo_evaluations": 1,
            "successful_simulations": 1,
            "safety_feasible_candidates": 0,
            "improving_1pct_candidates": 0,
            "all_constraints_passed_candidates": 0,
            "feasible_improving_candidates": 0,
            "final_noop": True,
            "final_top10_improvement": 0.0,
            "final_global_delay_improvement": 0.0,
            "sumo_budget_utilization": 1.0,
            "elapsed_s": 1,
        },
    ]

    aggregate = aggregate_rows(rows)[0]

    assert aggregate["proposal_validation_rate"] == pytest.approx(0.75)
    assert aggregate["simulation_efficiency"] == pytest.approx(0.25)
    assert aggregate["final_noop_rate"] == pytest.approx(0.5)


def test_comparison_defaults_to_random_initialized_llm_and_automatic_attempt_budgets():
    args = build_comparison_parser().parse_args(["--cfg", "sim.sumocfg"])

    assert args.methods == [
        "random_initial_llm",
        "grounded_random",
        "deterministic_heuristic",
    ]
    assert args.proposal_attempt_budget is None
