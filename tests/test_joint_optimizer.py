from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import agents.debate_optimizer as debate
import agents.joint_optimizer as joint
from agents.bundle_optimizer import generate_pair_bundles
from agents.catalog_selectors import make_catalog_selector
import experiment.run_optimization_only as runner


def _components():
    return [
        {
            "proposal_id": "speed", "agent": "network_optimizer",
            "action": "set_corridor_speed",
            "args": {"edge_id": "e1", "edge_count": 5, "speed_factor": 0.8},
            "validation": {"ok": True}, "score": 1,
        },
        {
            "proposal_id": "routing", "agent": "demand_routing_optimizer",
            "action": "reroute_flows",
            "args": {"avoid_edges": ["e2"], "reroute_fraction": 0.5, "min_route_retention": 1.0},
            "validation": {"ok": True}, "score": 1,
        },
    ]


@pytest.fixture
def parent(monkeypatch):
    monkeypatch.setattr(debate, "validate_modifier_capability", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(runner, "validate_modifier_capability", lambda *a, **kw: {"ok": True})
    return generate_pair_bundles(_components(), top_k=2, max_bundles=1)[0]


def _refine(parent, **kwargs):
    return joint.run_joint_feedback_refinement(
        SimpleNamespace(artifacts={"net": "baseline.net.xml", "routes": "baseline.rou.xml", "cfg": "baseline.sumocfg"}),
        parent_plan=parent,
        simulation_feedback={"failed_constraints": ["global_delay"], "measured_kpi": {"mean_delay": 52}},
        call_llm_fn=kwargs.pop("call_llm_fn", make_catalog_selector("catalog_first")),
        **kwargs,
    )


def test_specialist_sees_joint_outcome_and_only_changes_its_own_component(parent):
    original = deepcopy(parent)
    calls = []

    def choose(**kwargs):
        payload = json.loads(kwargs["user"])
        calls.append(payload)
        assert "ENTIRE joint plan" in kwargs["system"]
        return {"mutation_id": payload["mutation_catalog"][0]["mutation_id"]}

    child = _refine(parent, call_llm_fn=choose, component_feedback={"routing": {"measured_kpi": {"mean_delay": 49}}})
    assert parent == original
    assert len(calls) == 1
    assert calls[0]["role"]["agent"] == "network_optimizer"
    assert calls[0]["simulation_feedback"]["measured_kpi"]["mean_delay"] == 52
    context = calls[0]["joint_context"]
    assert context["components"][1]["fixed"] is True
    assert context["components"][1]["isolated_feedback"]["measured_kpi"]["mean_delay"] == 49
    assert child["actions"][1] == parent["actions"][1]
    assert child["proposals"][1] == parent["proposals"][1]
    assert child["proposals"][0]["args"]["speed_factor"] == 0.9
    assert child["validation"]["ok"]
    assert child["refinement"]["changed_fields"] == ["speed_factor"]
    assert child["parent_proposal_id"] == parent["proposal_id"]


def test_joint_child_can_be_refined_by_the_other_specialist(parent):
    child = _refine(parent, search_round=2)
    grandchild = _refine(child, component_start=1, search_round=3)
    assert grandchild["refinement"]["selected_specialist"] == "demand_routing_optimizer"
    assert grandchild["actions"][0] == child["actions"][0]
    assert grandchild["actions"][1]["args"]["reroute_fraction"] == 0.25
    assert grandchild["joint_root_id"] == parent["proposal_id"]
    assert grandchild["parent_proposal_id"] == child["proposal_id"]
    assert len(grandchild["proposal_id"]) < 100


def test_joint_refinement_forwards_explicit_output_budget(parent):
    seen=[]
    def choose(**kwargs):
        seen.append(kwargs['max_tokens'])
        return {'mutation_id':json.loads(kwargs['user'])['mutation_catalog'][0]['mutation_id']}
    _refine(parent,call_llm_fn=choose,max_completion_tokens=2048)
    assert seen==[2048]


def test_interaction_evidence_changes_prompt_but_not_parent_or_catalog(parent):
    calls=[]
    def choose(**kwargs):
        calls.append(kwargs)
        return {'mutation_id':json.loads(kwargs['user'])['mutation_catalog'][0]['mutation_id']}
    legacy=_refine(parent,call_llm_fn=choose)
    evidence={'parent_plan_id':parent['proposal_id'],'metrics':{'observed':'test'}}
    enhanced=_refine(parent,call_llm_fn=choose,interaction_feedback=evidence)
    old,new=[json.loads(c['user']) for c in calls]
    assert new['joint_context'].pop('component_interactions')==evidence
    assert old==new
    assert joint.plan_signature(legacy)==joint.plan_signature(enhanced)
    assert 'not causal blame' in calls[1]['system']
    assert 'component_interactions' not in calls[0]['system']
    with pytest.raises(AssertionError,match='another parent'):
        _refine(parent,interaction_feedback={'parent_plan_id':'wrong'})


def test_deduplication_is_over_complete_plans_not_isolated_components(parent):
    child = _refine(parent)
    same_action_alone = child["proposals"][0]
    repeated = _refine(parent, evaluated_history=[same_action_alone])
    assert joint.plan_signature(repeated) == joint.plan_signature(child)
    unseen = _refine(parent, evaluated_history=[child])
    assert joint.plan_signature(unseen) != joint.plan_signature(child)
    assert unseen["actions"][0]["args"]["speed_factor"] == 0.7
    after_failed_attempt = _refine(parent, excluded_plan_signatures={joint.plan_signature(child)})
    assert joint.plan_signature(after_failed_attempt) == joint.plan_signature(unseen)


def test_plan_signatures_ignore_paths_but_include_fixed_partner(parent):
    other = deepcopy(parent)
    other["actions"][0]["args"]["net_path"] = "different.net.xml"
    assert joint.plan_signature(other) == joint.plan_signature(parent)
    other["actions"][1]["args"]["reroute_fraction"] = 0.25
    assert joint.plan_signature(other) != joint.plan_signature(parent)
    history = runner._feedback_history(
        [{"status": "ok", "proposal": parent, "objective": {"delta": 0}}],
        profile=None, objective_scope="global",
    )
    assert joint.plan_signature(history[0]) == joint.plan_signature(parent)


def test_incompatible_mutations_are_removed_before_model_selection(parent):
    seen_catalog = []

    def choose(**kwargs):
        catalog = json.loads(kwargs["user"])["mutation_catalog"]
        seen_catalog.extend(catalog)
        return {"mutation_id": catalog[0]["mutation_id"]}

    _refine(parent, diagnosis={"top_congested_edges": [{"edge_id": "e2"}]}, call_llm_fn=choose)
    assert seen_catalog
    assert not any(row["args"].get("edge_id") == "e2" for row in seen_catalog)


def test_exhausted_component_yields_to_partner_without_extra_calls(parent):
    first = _refine(parent)
    second = _refine(parent, evaluated_history=[first])
    third = _refine(parent, evaluated_history=[first, second])
    assert third["refinement"]["selected_component_index"] == 1
    history = [first, second, third]
    history.append(_refine(parent, evaluated_history=history))
    history.append(_refine(parent, evaluated_history=history))
    with pytest.raises(joint.JointCatalogExhausted):
        _refine(parent, evaluated_history=history, call_llm_fn=lambda **kw: pytest.fail("Unexpected model call"))


def test_invalid_selection_cannot_become_a_valid_joint_plan(parent):
    child = _refine(parent, call_llm_fn=lambda **kw: {"mutation_id": "invented"})
    assert not child["validation"]["ok"]
    assert joint.plan_signature(child) == joint.plan_signature(parent)


def test_inconsistent_executed_actions_are_rejected(parent):
    parent["actions"][1]["args"]["reroute_fraction"] = 1.0
    with pytest.raises(ValueError, match="executed action list"):
        _refine(parent)


def test_catalog_selector_provenance_is_preserved_on_joint_plans(parent):
    child = _refine(parent, selector_kind="catalog_first")
    assert child["proposal_source"] == "joint_feedback_catalog_first"
    assert child["refinement"]["selector_kind"] == "catalog_first"
    assert child["proposals"][0]["refinement"]["operator"] == "finite_catalog_joint_catalog_first_selection"


@pytest.mark.parametrize("budget,rounds,expected_joint", [(3, 2, 0), (4, 2, 1), (5, 2, 2), (5, 0, 0)])
def test_runner_reuses_joint_parents_respects_budget_and_preserves_legacy_path(
    tmp_path, monkeypatch, parent, budget, rounds, expected_joint,
):
    artifacts = {"net": "baseline.net.xml", "routes": "baseline.rou.xml", "cfg": "baseline.sumocfg", "tripinfo": "tripinfo.xml"}
    baseline = {"mean_delay": 50, "mean_speed": 10, "throughput": 1, "co2": 100, "completed_trips": 10, "departed_trips": 10, "teleport_count": 0}
    monkeypatch.setattr(runner, "prepare_baseline_sandbox", lambda *a: artifacts)
    monkeypatch.setattr(runner, "_tool", lambda *a: {"status": "ok"})
    monkeypatch.setattr(runner, "collect_kpi", lambda *a: {**baseline, "diagnosis": {}})
    monkeypatch.setattr(runner, "add_bottleneck_subgraph", lambda *a: {})
    monkeypatch.setattr(runner, "_generate_proposal_source", lambda *a: {"proposals": _components()})
    monkeypatch.setattr(runner, "call_llm", lambda **kw: pytest.fail("Non-LLM condition called model"))
    evaluations = []

    def candidate(proposal, delay):
        current = {**baseline, "mean_delay": delay}
        objective = runner.objective_delta(baseline, current, baseline=baseline, weights=runner.get_weight_profile("delay"))
        return {
            "status": "ok", "sumo_evaluated": True, "proposal": deepcopy(proposal),
            "selected_action": {"action": proposal["action"]}, "modification": {},
            "optimized": {"kpi": current}, "objective": objective,
            "kpi_score": objective["current"]["value"],
        }

    def single(_bridge, proposal, *args):
        evaluations.append(deepcopy(proposal))
        return candidate(proposal, 48)

    def bundle(_bridge, proposal, directory, supplied_baseline, *args):
        assert supplied_baseline == artifacts
        evaluations.append(deepcopy(proposal))
        result = candidate(proposal, 45 - sum(p.get("proposal_source", "").startswith("joint_feedback") for p in evaluations))
        runner._write_json(directory / "06_comparison.json", {})
        return result

    monkeypatch.setattr(runner, "_evaluate_proposal", single)
    monkeypatch.setattr(runner, "_evaluate_bundle", bundle)
    args = runner.build_parser().parse_args([
        "--cfg", "sim.sumocfg", "--proposal-source", "random_initial_catalog_first",
        "--objective-scope", "global", "--search-rounds", "1",
        "--joint-feedback-rounds", str(rounds), "--feedback-beam-width", "1",
        "--max-bundle-evals", "1", "--bundle-top-k", "2", "--sumo-budget", str(budget),
    ])
    result = runner.run_case(object(), runner.ExperimentCase("case", "sim.sumocfg", "reduce delay", "delay", 42), tmp_path, args)
    assert result["sumo_evaluation_count"] == len(evaluations) == 3 + expected_joint
    assert result["feedback_search"]["evaluated_joint_refinement_count"] == expected_joint
    assert len(result["best_so_far_trace"]) == 3 + expected_joint
    saved = json.loads((tmp_path / "04_candidates.json").read_text())
    assert saved["single_candidate_count"] == saved["ready_single_candidate_count"] == 2
    assert saved["bundle_candidate_count"] == 1 + expected_joint
    if expected_joint:
        child = evaluations[3]
        assert child["parent_proposal_id"] == evaluations[2]["proposal_id"]
        assert child["actions"][1] == evaluations[2]["actions"][1]
        assert child["refinement"]["joint_context"]["components"][0]["isolated_feedback"]["measured_kpi"]["mean_delay"] == 48
        assert result["selected_proposal"]["proposal_id"] == evaluations[-1]["proposal_id"]
    if expected_joint == 2:
        assert evaluations[4]["parent_proposal_id"] == evaluations[3]["proposal_id"]
        assert evaluations[4]["refinement"]["selected_specialist"] == "demand_routing_optimizer"
        assert evaluations[4]["actions"][0] == evaluations[3]["actions"][0]
