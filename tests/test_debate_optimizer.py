import json

import agents.debate_optimizer as debate_optimizer
from agents.debate_optimizer import (
    DEBATER_ROLES,
    DEMAND_ROUTING_OPTIMIZER_SYSTEM_PROMPT,
    NETWORK_OPTIMIZER_SYSTEM_PROMPT,
    SIGNAL_OPTIMIZER_SYSTEM_PROMPT,
    _normalize_proposal,
    _score_proposal,
    build_finite_mutation_catalog,
    normalize_expected_effect,
    run_debate_optimizer,
    run_feedback_refinement,
)


def test_each_optimizer_has_a_distinct_specialist_prompt():
    prompts = [role["system_prompt"] for role in DEBATER_ROLES]

    assert prompts == [
        SIGNAL_OPTIMIZER_SYSTEM_PROMPT,
        NETWORK_OPTIMIZER_SYSTEM_PROMPT,
        DEMAND_ROUTING_OPTIMIZER_SYSTEM_PROMPT,
    ]
    assert len(set(prompts)) == 3
    assert "Compare all evidenced TLS candidates" in prompts[0]
    assert "exactly one evidenced anchor edge per proposal" in prompts[1]
    assert "set_corridor_speed" in prompts[1]
    assert "edge_count=5" in prompts[1]
    assert "between one and three distinct edge IDs in avoid_edges" in prompts[2]
    assert "unique reference-route vehicles using any target edge" in prompts[2]
    assert "Choose the number of avoid_edges adaptively from 1, 2, or 3" in prompts[2]
    assert "Choose reroute_fraction adaptively" in prompts[2]
    assert "Prefer three edges" not in prompts[2]
    assert "Never set min_split=0" in prompts[0]


def test_proposal_ids_are_unique_even_when_model_reuses_same_id():
    proposals = [
        _normalize_proposal(
            {"proposal_id": "1", "agent": "wrong_agent", "action": "reroute_flows"},
            role,
        )
        for role in DEBATER_ROLES
    ]

    ids = [proposal["proposal_id"] for proposal in proposals]
    assert ids == [
        "signal_optimizer_proposal",
        "network_optimizer_proposal",
        "demand_routing_optimizer_proposal",
    ]
    assert len(set(ids)) == len(ids)
    assert all(proposal["model_proposal_id"] == "1" for proposal in proposals)
    assert [proposal["agent"] for proposal in proposals] == [
        "signal_optimizer",
        "network_optimizer",
        "demand_routing_optimizer",
    ]


def test_canonical_model_id_is_not_duplicated_in_audit_field():
    role = DEBATER_ROLES[0]
    proposal = _normalize_proposal(
        {"proposal_id": "signal_optimizer_proposal", "action": "tls_optimize_and_apply"},
        role,
    )

    assert proposal["proposal_id"] == "signal_optimizer_proposal"
    assert "model_proposal_id" not in proposal


def test_tls_proposal_has_no_fixed_scoring_bias():
    validation = {"ok": True}
    base = {"evidence_refs": ["bottleneck"], "expected_effect": {}}

    tls_score = _score_proposal({**base, "action": "tls_optimize_and_apply"}, validation, "delay")
    routing_score = _score_proposal({**base, "action": "reroute_flows"}, validation, "delay")

    assert tls_score == routing_score == 2.0


def test_expected_effect_is_structured_and_synonyms_are_normalized():
    normalized = normalize_expected_effect(
        {
            "delay": "reduced",
            "throughput": "increased",
            "speed": "same",
            "emissions": "higher",
        }
    )

    assert normalized == {
        "mean_delay": "improve",
        "throughput": "improve",
        "mean_speed": "unchanged",
        "co2": "worsen",
    }
    assert _score_proposal(
        {"evidence_refs": ["bottleneck"], "expected_effect": normalized},
        {"ok": True},
        "delay",
    ) == 3.0


def test_single_optimizer_only_calls_requested_specialist_and_judge():
    calls = []

    class Artifacts:
        net = "network.net.xml"
        routes = "routes.rou.xml"
        cfg = "sim.sumocfg"

        def to_dict(self):
            return {"net": self.net, "routes": self.routes, "cfg": self.cfg}

    class State:
        artifacts = Artifacts()

    def fake_llm(**kwargs):
        calls.append(kwargs["usage_scope"])
        if kwargs["usage_scope"] == "debate:judge":
            return {"selected_proposal_id": "demand_routing_optimizer_proposal", "reason": "only valid proposal"}
        return {
            "proposal_id": "1",
            "action": "reroute_flows",
            "args": {
                "net_path": "hallucinated.net.xml",
                "route_path": "hallucinated.rou.xml",
                "cfg_path": "hallucinated.sumocfg",
                "avoid_edges": ["edge_a"],
                "reroute_fraction": 0.5,
            },
            "evidence_refs": ["bottleneck"],
        }

    result = run_debate_optimizer(
        State(),
        optimizer_agents=["demand_routing_optimizer"],
        call_llm_fn=fake_llm,
    )

    assert calls == ["debate:demand_routing_optimizer", "debate:judge"]
    assert result["active_optimizers"] == ["demand_routing_optimizer"]
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["args"]["net_path"] == "network.net.xml"
    assert result["proposals"][0]["args"]["route_path"] == "routes.rou.xml"
    assert result["proposals"][0]["args"]["cfg_path"] == "sim.sumocfg"


def test_feedback_refinement_uses_measured_result_and_changes_one_argument(monkeypatch):
    calls = []

    class Artifacts:
        net = "network.net.xml"
        routes = "routes.rou.xml"
        cfg = "sim.sumocfg"

        def to_dict(self):
            return {"net": self.net, "routes": self.routes, "cfg": self.cfg}

    class State:
        artifacts = Artifacts()

    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return {
            "proposal_id": "ignored",
            "action": "reroute_flows",
            "args": {
                "avoid_edges": ["edge_a"],
                "reroute_fraction": 0.25,
                "min_route_retention": 1.0,
            },
            "evidence_refs": ["measured global-delay increase"],
            "risk": "The detour may still transfer queues downstream.",
        }

    parent = {
        "proposal_id": "routing_parent",
        "agent": "demand_routing_optimizer",
        "action": "reroute_flows",
        "args": {
            "net_path": "network.net.xml",
            "route_path": "routes.rou.xml",
            "cfg_path": "sim.sumocfg",
            "avoid_edges": ["edge_a"],
            "reroute_fraction": 0.5,
            "min_route_retention": 1.0,
        },
    }
    feedback = {
        "failed_constraints": ["global_delay"],
        "scope_objective": {"global_delay_relative_change": 0.02},
    }
    refined = run_feedback_refinement(
        State(),
        parent_proposal=parent,
        simulation_feedback=feedback,
        evaluated_history=[{"proposal_id": "routing_parent"}],
        search_round=2,
        call_llm_fn=fake_llm,
    )

    assert calls[0]["usage_scope"] == "feedback:demand_routing_optimizer:round_02"
    payload = json.loads(calls[0]["user"])
    assert payload["simulation_feedback"] == feedback
    assert payload["parent_proposal"]["proposal_id"] == "routing_parent"
    assert payload["mutation_catalog"]
    assert refined["proposal_id"] == "routing_parent__refine_r02"
    assert refined["parent_proposal_id"] == "routing_parent"
    assert refined["refinement"]["changed_fields"] == ["reroute_fraction"]
    assert refined["refinement"]["operator"] == "finite_catalog_llm_selection"
    assert refined["validation"]["ok"] is True


def test_feedback_refinement_rejects_an_unchanged_candidate(monkeypatch):
    class State:
        artifacts = {
            "net": "network.net.xml",
            "routes": "routes.rou.xml",
            "cfg": "sim.sumocfg",
        }

    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )
    parent = {
        "proposal_id": "network_parent",
        "agent": "network_optimizer",
        "action": "set_corridor_speed",
        "args": {
            "net_path": "network.net.xml",
            "route_path": "routes.rou.xml",
            "cfg_path": "sim.sumocfg",
            "edge_id": "edge_a",
            "edge_count": 5,
            "speed_factor": 0.8,
        },
    }

    refined = run_feedback_refinement(
        State(),
        parent_proposal=parent,
        simulation_feedback={"failed_constraints": ["top10_improvement"]},
        search_round=2,
        call_llm_fn=lambda **kwargs: {
            "action": "set_corridor_speed",
            "args": {"edge_id": "edge_a", "edge_count": 5, "speed_factor": 0.8},
            "risk": "No safer local change was identified.",
        },
    )

    assert refined["validation"]["ok"] is False
    assert refined["validation"]["error_type"] == "INVALID_FEEDBACK_REFINEMENT"
    assert "repeats the parent" in refined["validation"]["refinement_errors"][0]


def test_second_feedback_mutation_has_a_unique_id_and_usage_scope(monkeypatch):
    calls = []

    class State:
        artifacts = {
            "net": "network.net.xml",
            "routes": "routes.rou.xml",
            "cfg": "sim.sumocfg",
        }

    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )
    parent = {
        "proposal_id": "routing_parent",
        "agent": "demand_routing_optimizer",
        "action": "reroute_flows",
        "args": {
            "net_path": "network.net.xml",
            "route_path": "routes.rou.xml",
            "cfg_path": "sim.sumocfg",
            "avoid_edges": ["edge_a"],
            "reroute_fraction": 0.5,
        },
    }

    refined = run_feedback_refinement(
        State(),
        parent_proposal=parent,
        simulation_feedback={"failed_constraints": ["global_delay"]},
        search_round=2,
        mutation_index=2,
        call_llm_fn=lambda **kwargs: calls.append(kwargs) or {
            "action": "reroute_flows",
            "args": {"avoid_edges": ["edge_a"], "reroute_fraction": 0.25},
            "risk": "A lower fraction may still transfer some congestion.",
        },
    )

    assert refined["proposal_id"] == "routing_parent__refine_r02_m02"
    assert refined["refinement"]["mutation_index"] == 2
    assert calls[0]["usage_scope"].endswith(":round_02:mutation_02")


def test_finite_mutation_catalog_excludes_parent_and_evaluated_history(monkeypatch):
    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )
    parent = {
        "action": "reroute_flows",
        "args": {
            "avoid_edges": ["edge_a"],
            "reroute_fraction": 0.5,
            "min_route_retention": 1.0,
        },
    }
    history = [
        {
            "action": "reroute_flows",
            "args": {
                "avoid_edges": ["edge_a"],
                "reroute_fraction": 0.25,
                "min_route_retention": 1.0,
            },
        }
    ]

    catalog = build_finite_mutation_catalog(
        parent,
        simulation_feedback={"failed_constraints": ["global_delay"]},
        evaluated_history=history,
    )
    fractions = [
        row["args"].get("reroute_fraction")
        for row in catalog
        if row["changed_field"] == "reroute_fraction"
    ]

    assert 0.5 not in fractions
    assert 0.25 not in fractions
    assert fractions == [0.75, 1.0]
    assert all(row["validation"]["ok"] for row in catalog)


def test_feedback_llm_selects_catalog_id_without_supplying_control_values(monkeypatch):
    calls = []

    class State:
        artifacts = {
            "net": "network.net.xml",
            "routes": "routes.rou.xml",
            "cfg": "sim.sumocfg",
        }

    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )
    parent = {
        "proposal_id": "speed_parent",
        "agent": "network_optimizer",
        "action": "set_corridor_speed",
        "args": {
            "net_path": "network.net.xml",
            "route_path": "routes.rou.xml",
            "cfg_path": "sim.sumocfg",
            "edge_id": "edge_a",
            "edge_count": 5,
            "speed_factor": 0.8,
        },
    }

    def fake_llm(**kwargs):
        calls.append(kwargs)
        catalog = json.loads(kwargs["user"])["mutation_catalog"]
        selected = next(row for row in catalog if row["changed_field"] == "speed_factor")
        return {
            "mutation_id": selected["mutation_id"],
            "claim": "Use the measured feedback to select a finite speed mutation.",
            "risk": "Queues may still move to an adjacent junction.",
        }

    refined = run_feedback_refinement(
        State(),
        parent_proposal=parent,
        simulation_feedback={"failed_constraints": ["top10_improvement"]},
        search_round=2,
        call_llm_fn=fake_llm,
    )

    assert len(calls) == 1
    assert refined["validation"]["ok"] is True
    assert refined["refinement"]["selected_mutation_id"].startswith("mutation_")
    assert refined["refinement"]["changed_fields"] == ["speed_factor"]
    assert refined["args"]["speed_factor"] in {0.7, 0.9}


def test_invalid_catalog_selection_gets_one_llm_repair_attempt(monkeypatch):
    calls = []

    class State:
        artifacts = {
            "net": "network.net.xml",
            "routes": "routes.rou.xml",
            "cfg": "sim.sumocfg",
        }

    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )
    parent = {
        "proposal_id": "routing_parent",
        "agent": "demand_routing_optimizer",
        "action": "reroute_flows",
        "args": {
            "net_path": "network.net.xml",
            "route_path": "routes.rou.xml",
            "cfg_path": "sim.sumocfg",
            "avoid_edges": ["edge_a"],
            "reroute_fraction": 0.5,
        },
    }

    def fake_llm(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"mutation_id": "invented_mutation"}
        catalog = json.loads(kwargs["user"])["mutation_catalog"]
        return {"mutation_id": catalog[0]["mutation_id"]}

    refined = run_feedback_refinement(
        State(),
        parent_proposal=parent,
        simulation_feedback={"failed_constraints": ["global_delay"]},
        search_round=2,
        call_llm_fn=fake_llm,
    )

    assert len(calls) == 2
    assert calls[1]["usage_scope"].endswith(":repair")
    assert refined["validation"]["ok"] is True
    assert refined["refinement"]["selection_attempts"] == 2
