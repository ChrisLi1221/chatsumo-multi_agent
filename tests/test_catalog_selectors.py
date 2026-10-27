"""Tests for the matched non-LLM catalog selectors.

The experimental claim these support is narrow and easy to break: the ablation
conditions must differ from the LLM condition *only* in which catalog entry is
chosen.  These tests pin that property down.
"""

import json

import pytest

import agents.debate_optimizer as debate_optimizer
from agents.catalog_selectors import (
    CATALOG_FIRST,
    CATALOG_RANDOM,
    make_catalog_selector,
)
from agents.debate_optimizer import run_feedback_refinement


PARENT = {
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


class _State:
    artifacts = {
        "net": "network.net.xml",
        "routes": "routes.rou.xml",
        "cfg": "sim.sumocfg",
    }


@pytest.fixture
def permissive_validator(monkeypatch):
    monkeypatch.setattr(
        debate_optimizer,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )


def _refine(selector):
    return run_feedback_refinement(
        _State(),
        parent_proposal=PARENT,
        simulation_feedback={"failed_constraints": ["top10_improvement"]},
        search_round=2,
        call_llm_fn=selector,
    )


def test_catalog_first_selects_the_enumerators_highest_priority_entry(
    permissive_validator,
):
    refined = _refine(make_catalog_selector(CATALOG_FIRST))

    assert refined["validation"]["ok"] is True
    assert refined["refinement"]["selected_mutation_id"] == "mutation_001"
    assert refined["refinement"]["changed_fields"] == ["speed_factor"]


def test_catalog_random_selects_a_valid_catalog_entry(permissive_validator):
    refined = _refine(make_catalog_selector(CATALOG_RANDOM, seed=42))

    assert refined["validation"]["ok"] is True
    assert (
        refined["refinement"]["selected_mutation_id"]
        in refined["refinement"]["mutation_catalog_ids"]
    )
    assert refined["refinement"]["changed_fields"] == ["speed_factor"]


def test_catalog_random_is_reproducible_under_a_fixed_seed(permissive_validator):
    first = _refine(make_catalog_selector(CATALOG_RANDOM, seed=7))
    second = _refine(make_catalog_selector(CATALOG_RANDOM, seed=7))

    assert (
        first["refinement"]["selected_mutation_id"]
        == second["refinement"]["selected_mutation_id"]
    )


def test_selectors_see_the_same_catalog_as_the_llm_would(permissive_validator):
    """The ablation is only valid if the catalog is identical across conditions."""
    seen = {}

    def recording_llm(**kwargs):
        catalog = json.loads(kwargs["user"])["mutation_catalog"]
        seen["llm"] = [row["mutation_id"] for row in catalog]
        return {"mutation_id": catalog[-1]["mutation_id"]}

    llm_refined = _refine(recording_llm)
    first_refined = _refine(make_catalog_selector(CATALOG_FIRST))

    assert seen["llm"] == first_refined["refinement"]["mutation_catalog_ids"]
    assert llm_refined["refinement"]["mutation_catalog_size"] == first_refined[
        "refinement"
    ]["mutation_catalog_size"]
    # Same catalog, different pick: that difference is the whole experiment.
    assert (
        llm_refined["refinement"]["selected_mutation_id"]
        != first_refined["refinement"]["selected_mutation_id"]
    )


def test_selectors_never_consume_a_repair_attempt(permissive_validator):
    """A malformed-output asymmetry against the LLM must not be introduced here."""
    for kind in (CATALOG_FIRST, CATALOG_RANDOM):
        refined = _refine(make_catalog_selector(kind, seed=1))
        assert refined["refinement"]["selection_attempts"] == 1
        assert refined["refinement"]["validation_errors"] == []


def test_selectors_accept_the_full_call_llm_keyword_signature(permissive_validator):
    """They are drop-in replacements, so the real call signature must work."""
    payload = json.dumps(
        {"mutation_catalog": [{"mutation_id": "mutation_001", "hint": "h"}]}
    )
    for kind in (CATALOG_FIRST, CATALOG_RANDOM):
        response = make_catalog_selector(kind, seed=3)(
            system="system prompt",
            user=payload,
            force_json=True,
            state=_State(),
            usage_scope="feedback:network_optimizer:round_02",
            max_tokens=500,
        )
        assert response["selected_mutation_id"] == "mutation_001"
        assert response["selector_kind"] == kind


def test_empty_catalog_yields_an_explicit_no_selection(permissive_validator):
    for kind in (CATALOG_FIRST, CATALOG_RANDOM):
        response = make_catalog_selector(kind)(
            system="s", user=json.dumps({"mutation_catalog": []})
        )
        assert response["selected_mutation_id"] == ""
        assert response["mutation_catalog_size"] == 0


def test_unknown_selector_kind_is_rejected():
    with pytest.raises(ValueError, match="Unknown catalog selector"):
        make_catalog_selector("catalog_oracle")


@pytest.mark.parametrize('budget', [None, 500, 2048])
def test_feedback_budget_is_explicit_and_reproducible(permissive_validator, budget):
    seen=[]
    def choose(**kwargs):
        seen.append(kwargs['max_tokens'])
        return {'mutation_id':json.loads(kwargs['user'])['mutation_catalog'][0]['mutation_id']}
    options={} if budget is None else {'max_completion_tokens':budget}
    result=run_feedback_refinement(_State(),parent_proposal=PARENT,simulation_feedback={},
                                   call_llm_fn=choose,**options)
    expected=4096 if budget is None else budget
    assert seen==[expected]
    assert result['refinement']['max_completion_tokens']==expected


def test_ablation_conditions_share_identical_grounded_initialization(monkeypatch):
    """The comparison is only paired if all three conditions start identically.

    Guards the ``init_source`` substitution in ``_generate_proposal_source``:
    the catalog conditions must request the same initializer as
    ``random_initial_llm`` so the first actions, and therefore the retained
    parents, match case by case.
    """
    import argparse

    import agents.grounded_baselines as grounded_baselines
    import experiment.run_optimization_only as runner

    monkeypatch.setattr(
        grounded_baselines,
        "validate_modifier_capability",
        lambda *args, **kwargs: {"ok": True, "action": args[0]},
    )

    rows = [
        {
            "edge_id": f"e{i}",
            "congestion_score": 1.0 - i / 10,
            "from_junction": f"j{i - 1}",
            "to_junction": f"j{i}",
        }
        for i in range(1, 6)
    ]
    diagnosis = {
        "top_congested_edges": rows,
        "bottlenecks": rows,
        "bottleneck_subgraph": {"tls_ids": ["j2", "j4"], "edges": rows},
    }
    artifacts = {"net": "n.net.xml", "routes": "r.rou.xml", "cfg": "s.sumocfg"}
    case = runner.ExperimentCase(case_id="c", cfg="s.sumocfg", seed=42)

    def initialization(source):
        args = argparse.Namespace(
            proposal_source=source,
            search_rounds=2,
            proposal_attempt_budget=None,
            sumo_budget=7,
            optimizer="all",
            proposal_seed=None,
            grid_edge_pool_size=3,
        )
        generated = runner._generate_proposal_source(
            None, diagnosis, artifacts, case, "delay", args
        )
        return generated, [
            (row.get("action"), row.get("args"))
            for row in generated.get("proposals") or []
        ]

    llm_generated, llm_actions = initialization("random_initial_llm")
    first_generated, first_actions = initialization("random_initial_catalog_first")
    random_generated, random_actions = initialization("random_initial_catalog_random")

    assert llm_actions == first_actions == random_actions
    assert llm_actions  # the fixture must actually produce proposals

    # Provenance still distinguishes the conditions.
    assert first_generated["generation"]["proposal_source"] == (
        "random_initial_catalog_first"
    )
    assert first_generated["generation"]["initialization_source"] == (
        "random_initial_llm"
    )
    assert first_generated["generation"]["feedback_selector"] == CATALOG_FIRST
    assert random_generated["generation"]["feedback_selector"] == CATALOG_RANDOM
    assert llm_generated["generation"]["feedback_selector"] == "llm"

    # Only the LLM condition may spend model calls on refinement.
    assert llm_generated["generation"]["feedback_llm_calls_enabled"] is True
    for generated in (first_generated, random_generated):
        assert generated["generation"]["feedback_llm_calls_enabled"] is False
        assert generated["generation"]["feedback_rounds_enabled"] is True
