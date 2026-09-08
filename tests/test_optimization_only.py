import pytest

import experiment.run_optimization_only as optimization_only
from experiment.run_optimization_only import (
    _adjust_kpi_for_source_demand,
    _aggregate_route_validation,
    _baseline_candidate,
    _best_so_far_trace,
    _bind_bundle_action_args,
    _candidate_selection_assessment,
    _expand_routing_variants,
    _feedback_parent_candidates,
    _parse_routing_edge_counts,
    _proposal_source_metrics,
    _resolved_proposal_attempt_budget,
    _select_best_candidate,
    _top10_scope_objective,
)


def test_dropped_route_demand_is_rejected_instead_of_assigned_a_delay_penalty():
    baseline = {"departed_trips": 800}
    optimized = {
        "completed_trips": 797,
        "departed_trips": 797,
        "throughput": 1.0,
        "mean_delay": 42.0,
    }
    modification = {
        "route_validation": {"before_demand_units": 800, "after_demand_units": 797}
    }

    with pytest.raises(ValueError, match="route demand was not preserved"):
        _adjust_kpi_for_source_demand(baseline, optimized, modification)


def test_preserved_route_demand_keeps_measured_delay_and_source_throughput():
    baseline = {"departed_trips": 800}
    optimized = {
        "completed_trips": 797,
        "departed_trips": 800,
        "throughput": 797 / 800,
        "mean_delay": 42.0,
    }
    modification = {
        "route_validation": {"before_demand_units": 800, "after_demand_units": 800}
    }

    adjusted = _adjust_kpi_for_source_demand(baseline, optimized, modification)

    assert adjusted["simulation_completion_rate"] == pytest.approx(797 / 800)
    assert adjusted["throughput"] == pytest.approx(797 / 800)
    assert adjusted["dropped_before_departure"] == 0
    assert adjusted["mean_delay"] == 42.0


def test_best_candidate_uses_lowest_measured_kpi_score():
    candidates = [
        {"status": "ok", "kpi_score": 0.19, "proposal": {"proposal_id": "signal"}},
        {"status": "error", "proposal": {"proposal_id": "network"}},
        {"status": "ok", "kpi_score": 0.16, "proposal": {"proposal_id": "routing"}},
    ]

    selected = _select_best_candidate(candidates)

    assert selected["proposal"]["proposal_id"] == "routing"


def test_baseline_guard_wins_ties_and_rejects_worse_interventions():
    baseline = {
        "status": "ok",
        "is_baseline": True,
        "kpi_score": 0.2,
        "proposal": {"proposal_id": "baseline_noop"},
    }
    tied = {
        "status": "ok",
        "kpi_score": 0.2,
        "proposal": {"proposal_id": "signal_noop"},
    }
    worse = {
        "status": "ok",
        "kpi_score": 0.21,
        "proposal": {"proposal_id": "routing_worse"},
    }

    selected = _select_best_candidate([worse, tied, baseline])

    assert selected["proposal"]["proposal_id"] == "baseline_noop"


def test_delay_guard_rejects_tiny_objective_and_delay_improvements():
    baseline = {
        "status": "ok",
        "is_baseline": True,
        "kpi_score": 0.2,
        "objective": {"delta": 0.0},
        "proposal": {"proposal_id": "baseline_noop"},
    }
    tiny = {
        "status": "ok",
        "kpi_score": 0.19998,
        "objective": {
            "delta": -0.00002,
            "current": {"terms": {"delay": {"normalized": 0.99997}}},
        },
        "proposal": {"proposal_id": "tiny_change"},
    }

    selected = _select_best_candidate([tiny, baseline], profile="delay")

    assert selected["proposal"]["proposal_id"] == "baseline_noop"


def test_delay_guard_requires_both_objective_and_primary_improvement():
    baseline = {
        "status": "ok",
        "is_baseline": True,
        "kpi_score": 0.2,
        "objective": {"delta": 0.0},
        "proposal": {"proposal_id": "baseline_noop"},
    }
    objective_only = {
        "status": "ok",
        "kpi_score": 0.19,
        "objective": {
            "delta": -0.01,
            "current": {"terms": {"delay": {"normalized": 0.9995}}},
        },
        "proposal": {"proposal_id": "objective_only"},
    }
    meaningful = {
        "status": "ok",
        "kpi_score": 0.195,
        "objective": {
            "delta": -0.005,
            "current": {"terms": {"delay": {"normalized": 0.98}}},
        },
        "proposal": {"proposal_id": "meaningful"},
    }

    selected = _select_best_candidate(
        [objective_only, meaningful, baseline],
        profile="delay",
    )

    assert selected["proposal"]["proposal_id"] == "meaningful"


def test_baseline_candidate_has_zero_delta_and_reuses_measured_run():
    kpi = {
        "mean_speed": 10,
        "mean_delay": 20,
        "throughput": 1,
        "co2": 100,
        "completed_trips": 4,
        "departed_trips": 4,
    }
    candidate = _baseline_candidate(
        {"cfg": "sim.sumocfg", "net": "net.xml", "routes": "routes.xml"},
        {"status": "ok"},
        kpi,
        {"bottlenecks": []},
        {"delay": 0.5, "co2": 0.1, "throughput": 0.25, "speed": 0.15},
    )

    assert candidate["is_baseline"] is True
    assert candidate["objective"]["delta"] == 0
    assert candidate["selected_action"]["action"] == "no_op"
    assert candidate["optimized"]["reused_baseline_run"] is True


def _top10_kpi(*, target, neighbor, global_delay, throughput=1.0, teleports=0):
    return {
        "mean_speed": 10,
        "mean_delay": global_delay,
        "throughput": throughput,
        "co2": 100,
        "teleport_count": teleports,
        "focus_scope": {
            "name": "top10",
            "target": {"total_time_loss_s": target},
            "neighbors": {"total_time_loss_s": neighbor},
        },
    }


def _top10_candidate(proposal_id, baseline_kpi, current_kpi):
    return {
        "status": "ok",
        "kpi_score": 0.2,
        "proposal": {"proposal_id": proposal_id},
        "scope_objective": _top10_scope_objective(baseline_kpi, current_kpi),
    }


def test_top10_selection_prefers_focus_gain_with_global_and_spillover_guards():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    baseline = {
        "status": "ok",
        "is_baseline": True,
        "kpi_score": 0.2,
        "proposal": {"proposal_id": "baseline_noop"},
        "scope_objective": _top10_scope_objective(baseline_kpi, baseline_kpi),
    }
    valid = _top10_candidate(
        "valid",
        baseline_kpi,
        _top10_kpi(target=80, neighbor=208, global_delay=49),
    )
    global_worse = _top10_candidate(
        "global_worse",
        baseline_kpi,
        _top10_kpi(target=60, neighbor=190, global_delay=50.6),
    )
    spillover = _top10_candidate(
        "spillover",
        baseline_kpi,
        _top10_kpi(target=50, neighbor=212, global_delay=48),
    )

    selected = _select_best_candidate(
        [global_worse, spillover, valid, baseline],
        profile="delay",
        objective_scope="top10",
    )

    assert selected["proposal"]["proposal_id"] == "valid"
    assert _candidate_selection_assessment(
        global_worse, "delay", "top10"
    )["constraint_checks"]["global_delay"] is False
    assert _candidate_selection_assessment(
        spillover, "delay", "top10"
    )["constraint_checks"]["neighbor_spillover"] is False


def test_top10_selection_allows_up_to_one_percent_global_delay_increase():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    within_tolerance = _top10_candidate(
        "within_tolerance",
        baseline_kpi,
        _top10_kpi(target=80, neighbor=205, global_delay=50.5),
    )
    above_tolerance = _top10_candidate(
        "above_tolerance",
        baseline_kpi,
        _top10_kpi(target=80, neighbor=205, global_delay=50.51),
    )

    within = _candidate_selection_assessment(within_tolerance, "delay", "top10")
    above = _candidate_selection_assessment(above_tolerance, "delay", "top10")

    assert within["maximum_global_delay_relative_increase"] == pytest.approx(0.01)
    assert within["constraint_checks"]["global_delay"] is True
    assert within["selection_eligible"] is True
    assert above["constraint_checks"]["global_delay"] is False
    assert above["selection_eligible"] is False


def test_simulation_efficiency_uses_one_percent_global_delay_tolerance():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    current_kpi = _top10_kpi(target=80, neighbor=205, global_delay=50.25)
    proposal = {
        "proposal_id": "within_tolerance",
        "action": "reroute_flows",
        "validation": {"ok": True},
    }
    candidate = {
        "proposal": proposal,
        "sumo_evaluated": True,
        "status": "ok",
        "optimized": {"kpi": current_kpi},
        "scope_objective": _top10_scope_objective(baseline_kpi, current_kpi),
        "modification": {},
    }

    metrics = _proposal_source_metrics(
        [proposal],
        [candidate],
        baseline_kpi,
        candidate,
        proposal_source="llm",
        sumo_budget=1,
        objective_scope="top10",
    )

    assert metrics["counts"]["feasible_improving_candidates"] == 1
    assert metrics["rates"]["simulation_efficiency"] == pytest.approx(1.0)


def test_top10_selection_rejects_small_gain_throughput_loss_and_new_teleport():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    small = _top10_candidate(
        "small",
        baseline_kpi,
        _top10_kpi(target=99.5, neighbor=190, global_delay=49),
    )
    throughput_loss = _top10_candidate(
        "throughput_loss",
        baseline_kpi,
        _top10_kpi(target=80, neighbor=190, global_delay=49, throughput=0.99),
    )
    teleport = _top10_candidate(
        "teleport",
        baseline_kpi,
        _top10_kpi(target=80, neighbor=190, global_delay=49, teleports=1),
    )

    assert _candidate_selection_assessment(
        small, "delay", "top10"
    )["constraint_checks"]["top10_improvement"] is False
    assert _candidate_selection_assessment(
        throughput_loss, "delay", "top10"
    )["constraint_checks"]["throughput"] is False
    assert _candidate_selection_assessment(
        teleport, "delay", "top10"
    )["constraint_checks"]["teleports"] is False


def test_feedback_beam_is_feasible_first_and_role_diverse():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    routing_best = _top10_candidate(
        "routing_best",
        baseline_kpi,
        _top10_kpi(target=70, neighbor=195, global_delay=48),
    )
    routing_best["proposal"].update(
        {"agent": "demand_routing_optimizer", "action": "reroute_flows"}
    )
    routing_second = _top10_candidate(
        "routing_second",
        baseline_kpi,
        _top10_kpi(target=75, neighbor=195, global_delay=48),
    )
    routing_second["proposal"].update(
        {"agent": "demand_routing_optimizer", "action": "reroute_flows"}
    )
    signal = _top10_candidate(
        "signal",
        baseline_kpi,
        _top10_kpi(target=85, neighbor=198, global_delay=49),
    )
    signal["proposal"].update(
        {"agent": "signal_optimizer", "action": "tls_optimize_and_apply"}
    )
    unsafe_network = _top10_candidate(
        "unsafe_network",
        baseline_kpi,
        _top10_kpi(target=50, neighbor=240, global_delay=55),
    )
    unsafe_network["proposal"].update(
        {"agent": "network_optimizer", "action": "set_corridor_speed"}
    )

    parents = _feedback_parent_candidates(
        [unsafe_network, routing_second, signal, routing_best],
        profile="delay",
        objective_scope="top10",
        beam_width=2,
    )

    assert [parent["proposal"]["proposal_id"] for parent in parents] == [
        "routing_best",
        "signal",
    ]


def test_feedback_beam_does_not_trade_feasibility_for_role_diversity():
    baseline_kpi = _top10_kpi(target=100, neighbor=200, global_delay=50)
    feasible = []
    for proposal_id, target in (("routing_a", 70), ("routing_b", 75)):
        candidate = _top10_candidate(
            proposal_id,
            baseline_kpi,
            _top10_kpi(target=target, neighbor=195, global_delay=48),
        )
        candidate["proposal"].update(
            {"agent": "demand_routing_optimizer", "action": "reroute_flows"}
        )
        feasible.append(candidate)
    unsafe_signal = _top10_candidate(
        "unsafe_signal",
        baseline_kpi,
        _top10_kpi(target=50, neighbor=250, global_delay=60),
    )
    unsafe_signal["proposal"].update(
        {"agent": "signal_optimizer", "action": "tls_optimize_and_apply"}
    )

    parents = _feedback_parent_candidates(
        [unsafe_signal, *feasible],
        profile="delay",
        objective_scope="top10",
        beam_width=2,
    )

    assert [parent["proposal"]["proposal_id"] for parent in parents] == [
        "routing_a",
        "routing_b",
    ]


def test_feedback_search_cli_is_backward_compatible_and_configurable():
    default_args = optimization_only.build_parser().parse_args(["--cfg", "sim.sumocfg"])
    feedback_args = optimization_only.build_parser().parse_args(
        [
            "--cfg",
            "sim.sumocfg",
            "--search-rounds",
            "2",
            "--feedback-beam-width",
            "3",
        ]
    )
    hybrid_args = optimization_only.build_parser().parse_args(
        [
            "--cfg",
            "sim.sumocfg",
            "--proposal-source",
            "random_initial_llm",
            "--search-rounds",
            "2",
            "--sumo-budget",
            "6",
        ]
    )

    assert default_args.search_rounds == 1
    assert default_args.feedback_beam_width == 2
    assert feedback_args.search_rounds == 2
    assert feedback_args.feedback_beam_width == 3
    assert _resolved_proposal_attempt_budget(hybrid_args) == 3


def test_automatic_attempt_budget_uses_full_budget_for_one_shot_baselines():
    args = optimization_only.build_parser().parse_args(
        [
            "--cfg",
            "sim.sumocfg",
            "--proposal-source",
            "grounded_random",
            "--search-rounds",
            "2",
            "--sumo-budget",
            "6",
        ]
    )

    assert _resolved_proposal_attempt_budget(args) == 6


def test_best_so_far_trace_records_incumbent_by_sumo_budget():
    baseline = {
        "status": "ok",
        "is_baseline": True,
        "kpi_score": 0.2,
        "proposal": {"proposal_id": "baseline_noop", "action": "no_op"},
    }
    worse = {
        "status": "ok",
        "sumo_evaluated": True,
        "kpi_score": 0.21,
        "proposal": {"proposal_id": "worse", "action": "reroute_flows"},
    }
    better = {
        "status": "ok",
        "sumo_evaluated": True,
        "kpi_score": 0.18,
        "proposal": {"proposal_id": "better", "action": "set_corridor_speed"},
    }

    trace = _best_so_far_trace(
        [baseline, worse, better],
        profile=None,
        objective_scope="global",
    )

    assert [row["incumbent_proposal_id"] for row in trace] == [
        "baseline_noop",
        "better",
    ]
    assert [row["sumo_evaluation"] for row in trace] == [1, 2]


def test_controlled_routing_variants_share_model_output_and_fixed_fraction():
    proposals = [
        {
            "proposal_id": "demand_routing_optimizer_proposal",
            "agent": "demand_routing_optimizer",
            "action": "reroute_flows",
            "args": {
                "net_path": "net.xml",
                "route_path": "routes.xml",
                "cfg_path": "sim.sumocfg",
                "avoid_edges": ["e1", "e2", "e3"],
                "reroute_fraction": 0.75,
            },
            "validation": {"ok": True},
        }
    ]

    variants, audit = _expand_routing_variants(
        proposals,
        [1, 3],
        0.5,
        {"net": "net.xml", "routes": "routes.xml", "cfg": "sim.sumocfg"},
    )

    assert [proposal["proposal_id"] for proposal in variants] == [
        "demand_routing_optimizer_proposal__edges_1",
        "demand_routing_optimizer_proposal__edges_3",
    ]
    assert [proposal["args"]["avoid_edges"] for proposal in variants] == [
        ["e1"],
        ["e1", "e2", "e3"],
    ]
    assert [proposal["args"]["reroute_fraction"] for proposal in variants] == [0.5, 0.5]
    assert all(item["status"] == "ready" for item in audit)


def test_routing_edge_count_parser_rejects_values_outside_one_to_three():
    assert _parse_routing_edge_counts("1,3") == [1, 3]
    assert _parse_routing_edge_counts("3,1,3") == [3, 1]
    with pytest.raises(Exception, match="selected from 1, 2, 3"):
        _parse_routing_edge_counts("1,4")


def test_bundle_action_rebinds_stale_paths_to_current_artifacts(tmp_path):
    args = _bind_bundle_action_args(
        "reroute_flows",
        {
            "net_path": "baseline.net.xml",
            "route_path": "baseline.rou.xml",
            "cfg_path": "baseline.sumocfg",
            "avoid_edges": ["e1"],
        },
        {
            "net": "modified.net.xml",
            "routes": "modified.rou.xml",
            "cfg": "modified.sumocfg",
        },
        tmp_path / "step",
    )

    assert args["net_path"] == "modified.net.xml"
    assert args["route_path"] == "modified.rou.xml"
    assert args["cfg_path"] == "modified.sumocfg"
    assert args["out_dir"] == str((tmp_path / "step").resolve())


def test_corridor_action_rebinds_network_artifacts(tmp_path):
    args = _bind_bundle_action_args(
        "set_corridor_speed",
        {"net_path": "stale.net.xml", "edge_id": "e1", "speed_factor": 0.8},
        {
            "net": "current.net.xml",
            "routes": "current.rou.xml",
            "cfg": "current.sumocfg",
        },
        tmp_path / "corridor",
    )

    assert args["net_path"] == "current.net.xml"
    assert args["route_path"] == "current.rou.xml"
    assert args["cfg_path"] == "current.sumocfg"


def test_bundle_route_validation_preserves_original_demand():
    aggregated = _aggregate_route_validation(
        [
            {
                "route_validation": {
                    "available": True,
                    "before_demand_units": 100,
                    "after_demand_units": 98,
                    "minimum_retention": 0.95,
                }
            },
            {
                "route_validation": {
                    "available": True,
                    "before_demand_units": 98,
                    "after_demand_units": 97,
                    "minimum_retention": 0.98,
                }
            },
        ]
    )

    assert aggregated["before_demand_units"] == 100
    assert aggregated["after_demand_units"] == 97
    assert aggregated["minimum_retention"] == 0.98


def test_bundle_evaluation_revalidates_against_previous_step_artifacts(tmp_path, monkeypatch):
    validations = []
    tool_calls = []

    def fake_validate(action, args, artifacts):
        validations.append((action, dict(args), dict(artifacts)))
        return {"ok": True, "action": action}

    def fake_tool(_bridge, server, action, args):
        tool_calls.append((server, action, dict(args)))
        if server == "runner":
            return {"status": "ok"}
        if action == "remove_edge":
            return {
                "status": "ok",
                "artifacts": {
                    "net": "after_remove.net.xml",
                    "routes": "after_remove.rou.xml",
                    "cfg": "after_remove.sumocfg",
                },
            }
        return {
            "status": "ok",
            "artifacts": {"cfg": "after_tls.sumocfg"},
        }

    monkeypatch.setattr(optimization_only, "validate_modifier_capability", fake_validate)
    monkeypatch.setattr(optimization_only, "_tool", fake_tool)
    monkeypatch.setattr(
        optimization_only,
        "collect_kpi",
        lambda _bridge, _artifacts, _top_n: {
            "mean_speed": 11,
            "mean_delay": 9,
            "throughput": 1,
            "co2": 90,
            "completed_trips": 10,
            "departed_trips": 10,
            "unfinished_trips": 0,
            "diagnosis": {},
        },
    )
    bundle = {
        "bundle_id": "bundle_test",
        "actions": [
            {
                "action": "remove_edge",
                "args": {"edge_id": "e1"},
                "source_proposal_id": "network",
            },
            {
                "action": "tls_optimize_and_apply",
                "args": {"tl_id": "tls1"},
                "source_proposal_id": "signal",
            },
        ],
    }
    baseline_artifacts = {
        "net": "baseline.net.xml",
        "routes": "baseline.rou.xml",
        "cfg": "baseline.sumocfg",
        "tripinfo": "baseline.tripinfo.xml",
        "edgedata": "baseline.edgedata.xml",
    }
    baseline_kpi = {
        "mean_speed": 10,
        "mean_delay": 10,
        "throughput": 1,
        "co2": 100,
        "completed_trips": 10,
        "departed_trips": 10,
    }

    result = optimization_only._evaluate_bundle(
        object(),
        bundle,
        tmp_path / "bundle",
        baseline_artifacts,
        baseline_kpi,
        {"delay": 0.5, "speed": 0.2, "throughput": 0.2, "co2": 0.1},
        5,
        {"network": 0.1, "signal": 0.1},
    )

    assert result["status"] == "ok"
    assert validations[0][2]["net"] == "baseline.net.xml"
    assert validations[1][1]["cfg_path"] == "after_remove.sumocfg"
    assert validations[1][2]["net"] == "after_remove.net.xml"
    assert tool_calls[-1] == ("runner", "run", {"cfg_path": "after_tls.sumocfg", "gui": False})
