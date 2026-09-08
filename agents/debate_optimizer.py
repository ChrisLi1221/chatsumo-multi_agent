from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from agents.capabilities import (
    KNOWN_UNSUPPORTED_CAPABILITIES,
    MODIFIER_CAPABILITIES,
    normalize_capability_args,
    validate_modifier_capability,
)
from agents.bundle_optimizer import generate_pair_bundles
from agents.grounded_baselines import (
    REROUTE_FRACTIONS,
    SPEED_FACTORS,
    build_legal_candidate_pool,
    diagnosed_tls_ids,
    objective_edge_rows,
)
from agents.objectives import get_weight_profile, infer_weight_profile


PROPOSAL_JSON_CONTRACT = """Return EXACTLY ONE JSON object:
{
  "proposal_id": "...",
  "agent": "...",
  "claim": "...",
  "capability": "...",
  "tool": "provider_sumo_modifier",
  "action": "...",
  "args": {...},
  "evidence_refs": ["..."],
  "expected_effect": {
    "mean_delay": "improve|unchanged|worsen|unknown",
    "throughput": "improve|unchanged|worsen|unknown",
    "mean_speed": "improve|unchanged|worsen|unknown",
    "co2": "improve|unchanged|worsen|unknown"
  },
  "risk": "..."
}
"""


EFFECT_KEYS = ("mean_delay", "throughput", "mean_speed", "co2")
LOWER_IS_BETTER_EFFECTS = {"mean_delay", "co2"}


def _canonical_effect(metric: str, value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"improve", "improved", "improvement", "better", "beneficial"}:
        return "improve"
    if text == "same_or_increase":
        return "improve" if metric not in LOWER_IS_BETTER_EFFECTS else "worsen"
    if text in {"unchanged", "same", "stable", "neutral"}:
        return "unchanged"
    if text in {"worsen", "worsened", "worse", "degrade", "degraded"}:
        return "worsen"
    decreasing = any(token in text for token in ("decrease", "decreased", "reduce", "reduced", "lower", "less"))
    increasing = any(token in text for token in ("increase", "increased", "raise", "raised", "higher", "more"))
    if decreasing:
        return "improve" if metric in LOWER_IS_BETTER_EFFECTS else "worsen"
    if increasing:
        return "worsen" if metric in LOWER_IS_BETTER_EFFECTS else "improve"
    return "unknown"


def normalize_expected_effect(value: Any) -> Dict[str, str]:
    raw = dict(value) if isinstance(value, Mapping) else {}
    aliases = {
        "delay": "mean_delay",
        "speed": "mean_speed",
        "emissions": "co2",
        "emission": "co2",
    }
    normalized_raw: Dict[str, Any] = {}
    for key, item in raw.items():
        normalized_raw[aliases.get(str(key), str(key))] = item
    return {metric: _canonical_effect(metric, normalized_raw.get(metric)) for metric in EFFECT_KEYS}


SIGNAL_OPTIMIZER_SYSTEM_PROMPT = f"""You are the signal-control specialist in a traffic optimization team.

{PROPOSAL_JSON_CONTRACT}

Your allowed actions are tls_optimize_and_apply, tls_apply_program, and tls_set_program.

Rules:
- Use only evidence supplied in the input.
- Use only a concrete TLS ID listed in diagnosis.bottleneck_subgraph.tls_ids.
- Compare all evidenced TLS candidates; do not automatically choose the first ID.
- Prefer a TLS whose controlled connections or phase edge groups directly contain or feed a diagnosed bottleneck.
- Do not invent TLS IDs, phases, movement groups, cycle lengths, or split values.
- Never use placeholders such as unknown, existing_tls, current_tls, or target_id.
- Prefer tls_optimize_and_apply when a verified TLS exists but explicit safe split values are not evidenced.
- For tls_optimize_and_apply, provide only tl_id by default. Omit cycle, min_split, offset, demands, and phase_edge_groups unless the supplied evidence explicitly supports them; omission uses deterministic safe defaults and route-derived demand.
- Numeric split parameters are fractions; when explicitly evidenced, min_split must be strictly positive and less than 0.5. Never set min_split=0.
- Preserve yellow/all-red states and identify risks from starving minor movements or transferring queues downstream.
- risk must be substantive and must never be "None".
- If no evidence-grounded TLS target exists, abstain with action="", capability="", args={{}}, and explain why in risk.
"""


NETWORK_OPTIMIZER_SYSTEM_PROMPT = f"""You are the road-network intervention specialist in a traffic optimization team.

{PROPOSAL_JSON_CONTRACT}

Your executable actions are set_corridor_speed, remove_edge, and make_road_one_way.

Rules:
- Use only evidence supplied in the input.
- Target only a concrete edge ID present in the diagnosis or bottleneck subgraph.
- Do not invent edge IDs and never use placeholders.
- set_corridor_speed is a static corridor-level speed-limit adjustment. It does not directly set vehicle speeds, change OD demand, or reroute vehicles.
- Prefer the reversible set_corridor_speed intervention. Use remove_edge only when evidence supports a closure.
- For set_corridor_speed, provide exactly one evidenced anchor edge_id, edge_count=5, and speed_factor chosen from 0.7, 0.8, or 0.9.
- The tool deterministically expands the anchor upstream and downstream along the same named road or segmented road ID; do not invent or enumerate adjacent edge IDs.
- speed_factor is applied proportionally to every lane speed limit in the resolved corridor, where 0.8 means 80% of each current lane speed limit.
- Do not assume that increasing speed reduces congestion. Consider lowering upstream speed to smooth arrivals and prevent queue spillback.
- Treat edge removal as a closure intervention, not as generic capacity improvement.
- Propose removal only when the evidence indicates an alternative local connection and route preservation is plausible.
- Use make_road_one_way only when evidence identifies the exact directed edge to keep. It removes the reverse edge and does not add lanes or double capacity.
- Consider blocked movements, OD disconnection, route loss, detour, and congestion transfer.
- Use exactly one evidenced anchor edge per proposal; the tool may resolve fewer than five edges if the same-road corridor ends.
- risk must be substantive and must never be "None".
- If no evidence-grounded safe speed-limit adjustment, one-way conversion, or closure exists, abstain with action="", capability="", args={{}}, and explain why in risk.
"""


DEMAND_ROUTING_OPTIMIZER_SYSTEM_PROMPT = f"""You are the demand-routing specialist in a traffic optimization team.

{PROPOSAL_JSON_CONTRACT}

Your executable action is reroute_flows. demand_scaling is currently unsupported.

Rules:
- Use only evidence supplied in the input.
- Target only concrete edge IDs present in the diagnosis or bottleneck subgraph.
- Do not invent edge IDs and never use placeholders.
- You MUST provide between one and three distinct edge IDs in avoid_edges.
- Choose the number of avoid_edges adaptively from 1, 2, or 3. Prefer one edge when one bottleneck dominates or when congestion-transfer risk is high; use two or three only when multiple independent bottlenecks have evidenced alternatives.
- All avoid_edges are jointly assigned a high routing cost in one rerouting action.
- You MUST choose reroute_fraction from exactly one of 0.25, 0.5, 0.75, or 1.0. This is the fraction of unique reference-route vehicles using any target edge that will receive new routes.
- Choose reroute_fraction adaptively. Use 0.25 or 0.5 when detour, downstream congestion, or global-delay risk is uncertain; use 0.75 or 1.0 only when the evidence shows strong alternative-route capacity and low transfer risk.
- Preserve every demand record and its exact edge-level OD. min_route_retention must be 1.0.
- Rerouting is soft avoidance: the target edges receive a high routing cost, but an unavoidable or endpoint edge may remain in a valid route.
- Do not claim that rerouting increases throughput unless evidence supports that claim.
- Consider route loss, excessive detour, new downstream congestion, and displaced queues.
- risk must be substantive and must never be "None".
- If no plausible alternative route is evidenced, abstain with action="", capability="", args={{}}, and explain why in risk.
"""


FEEDBACK_REFINEMENT_RULES = """You are refining one previously simulated traffic intervention.

The input contains the parent proposal, measured SUMO feedback, the constraint
assessment, and a compact history of candidates that have already been tested.
For this feedback call, the orchestrator supplies a finite mutation_catalog.
Return EXACTLY ONE JSON object:
{
  "mutation_id": "mutation_...",
  "claim": "why this measured one-step mutation is preferred",
  "evidence_refs": ["measured feedback used for the choice"],
  "expected_effect": {
    "mean_delay": "improve|unchanged|worsen|unknown",
    "throughput": "improve|unchanged|worsen|unknown",
    "mean_speed": "improve|unchanged|worsen|unknown",
    "co2": "improve|unchanged|worsen|unknown"
  },
  "risk": "substantive remaining risk"
}

Refinement rules:
- Select exactly one mutation_id that appears in mutation_catalog.
- Do not invent or return an edge ID, TLS ID, numeric value, action, or artifact path.
- The orchestrator applies the selected catalog entry and keeps all other parent arguments.
- Use the measured constraint failures to decide the direction of the change.
- If global delay or neighbor spillover failed, prefer a less aggressive control or a safer target.
- If the local objective did not improve but safety constraints passed, consider a stronger control or a better evidenced target.
- Every catalog entry is validator-confirmed, differs from the parent in exactly one
  semantic argument, and excludes evaluated_history.
"""


# Retained for external imports; optimization calls use the three specialist prompts above.
DEBATER_SYSTEM_PROMPT = PROPOSAL_JSON_CONTRACT


JUDGE_SYSTEM_PROMPT = """You are a feasibility judge for traffic optimization proposals.

Return EXACTLY ONE JSON object:
{
  "selected_proposal_id": "...",
  "reason": "...",
  "rejected": [{"proposal_id": "...", "reason": "..."}]
}

Rules:
- Select only proposals with validation.ok=true.
- Prefer proposals that match the objective profile and cite concrete evidence.
- If no proposal is executable, selected_proposal_id must be "".
"""


DEBATER_ROLES = [
    {
        "agent": "signal_optimizer",
        "focus": "traffic signal timing, cycle length, green split, and offset interventions",
        "preferred_capabilities": ["tls_optimize_and_apply", "tls_apply_program", "tls_set_program"],
        "system_prompt": SIGNAL_OPTIMIZER_SYSTEM_PROMPT,
    },
    {
        "agent": "network_optimizer",
        "focus": "static corridor-level speed-limit adjustment, closures, and one-way conversion",
        "preferred_capabilities": ["set_corridor_speed", "remove_edge", "make_road_one_way"],
        "system_prompt": NETWORK_OPTIMIZER_SYSTEM_PROMPT,
    },
    {
        "agent": "demand_routing_optimizer",
        "focus": "route guidance and rerouting around bottleneck edges while preserving demand",
        "preferred_capabilities": ["reroute_flows", "demand_scaling"],
        "system_prompt": DEMAND_ROUTING_OPTIMIZER_SYSTEM_PROMPT,
    },
]

OPTIMIZER_ALIASES = {
    "signal": "signal_optimizer",
    "network": "network_optimizer",
    "routing": "demand_routing_optimizer",
}


def _capability_view() -> Dict[str, Any]:
    return {
        "supported": MODIFIER_CAPABILITIES,
        "known_unsupported": KNOWN_UNSUPPORTED_CAPABILITIES,
    }


def _artifact_dict(state: Any) -> Dict[str, Any]:
    art = getattr(state, "artifacts", None)
    if art is None:
        return {}
    if hasattr(art, "to_dict"):
        return art.to_dict()
    if isinstance(art, dict):
        return dict(art)
    return {
        "net": getattr(art, "net", None),
        "routes": getattr(art, "routes", None),
        "cfg": getattr(art, "cfg", None),
        "tripinfo": getattr(art, "tripinfo", None),
    }


def _normalize_proposal(raw: Mapping[str, Any], role: Mapping[str, Any]) -> Dict[str, Any]:
    proposal = dict(raw or {})
    # Proposal identity is orchestration metadata, not model-generated content.
    # Force a stable role-scoped ID so duplicate LLM IDs cannot overwrite one
    # another when the judge selection is resolved through a dictionary.
    canonical_id = f"{role['agent']}_proposal"
    model_proposal_id = str(proposal.get("proposal_id") or "").strip()
    if model_proposal_id and model_proposal_id != canonical_id:
        proposal["model_proposal_id"] = model_proposal_id
    proposal["proposal_id"] = canonical_id
    proposal["agent"] = role["agent"]
    proposal.setdefault("tool", "provider_sumo_modifier")

    action = str(proposal.get("action") or proposal.get("capability") or "").strip()
    proposal["action"] = action
    proposal["capability"] = str(proposal.get("capability") or action)
    proposal["args"] = normalize_capability_args(action, proposal.get("args") or {})
    proposal.setdefault("evidence_refs", [])
    proposal["expected_effect"] = normalize_expected_effect(proposal.get("expected_effect"))
    proposal.setdefault("risk", "")
    return proposal


def _with_artifact_args(action: str, args: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Dict[str, Any]:
    """Replace model-supplied artifact paths with orchestrator-owned paths."""
    enriched = dict(args or {})
    if action in {"remove_edge", "set_edge_speed", "set_corridor_speed", "make_road_one_way"}:
        enriched["net_path"] = artifacts.get("net")
        enriched["route_path"] = artifacts.get("routes")
        enriched["cfg_path"] = artifacts.get("cfg")
    elif action == "reroute_flows":
        enriched["net_path"] = artifacts.get("net")
        enriched["route_path"] = artifacts.get("routes")
        enriched["cfg_path"] = artifacts.get("cfg")
    elif action in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
        enriched["cfg_path"] = artifacts.get("cfg")
        if action == "tls_optimize_and_apply":
            enriched["route_path"] = artifacts.get("routes")
    return normalize_capability_args(action, enriched)


_ORCHESTRATOR_ARG_KEYS = {"net_path", "route_path", "cfg_path", "out_dir"}
_REFINEMENT_MUTABLE_FIELDS = {
    "remove_edge": {"edge_id"},
    "make_road_one_way": {"keep_edge_id", "opposite_edge_id"},
    "set_edge_speed": {"edge_id", "speed"},
    "set_corridor_speed": {"edge_id", "edge_count", "speed_factor"},
    "reroute_flows": {"avoid_edges", "reroute_fraction"},
    "tls_apply_program": {"tl_id", "cycle", "splits", "offset"},
    "tls_set_program": {"tls_id", "program_xml"},
    "tls_optimize_and_apply": {"tl_id", "cycle", "min_split", "offset"},
}

TLS_CYCLE_CHOICES = (75.0, 90.0, 105.0)
TLS_MIN_SPLIT_CHOICES = (0.05, 0.08, 0.12)
MAX_FINITE_MUTATIONS = 24


def _semantic_args(args: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        str(key): value
        for key, value in dict(args or {}).items()
        if str(key) not in _ORCHESTRATOR_ARG_KEYS
    }


def _refinement_change_fields(
    parent: Mapping[str, Any],
    child: Mapping[str, Any],
) -> List[str]:
    parent_args = _semantic_args(parent.get("args") or {})
    child_args = _semantic_args(child.get("args") or {})
    return sorted(
        key
        for key in set(parent_args) | set(child_args)
        if parent_args.get(key) != child_args.get(key)
    )


def _semantic_signature(action: str, args: Mapping[str, Any]) -> str:
    return json.dumps(
        {"action": str(action or ""), "args": _semantic_args(args)},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _evaluated_semantic_signatures(
    evaluated_history: Optional[List[Mapping[str, Any]]],
) -> set[str]:
    return {
        _semantic_signature(str(row.get("action") or ""), row.get("args") or {})
        for row in (evaluated_history or [])
        if row.get("action")
    }


def build_finite_mutation_catalog(
    parent_proposal: Mapping[str, Any],
    *,
    diagnosis: Optional[Mapping[str, Any]] = None,
    artifacts: Optional[Mapping[str, Any]] = None,
    simulation_feedback: Optional[Mapping[str, Any]] = None,
    evaluated_history: Optional[List[Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Enumerate validator-confirmed, unseen, single-field local mutations."""
    parent = dict(parent_proposal or {})
    action = str(parent.get("action") or "")
    semantic_parent = _semantic_args(parent.get("args") or {})
    artifact_map = dict(artifacts or {})
    diagnosis_map = dict(diagnosis or {})
    excluded = _evaluated_semantic_signatures(evaluated_history)
    excluded.add(_semantic_signature(action, semantic_parent))
    candidates: List[Dict[str, Any]] = []
    seen = set(excluded)

    def add(field: str, value: Any, hint: str) -> None:
        if len(candidates) >= MAX_FINITE_MUTATIONS:
            return
        args = dict(semantic_parent)
        args[field] = value
        changed = _refinement_change_fields(parent, {"args": args})
        if changed != [field]:
            return
        full_args = _with_artifact_args(action, args, artifact_map)
        validation = validate_modifier_capability(action, full_args, artifact_map)
        signature = _semantic_signature(action, full_args)
        if not validation.get("ok") or signature in seen:
            return
        seen.add(signature)
        candidates.append(
            {
                "action": action,
                "changed_field": field,
                "args": _semantic_args(full_args),
                "full_args": full_args,
                "hint": hint,
                "validation": validation,
            }
        )

    failed_constraints = set(
        str(item)
        for item in (simulation_feedback or {}).get("failed_constraints") or []
    )
    unsafe = bool(
        failed_constraints & {"global_delay", "neighbor_spillover", "teleports", "throughput"}
    )

    if action == "tls_optimize_and_apply":
        current_cycle = float(semantic_parent.get("cycle", 90.0))
        cycle_choices = sorted(
            (value for value in TLS_CYCLE_CHOICES if value != current_cycle),
            key=lambda value: (abs(value - current_cycle), value),
        )
        for value in cycle_choices:
            add("cycle", value, f"Test validator-confirmed TLS cycle {value:g} s.")

        current_min_split = float(semantic_parent.get("min_split", 0.08))
        split_choices = sorted(
            (value for value in TLS_MIN_SPLIT_CHOICES if value != current_min_split),
            key=lambda value: (abs(value - current_min_split), value),
        )
        for value in split_choices:
            add("min_split", value, f"Test minimum green split {value:g}.")

        legal = build_legal_candidate_pool(
            diagnosis_map,
            artifact_map,
            optimizer="signal",
        ).get("signal_optimizer", [])
        for spec in legal:
            tls_id = (spec.get("args") or {}).get("tl_id")
            if tls_id not in (None, ""):
                add("tl_id", str(tls_id), f"Move the control to evidenced TLS {tls_id}.")

    elif action == "set_corridor_speed":
        current = float(semantic_parent.get("speed_factor", 1.0))
        factors = [value for value in SPEED_FACTORS if value != current]
        factors.sort(key=lambda value: (-value if unsafe else value))
        for value in factors:
            add("speed_factor", value, f"Test corridor speed factor {value:g}.")
        for row in objective_edge_rows(diagnosis_map):
            edge_id = str(row["edge_id"])
            add("edge_id", edge_id, f"Move the speed intervention to objective edge {edge_id}.")

    elif action == "remove_edge":
        legal = build_legal_candidate_pool(
            diagnosis_map,
            artifact_map,
            optimizer="network",
        ).get("network_optimizer", [])
        for spec in legal:
            if spec.get("action") == "remove_edge":
                edge_id = (spec.get("args") or {}).get("edge_id")
                add("edge_id", str(edge_id), f"Use validator-confirmed closure edge {edge_id}.")

    elif action == "make_road_one_way":
        # Only mutate a target when no explicit reverse edge would become stale.
        if "opposite_edge_id" not in semantic_parent:
            legal = build_legal_candidate_pool(
                diagnosis_map,
                artifact_map,
                optimizer="network",
            ).get("network_optimizer", [])
            for spec in legal:
                if spec.get("action") == "make_road_one_way":
                    edge_id = (spec.get("args") or {}).get("keep_edge_id")
                    add(
                        "keep_edge_id",
                        str(edge_id),
                        f"Use validator-confirmed one-way target {edge_id}.",
                    )

    elif action == "reroute_flows":
        current = float(semantic_parent.get("reroute_fraction", 0.25))
        fractions = [value for value in REROUTE_FRACTIONS if value != current]
        if unsafe:
            fractions.sort(key=lambda value: (value >= current, abs(value - current), value))
        else:
            fractions.sort(key=lambda value: (value <= current, abs(value - current), value))
        for value in fractions:
            add("reroute_fraction", value, f"Test grounded reroute fraction {value:g}.")
        for row in objective_edge_rows(diagnosis_map):
            edge_id = str(row["edge_id"])
            add("avoid_edges", [edge_id], f"Reroute around objective edge {edge_id}.")

    for index, candidate in enumerate(candidates, 1):
        candidate["mutation_id"] = f"mutation_{index:03d}"
    return candidates


def _mutation_payload(catalog: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "mutation_id": row.get("mutation_id"),
            "action": row.get("action"),
            "changed_field": row.get("changed_field"),
            "args": row.get("args"),
            "hint": row.get("hint"),
        }
        for row in catalog
    ]


def _resolve_mutation_selection(
    raw: Mapping[str, Any],
    catalog: List[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    by_id = {str(row.get("mutation_id")): row for row in catalog}
    selected_id = str(
        raw.get("mutation_id") or raw.get("selected_mutation_id") or ""
    ).strip()
    if selected_id in by_id:
        return by_id[selected_id]

    # Backward-compatible acceptance for tests or older models: a complete
    # proposal is accepted only when it exactly matches a finite catalog item.
    action = str(raw.get("action") or raw.get("capability") or "")
    if action and isinstance(raw.get("args"), Mapping):
        signature = _semantic_signature(action, raw.get("args") or {})
        for row in catalog:
            if signature == _semantic_signature(str(row.get("action") or ""), row.get("args") or {}):
                return row
    return None


def run_feedback_refinement(
    state: Any,
    *,
    parent_proposal: Mapping[str, Any],
    simulation_feedback: Mapping[str, Any],
    diagnosis: Optional[Mapping[str, Any]] = None,
    evaluated_history: Optional[List[Mapping[str, Any]]] = None,
    user_request: str = "",
    objective_profile: Optional[str] = None,
    search_round: int = 2,
    mutation_index: int = 1,
    call_llm_fn,
) -> Dict[str, Any]:
    """Let the LLM select one validator-confirmed finite local mutation."""
    parent = dict(parent_proposal or {})
    parent_agent = str(parent.get("agent") or "")
    role = next(
        (candidate for candidate in DEBATER_ROLES if candidate["agent"] == parent_agent),
        None,
    )
    if role is None:
        raise ValueError(f"Cannot refine proposal from unknown optimizer agent '{parent_agent}'")

    profile = objective_profile or infer_weight_profile(user_request)
    artifacts = _artifact_dict(state)
    role_view = {key: value for key, value in role.items() if key != "system_prompt"}
    catalog = build_finite_mutation_catalog(
        parent,
        diagnosis=diagnosis,
        artifacts=artifacts,
        simulation_feedback=simulation_feedback,
        evaluated_history=evaluated_history,
    )
    payload = {
        "role": role_view,
        "search_round": int(search_round),
        "mutation_index": int(mutation_index),
        "user_request": user_request,
        "objective_profile": profile,
        "objective_weights": get_weight_profile(profile),
        "artifacts": artifacts,
        "diagnosis": dict(diagnosis or {}),
        "parent_proposal": parent,
        "simulation_feedback": dict(simulation_feedback or {}),
        "evaluated_history": [dict(item) for item in (evaluated_history or [])],
        "capabilities": _capability_view(),
        "mutation_catalog": _mutation_payload(catalog),
    }
    base_usage_scope = (
        f"feedback:{parent_agent}:round_{int(search_round):02d}"
        + (f":mutation_{int(mutation_index):02d}" if int(mutation_index) > 1 else "")
    )
    raw: Dict[str, Any] = {}
    selected: Optional[Mapping[str, Any]] = None
    selection_attempts = 0
    if catalog:
        for attempt in range(2):
            request_payload = dict(payload)
            if attempt:
                request_payload["selection_error"] = (
                    "The previous response did not select an available mutation_id. "
                    "Choose exactly one ID from mutation_catalog."
                )
            response = call_llm_fn(
                system=str(role["system_prompt"]) + "\n\n" + FEEDBACK_REFINEMENT_RULES,
                user=json.dumps(request_payload, ensure_ascii=False, default=str),
                force_json=True,
                state=state,
                usage_scope=base_usage_scope + (":repair" if attempt else ""),
                max_tokens=500,
            )
            selection_attempts += 1
            raw = dict(response) if isinstance(response, Mapping) else {}
            selected = _resolve_mutation_selection(raw, catalog)
            if selected is not None:
                break

    if selected is not None:
        proposal = {
            "agent": parent_agent,
            "claim": str(raw.get("claim") or selected.get("hint") or ""),
            "capability": str(selected.get("action") or parent.get("action") or ""),
            "tool": "provider_sumo_modifier",
            "action": str(selected.get("action") or parent.get("action") or ""),
            "args": dict(selected.get("full_args") or {}),
            "evidence_refs": list(raw.get("evidence_refs") or ["simulation_feedback"]),
            "expected_effect": normalize_expected_effect(raw.get("expected_effect")),
            "risk": str(
                raw.get("risk")
                or "The validator-confirmed mutation may still transfer congestion downstream."
            ),
        }
    else:
        proposal = {
            "agent": parent_agent,
            "claim": str(raw.get("claim") or "No finite mutation was selected."),
            "capability": str(parent.get("action") or ""),
            "tool": "provider_sumo_modifier",
            "action": str(parent.get("action") or ""),
            "args": dict(parent.get("args") or {}),
            "evidence_refs": list(raw.get("evidence_refs") or []),
            "expected_effect": normalize_expected_effect(raw.get("expected_effect")),
            "risk": str(raw.get("risk") or "No unseen validator-confirmed mutation is available."),
        }

    proposal["proposal_id"] = (
        f"{parent.get('proposal_id') or parent_agent + '_proposal'}"
        f"__refine_r{int(search_round):02d}"
        + (f"_m{int(mutation_index):02d}" if int(mutation_index) > 1 else "")
    )
    proposal["args"] = _with_artifact_args(proposal["action"], proposal.get("args") or {}, artifacts)

    validation = validate_modifier_capability(
        proposal["action"],
        proposal.get("args") or {},
        getattr(state, "artifacts", {}),
    )
    changed_fields = _refinement_change_fields(parent, proposal)
    refinement_errors: List[str] = []
    if selected is None:
        raw_signature = _semantic_signature(
            str(raw.get("action") or raw.get("capability") or ""),
            raw.get("args") or {},
        )
        if raw_signature == _semantic_signature(
            str(parent.get("action") or ""), parent.get("args") or {}
        ):
            refinement_errors.append("refinement repeats the parent proposal")
        elif not catalog:
            refinement_errors.append("no unseen validator-confirmed finite mutation is available")
        else:
            refinement_errors.append("model did not select a mutation_id from mutation_catalog")
    if proposal.get("action") != parent.get("action"):
        refinement_errors.append("refinement must keep the parent action")
    if selected is not None and not changed_fields:
        refinement_errors.append("refinement repeats the parent proposal")
    elif selected is not None and len(changed_fields) > 1:
        refinement_errors.append(
            "refinement must change exactly one non-path argument; changed: "
            + ", ".join(changed_fields)
        )
    elif selected is not None:
        changed_field = changed_fields[0]
        mutable = _REFINEMENT_MUTABLE_FIELDS.get(str(parent.get("action") or ""), set())
        child_semantic_args = _semantic_args(proposal.get("args") or {})
        if changed_field not in mutable:
            refinement_errors.append(
                f"argument '{changed_field}' is not a supported local refinement variable"
            )
        elif changed_field not in child_semantic_args:
            refinement_errors.append(
                f"refinement cannot improve a candidate by deleting '{changed_field}'"
            )
    if refinement_errors:
        validation = dict(validation)
        validation["ok"] = False
        validation["error_type"] = "INVALID_FEEDBACK_REFINEMENT"
        validation["message"] = "Feedback refinement violated the local-search contract."
        validation["refinement_errors"] = refinement_errors

    proposal["validation"] = validation
    proposal["score"] = _score_proposal(proposal, validation, profile)
    proposal["proposal_source"] = "llm_feedback"
    proposal["search_round"] = int(search_round)
    proposal["parent_proposal_id"] = parent.get("proposal_id")
    proposal["refinement"] = {
        "operator": "finite_catalog_llm_selection",
        "mutation_index": int(mutation_index),
        "selected_mutation_id": selected.get("mutation_id") if selected else None,
        "mutation_catalog_size": len(catalog),
        "mutation_catalog_ids": [row.get("mutation_id") for row in catalog],
        "selection_attempts": selection_attempts,
        "changed_fields": changed_fields,
        "feedback_used": True,
        "validation_errors": refinement_errors,
    }
    return proposal


def _score_proposal(
    proposal: Mapping[str, Any],
    validation: Mapping[str, Any],
    objective_profile: str,
) -> float:
    if not validation.get("ok"):
        return -1_000.0

    score = 1.0
    evidence_refs = proposal.get("evidence_refs") or []
    if evidence_refs:
        score += 1.0

    effect = normalize_expected_effect(proposal.get("expected_effect"))
    primary_metric = {
        "delay": "mean_delay",
        "throughput": "throughput",
        "emissions": "co2",
    }.get(objective_profile)
    if primary_metric and effect.get(primary_metric) == "improve":
        score += 1.0
    elif objective_profile == "balanced" and any(direction == "improve" for direction in effect.values()):
        score += 1.0

    return score


def run_debate_optimizer(
    state: Any,
    *,
    diagnosis: Optional[Dict[str, Any]] = None,
    user_request: str = "",
    objective_profile: Optional[str] = None,
    optimizer_agents: Optional[List[str]] = None,
    call_llm_fn,
) -> Dict[str, Any]:
    profile = objective_profile or infer_weight_profile(user_request)
    weights = get_weight_profile(profile)
    artifacts = _artifact_dict(state)
    context = {
        "user_request": user_request,
        "objective_profile": profile,
        "objective_weights": weights,
        "artifacts": artifacts,
        "diagnosis": diagnosis or {},
        "capabilities": _capability_view(),
    }

    known_agents = {str(role["agent"]) for role in DEBATER_ROLES}
    requested_agents = set(optimizer_agents or known_agents)
    unknown_agents = requested_agents - known_agents
    if unknown_agents:
        raise ValueError(f"Unknown optimizer agent(s): {', '.join(sorted(unknown_agents))}")
    active_roles = [role for role in DEBATER_ROLES if role["agent"] in requested_agents]
    if not active_roles:
        raise ValueError("At least one optimizer agent must be enabled")

    proposals: List[Dict[str, Any]] = []
    for role in active_roles:
        role_view = {key: value for key, value in role.items() if key != "system_prompt"}
        user = json.dumps(
            {
                "role": role_view,
                **context,
            },
            ensure_ascii=False,
            default=str,
        )
        raw = call_llm_fn(
            system=str(role["system_prompt"]),
            user=user,
            force_json=True,
            state=state,
            usage_scope=f"debate:{role['agent']}",
            max_tokens=800,
        )
        proposal = _normalize_proposal(raw, role)
        proposal["args"] = _with_artifact_args(
            proposal["action"],
            proposal.get("args") or {},
            artifacts,
        )
        validation = validate_modifier_capability(
            proposal["action"],
            proposal.get("args") or {},
            getattr(state, "artifacts", {}),
        )
        proposal["validation"] = validation
        proposal["score"] = _score_proposal(proposal, validation, profile)
        proposals.append(proposal)

    valid = [p for p in proposals if (p.get("validation") or {}).get("ok")]
    selected: Optional[Dict[str, Any]] = max(valid, key=lambda p: float(p.get("score") or 0.0), default=None)

    judge_input = {
        "objective_profile": profile,
        "objective_weights": weights,
        "proposals": proposals,
        "programmatic_selected_proposal_id": selected.get("proposal_id") if selected else "",
    }
    judge = call_llm_fn(
        system=JUDGE_SYSTEM_PROMPT,
        user=json.dumps(judge_input, ensure_ascii=False, default=str),
        force_json=True,
        state=state,
        usage_scope="debate:judge",
        max_tokens=600,
    )

    judge_selected_id = str(judge.get("selected_proposal_id") or "").strip()
    if judge_selected_id:
        by_id = {str(p.get("proposal_id")): p for p in valid}
        selected = by_id.get(judge_selected_id, selected)

    selected_action = None
    if selected:
        selected_action = {
            "tool": selected.get("tool") or "provider_sumo_modifier",
            "action": selected.get("action"),
            "args": selected.get("args") or {},
        }

    bundle_candidates = generate_pair_bundles(
        valid,
        top_k=min(4, len(valid)),
        max_bundles=3,
    )

    return {
        "status": "ok" if selected_action else "no_executable_proposal",
        "active_optimizers": [role["agent"] for role in active_roles],
        "objective_profile": profile,
        "objective_weights": weights,
        "proposals": proposals,
        "judge": judge,
        "selected": selected,
        "selected_action": selected_action,
        "bundle_candidates": bundle_candidates,
    }
