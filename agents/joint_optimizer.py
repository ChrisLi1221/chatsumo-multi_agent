"""Conditional, one-component refinement of measured two-action plans."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional

from agents.bundle_optimizer import check_bundle_compatibility
from agents.debate_optimizer import (
    _artifact_dict,
    _semantic_args,
    build_finite_mutation_catalog,
    run_feedback_refinement,
)


class JointCatalogExhausted(ValueError):
    """No component has an unseen compatible local mutation."""


def plan_signature(proposal: Mapping[str, Any]) -> str:
    """Ignore paths and provenance, but preserve every ordered joint action."""
    payload: Dict[str, Any] = {
        "action": proposal.get("action"),
        "args": _semantic_args(proposal.get("args") or {}),
    }
    if proposal.get("action") == "bundle":
        payload["actions"] = [
            {"action": row.get("action"), "args": _semantic_args(row.get("args") or {})}
            for row in proposal.get("actions") or []
        ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _action_specs(components: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "tool": part.get("tool") or "provider_sumo_modifier",
            "action": part["action"],
            "args": deepcopy(part.get("args") or {}),
            "source_proposal_id": part["proposal_id"],
        }
        for part in components
    ]


def run_joint_feedback_refinement(
    state: Any,
    *,
    parent_plan: Mapping[str, Any],
    simulation_feedback: Mapping[str, Any],
    diagnosis: Optional[Mapping[str, Any]] = None,
    evaluated_history: Optional[List[Mapping[str, Any]]] = None,
    excluded_plan_signatures: Optional[set[str]] = None,
    component_feedback: Optional[Mapping[str, Any]] = None,
    interaction_feedback: Optional[Mapping[str, Any]] = None,
    user_request: str = "",
    objective_profile: Optional[str] = None,
    search_round: int = 2,
    mutation_index: int = 1,
    component_start: int = 0,
    selector_kind: str = "llm",
    max_completion_tokens: int = 4096,
    call_llm_fn,
) -> Dict[str, Any]:
    """Hold one component fixed and let the other's specialist select a mutation.

    Component ownership rotates deterministically. Exhausted components are
    skipped without a model call. Deduplication is conditional on the *whole*
    plan: an action tested alone remains eligible with a different partner.
    Catalog validation uses baseline artifacts; execution must also validate
    each action against the artifacts produced by its predecessors.
    """
    parent = deepcopy(dict(parent_plan))
    components = parent.get("proposals") or []
    if parent.get("action") != "bundle" or len(components) != 2:
        raise ValueError("Joint refinement requires a two-component bundle")
    reconstructed = {"action": "bundle", "actions": _action_specs(components)}
    if plan_signature(parent) != plan_signature(reconstructed):
        raise ValueError("Bundle components do not match the executed action list")

    seen = {plan_signature(row) for row in evaluated_history or []}
    seen.update(excluded_plan_signatures or set())
    seen.add(plan_signature(parent))
    artifacts = _artifact_dict(state)
    catalog = []
    component_index = component_start % len(components)
    for offset in range(len(components)):
        component_index = (component_start + offset) % len(components)
        part = components[component_index]
        # Never exclude a component just because it was simulated in isolation.
        mutations = build_finite_mutation_catalog(
            part,
            diagnosis=diagnosis,
            artifacts=artifacts,
            simulation_feedback=simulation_feedback,
            evaluated_history=[],
        )
        for mutation in mutations:
            trial = deepcopy(components)
            trial[component_index]["args"] = dict(mutation["full_args"])
            trial_plan = {"action": "bundle", "actions": _action_specs(trial)}
            if plan_signature(trial_plan) in seen:
                continue
            if check_bundle_compatibility(trial)["ok"]:
                catalog.append(mutation)
        if catalog:
            break
    if not catalog:
        raise JointCatalogExhausted("All joint components lack unseen compatible mutations")

    part = components[component_index]
    context = {
        "parent_plan_id": parent["proposal_id"],
        "selected_component_index": component_index,
        "selected_component_id": part["proposal_id"],
        "selected_specialist": part["agent"],
        "components": [
            {
                "proposal_id": row["proposal_id"],
                "agent": row.get("agent"),
                "action": row["action"],
                "args": _semantic_args(row.get("args") or {}),
                "fixed": index != component_index,
                "isolated_feedback": (component_feedback or {}).get(row["proposal_id"]),
            }
            for index, row in enumerate(components)
        ],
        "feedback_scope": "joint_plan",
        "rebuild_from": "baseline",
    }
    if interaction_feedback is not None:
        if interaction_feedback.get("parent_plan_id") != parent["proposal_id"]:
            raise AssertionError("Interaction evidence belongs to another parent")
        context["component_interactions"] = deepcopy(dict(interaction_feedback))
    refined_part = run_feedback_refinement(
        state,
        parent_proposal=part,
        simulation_feedback=simulation_feedback,
        diagnosis=diagnosis,
        evaluated_history=evaluated_history,
        user_request=user_request,
        objective_profile=objective_profile,
        search_round=search_round,
        mutation_index=mutation_index,
        joint_context=context,
        catalog_override=catalog,
        max_completion_tokens=max_completion_tokens,
        call_llm_fn=call_llm_fn,
    )
    identifier = hashlib.sha256(
        f"{parent['proposal_id']}:{search_round}:{mutation_index}".encode()
    ).hexdigest()[:20]
    # Bounded IDs avoid growing directory names as a lineage is refined again.
    plan_id = f"joint__{identifier}__r{search_round:02d}_m{mutation_index:02d}"
    refined_part["proposal_id"] = f"{plan_id}__component_{component_index}"
    refined_part["proposal_source"] = f"joint_feedback_{selector_kind}"
    refined_part["refinement"]["selector_kind"] = selector_kind
    refined_part["refinement"]["operator"] = f"finite_catalog_joint_{selector_kind}_selection"
    components[component_index] = refined_part
    compatibility = check_bundle_compatibility(components)
    component_validation = dict(refined_part.get("validation") or {})
    return {
        "proposal_id": plan_id,
        "bundle_id": plan_id,
        "agent": "bundle_optimizer",
        "action": "bundle",
        "bundle_size": len(components),
        "proposals": components,
        "actions": _action_specs(components),
        "compatibility": compatibility,
        "validation": {
            "ok": bool(component_validation.get("ok")) and compatibility["ok"],
            "component_validation": component_validation,
            "compatibility": compatibility,
            "message": "Joint refinement requires a valid component and compatible fixed actions.",
        },
        "score": sum(float(row.get("score") or 0) for row in components) / len(components),
        "risk": [str(row.get("risk") or "") for row in components],
        "proposal_source": f"joint_feedback_{selector_kind}",
        "parent_proposal_id": parent["proposal_id"],
        "joint_root_id": parent.get("joint_root_id") or parent["proposal_id"],
        "search_round": search_round,
        "refinement": {
            **refined_part["refinement"],
            "feedback_scope": "joint_plan",
            "selected_component_index": component_index,
            "selected_component_id": part["proposal_id"],
            "selected_specialist": part["agent"],
            "fixed_component_ids": [
                row["proposal_id"] for index, row in enumerate(components)
                if index != component_index
            ],
            "component_schedule": "round_robin_skip_exhausted",
            "mutation_catalog": [
                {"mutation_id": row["mutation_id"], "action": row["action"],
                 "args": row["args"], "changed_field": row["changed_field"]}
                for row in catalog
            ],
            "joint_context": context,
        },
    }
