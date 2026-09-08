from __future__ import annotations

import copy
import json
import random
from collections import deque
from itertools import combinations
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agents.capabilities import normalize_capability_args, validate_modifier_capability


SPEED_FACTORS: Tuple[float, ...] = (0.7, 0.8, 0.9)
REROUTE_FRACTIONS: Tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
MIN_OBJECTIVE_EDGE_LENGTH_M = 10.0
SPECIALIST_ROLES: Tuple[str, ...] = (
    "signal_optimizer",
    "network_optimizer",
    "demand_routing_optimizer",
)
ROLE_ALIASES = {
    "signal": "signal_optimizer",
    "network": "network_optimizer",
    "routing": "demand_routing_optimizer",
}
_LEGAL_POOL_CACHE: Dict[Tuple[Any, ...], Dict[str, List[Dict[str, Any]]]] = {}


def _ordered_unique(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def diagnosed_edge_rows(diagnosis: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return grounded edge rows, with measured bottlenecks ranked first."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for section in ("top_congested_edges", "bottlenecks", "critical_roads"):
        for raw in diagnosis.get(section) or []:
            if not isinstance(raw, Mapping) or not raw.get("edge_id"):
                continue
            edge_id = str(raw["edge_id"])
            if edge_id in seen:
                continue
            seen.add(edge_id)
            rows.append(dict(raw))
    for raw in (diagnosis.get("bottleneck_subgraph") or {}).get("edges") or []:
        if not isinstance(raw, Mapping) or not raw.get("edge_id"):
            continue
        edge_id = str(raw["edge_id"])
        if edge_id in seen:
            continue
        seen.add(edge_id)
        rows.append(dict(raw))
    return rows


def objective_edge_rows(diagnosis: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the measured objective edges used to ground executable actions.

    Top-congested edges are frozen by the experiment runner and already reflect
    the configured objective scope.  Prefer that focused set instead of drawing
    uniformly from every diagnostic list.  When older/global diagnoses do not
    provide it, fall back to the broader ranked rows for compatibility.
    """
    raw_rows = diagnosis.get("top_congested_edges") or diagnosed_edge_rows(diagnosis)
    rows: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping) or not raw.get("edge_id"):
            continue
        edge_id = str(raw["edge_id"])
        if edge_id in seen:
            continue
        length = raw.get("length_m")
        if length not in (None, ""):
            try:
                if float(length) < MIN_OBJECTIVE_EDGE_LENGTH_M:
                    continue
            except (TypeError, ValueError):
                continue
        seen.add(edge_id)
        rows.append(dict(raw))
    return rows


def diagnosed_tls_ids(diagnosis: Mapping[str, Any]) -> List[str]:
    return _ordered_unique(
        (diagnosis.get("bottleneck_subgraph") or {}).get("tls_ids") or []
    )


def nearest_tls_ids(
    diagnosis: Mapping[str, Any],
    anchor_edge_id: Optional[str] = None,
) -> List[str]:
    """Rank TLS junctions by hop distance from a diagnosed anchor edge."""
    subgraph = dict(diagnosis.get("bottleneck_subgraph") or {})
    tls_ids = diagnosed_tls_ids(diagnosis)
    if not tls_ids:
        return []

    edge_rows = [
        dict(row)
        for row in subgraph.get("edges") or []
        if isinstance(row, Mapping)
    ]
    anchor = next(
        (row for row in edge_rows if str(row.get("edge_id") or "") == str(anchor_edge_id or "")),
        None,
    )
    if anchor is None:
        return tls_ids

    adjacency: Dict[str, set[str]] = {}
    for row in edge_rows:
        source = str(row.get("from_junction") or "")
        target = str(row.get("to_junction") or "")
        if not source or not target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)

    starts = _ordered_unique(
        [anchor.get("from_junction"), anchor.get("to_junction")]
    )
    distances: Dict[str, int] = {}
    queue = deque((node, 0) for node in starts)
    visited = set(starts)
    while queue:
        node, distance = queue.popleft()
        if node in tls_ids and node not in distances:
            distances[node] = distance
        for neighbor in sorted(adjacency.get(node, set())):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, distance + 1))
    return sorted(tls_ids, key=lambda tls_id: (distances.get(tls_id, 10**9), tls_id))


def _active_roles(optimizer: str) -> List[str]:
    if optimizer == "all":
        return list(SPECIALIST_ROLES)
    role = ROLE_ALIASES.get(str(optimizer))
    if not role:
        raise ValueError(f"Unknown optimizer family: {optimizer}")
    return [role]


def _artifact_args(action: str, args: Mapping[str, Any], artifacts: Mapping[str, str]) -> Dict[str, Any]:
    enriched = dict(args or {})
    if action in {
        "set_corridor_speed",
        "remove_edge",
        "make_road_one_way",
        "reroute_flows",
    }:
        enriched["net_path"] = artifacts.get("net")
        enriched["route_path"] = artifacts.get("routes")
        enriched["cfg_path"] = artifacts.get("cfg")
    elif action in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
        enriched["cfg_path"] = artifacts.get("cfg")
        if action == "tls_optimize_and_apply":
            enriched["route_path"] = artifacts.get("routes")
    return normalize_capability_args(action, enriched)


def _proposal_signature(action: str, args: Mapping[str, Any]) -> str:
    excluded = {"net_path", "route_path", "cfg_path", "out_dir"}
    semantic_args = {key: value for key, value in args.items() if key not in excluded}
    return json.dumps([action, semantic_args], sort_keys=True, default=str)


def _make_proposal(
    *,
    proposal_source: str,
    proposal_index: int,
    agent: str,
    action: str,
    args: Mapping[str, Any],
    artifacts: Mapping[str, str],
    evidence_refs: Optional[Sequence[str]] = None,
    reason: str = "",
) -> Dict[str, Any]:
    normalized = _artifact_args(action, args, artifacts) if action else dict(args or {})
    validation = validate_modifier_capability(action, normalized, artifacts)
    proposal_id = f"{proposal_source}_{proposal_index:03d}_{action or 'noop'}"
    return {
        "proposal_id": proposal_id,
        "proposal_source": proposal_source,
        "agent": agent,
        "claim": reason or f"Apply grounded {action or 'no-op'} candidate.",
        "capability": action,
        "tool": "provider_sumo_modifier",
        "action": action,
        "args": normalized,
        "evidence_refs": list(evidence_refs or []),
        "expected_effect": {
            "mean_delay": "unknown",
            "throughput": "unknown",
            "mean_speed": "unknown",
            "co2": "unknown",
        },
        "risk": "The measured network response is unknown before SUMO evaluation.",
        "validation": validation,
        "score": 1.0 if validation.get("ok") else -1000.0,
        "grounding": {
            "edge_targets_from_diagnosis": True,
            "tls_targets_from_bottleneck_subgraph": True,
            "discrete_speed_factors": list(SPEED_FACTORS),
            "discrete_reroute_fractions": list(REROUTE_FRACTIONS),
        },
    }


def _abstention(
    source: str,
    index: int,
    role: str,
    artifacts: Mapping[str, str],
    reason: str,
) -> Dict[str, Any]:
    proposal = _make_proposal(
        proposal_source=source,
        proposal_index=index,
        agent=role,
        action="",
        args={},
        artifacts=artifacts,
        reason=reason,
    )
    proposal["abstained"] = True
    proposal["risk"] = reason
    return proposal


def _candidate_spec(
    action: str,
    args: Mapping[str, Any],
    evidence_refs: Sequence[str],
    reason: str,
) -> Dict[str, Any]:
    return {
        "action": action,
        "args": dict(args),
        "evidence_refs": list(evidence_refs),
        "reason": reason,
    }


def _raw_role_candidate_specs(
    diagnosis: Mapping[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Enumerate a compact, objective-aligned discrete search space."""
    edge_ids = [str(row["edge_id"]) for row in objective_edge_rows(diagnosis)]
    tls_ids: List[str] = []
    for edge_id in edge_ids:
        tls_ids.extend(nearest_tls_ids(diagnosis, edge_id))
    tls_ids = _ordered_unique(tls_ids or diagnosed_tls_ids(diagnosis))

    signal = [
        _candidate_spec(
            "tls_optimize_and_apply",
            {"tl_id": tls_id},
            ["diagnosis.bottleneck_subgraph.tls_ids", "diagnosis.top_congested_edges"],
            f"Use validator-confirmed TLS {tls_id} near the measured objective edges.",
        )
        for tls_id in tls_ids
    ]

    network: List[Dict[str, Any]] = []
    for edge_id in edge_ids:
        for factor in SPEED_FACTORS:
            network.append(
                _candidate_spec(
                    "set_corridor_speed",
                    {"edge_id": edge_id, "edge_count": 5, "speed_factor": factor},
                    ["diagnosis.top_congested_edges"],
                    f"Apply speed factor {factor:g} to the objective corridor at {edge_id}.",
                )
            )
        network.append(
            _candidate_spec(
                "remove_edge",
                {"edge_id": edge_id},
                ["diagnosis.top_congested_edges"],
                f"Close validator-confirmed objective edge {edge_id}.",
            )
        )
        network.append(
            _candidate_spec(
                "make_road_one_way",
                {"keep_edge_id": edge_id},
                ["diagnosis.top_congested_edges"],
                f"Keep objective edge {edge_id} and remove its validator-confirmed reverse edge.",
            )
        )

    # Start from a one-edge trust region.  The experiment runner can still
    # expand routing edge counts explicitly, while feedback may change the
    # target or intensity inside this auditable finite neighborhood.
    routing = [
        _candidate_spec(
            "reroute_flows",
            {
                "avoid_edges": [edge_id],
                "reroute_fraction": fraction,
                "min_route_retention": 1.0,
            },
            ["diagnosis.top_congested_edges"],
            f"Soft-reroute a {fraction:.0%} share around objective edge {edge_id}.",
        )
        for edge_id in edge_ids
        for fraction in REROUTE_FRACTIONS
    ]
    return {
        "signal_optimizer": signal,
        "network_optimizer": network,
        "demand_routing_optimizer": routing,
    }


def build_legal_candidate_pool(
    diagnosis: Mapping[str, Any],
    artifacts: Mapping[str, str],
    *,
    optimizer: str = "all",
) -> Dict[str, List[Dict[str, Any]]]:
    """Build role-specific candidates that pass executable capability validation.

    Invalid topology targets, non-optimizable TLS IDs, malformed controls, and
    missing artifacts are removed before they can consume a proposal or SUMO
    budget.  Returned args include orchestrator-owned artifact paths.
    """
    active_roles = set(_active_roles(optimizer))
    cache_key = (
        str(artifacts.get("net") or ""),
        str(artifacts.get("routes") or ""),
        str(artifacts.get("cfg") or ""),
        tuple(str(row["edge_id"]) for row in objective_edge_rows(diagnosis)),
        tuple(diagnosed_tls_ids(diagnosis)),
    )
    cached = _LEGAL_POOL_CACHE.get(cache_key)
    if cached is not None:
        return {
            role: copy.deepcopy(cached.get(role) or [])
            for role in active_roles
        }

    all_roles = set(SPECIALIST_ROLES)
    pool: Dict[str, List[Dict[str, Any]]] = {role: [] for role in all_roles}
    signatures = set()
    for role, specs in _raw_role_candidate_specs(diagnosis).items():
        if role not in all_roles:
            continue
        for spec in specs:
            action = str(spec["action"])
            args = _artifact_args(action, spec.get("args") or {}, artifacts)
            validation = validate_modifier_capability(action, args, artifacts)
            signature = _proposal_signature(action, args)
            if not validation.get("ok") or signature in signatures:
                continue
            signatures.add(signature)
            pool[role].append(
                {
                    **spec,
                    "args": args,
                    "validation": validation,
                }
            )
    _LEGAL_POOL_CACHE[cache_key] = copy.deepcopy(pool)
    return {
        role: copy.deepcopy(pool.get(role) or [])
        for role in active_roles
    }


def _draw_legal_spec(
    role_pool: List[Dict[str, Any]],
    rng: random.Random,
) -> Optional[Dict[str, Any]]:
    """Draw uniformly across available action families, then configurations."""
    if not role_pool:
        return None
    by_action: Dict[str, List[Dict[str, Any]]] = {}
    for spec in role_pool:
        by_action.setdefault(str(spec["action"]), []).append(spec)
    action = rng.choice(sorted(by_action))
    selected = rng.choice(by_action[action])
    role_pool.remove(selected)
    return selected


def generate_grounded_random(
    diagnosis: Mapping[str, Any],
    artifacts: Mapping[str, str],
    *,
    attempt_budget: int,
    seed: int,
    optimizer: str = "all",
) -> List[Dict[str, Any]]:
    roles = _active_roles(optimizer)
    legal_pool = build_legal_candidate_pool(diagnosis, artifacts, optimizer=optimizer)
    remaining = {role: list(legal_pool.get(role) or []) for role in roles}
    rng = random.Random(int(seed))
    proposals: List[Dict[str, Any]] = []
    while len(proposals) < attempt_budget:
        added = False
        for role in roles:
            spec = _draw_legal_spec(remaining[role], rng)
            if spec is None:
                continue
            proposal = _make_proposal(
                proposal_source="grounded_random",
                proposal_index=len(proposals) + 1,
                agent=role,
                action=str(spec["action"]),
                args=spec.get("args") or {},
                artifacts=artifacts,
                evidence_refs=spec.get("evidence_refs") or [],
                reason=str(spec.get("reason") or ""),
            )
            proposal["grounding"].update(
                {
                    "legal_candidate_pool": True,
                    "role_pool_size": len(legal_pool.get(role) or []),
                    "objective_edge_ids": [
                        str(row["edge_id"]) for row in objective_edge_rows(diagnosis)
                    ],
                }
            )
            proposals.append(proposal)
            added = True
            if len(proposals) >= attempt_budget:
                break
        if not added:
            break
    return proposals


def _round_robin_specs(
    role_specs: Mapping[str, Sequence[Tuple[str, Dict[str, Any], List[str], str]]],
    roles: Sequence[str],
    attempt_budget: Optional[int],
) -> List[Tuple[str, Tuple[str, Dict[str, Any], List[str], str]]]:
    positions = {role: 0 for role in roles}
    result: List[Tuple[str, Tuple[str, Dict[str, Any], List[str], str]]] = []
    while attempt_budget is None or len(result) < attempt_budget:
        added = False
        for role in roles:
            position = positions[role]
            specs = role_specs.get(role) or []
            if position >= len(specs):
                continue
            result.append((role, specs[position]))
            positions[role] += 1
            added = True
            if attempt_budget is not None and len(result) >= attempt_budget:
                break
        if not added:
            break
    return result


def _heuristic_role_specs(
    diagnosis: Mapping[str, Any],
) -> Dict[str, List[Tuple[str, Dict[str, Any], List[str], str]]]:
    rows = diagnosed_edge_rows(diagnosis)
    ranked_edges = [str(row["edge_id"]) for row in rows]
    tls_ids: List[str] = []
    for edge_id in ranked_edges:
        tls_ids.extend(nearest_tls_ids(diagnosis, edge_id))
    tls_ids = _ordered_unique(tls_ids or diagnosed_tls_ids(diagnosis))

    signal = [
        (
            "tls_optimize_and_apply",
            {"tl_id": tls_id},
            ["diagnosis.bottleneck_subgraph", "diagnosis.top_congested_edges"],
            f"Select TLS {tls_id} with the shortest diagnosed hop distance to a ranked bottleneck.",
        )
        for tls_id in tls_ids
    ]
    network = [
        (
            "set_corridor_speed",
            {"edge_id": edge_id, "edge_count": 5, "speed_factor": factor},
            ["diagnosis.top_congested_edges"],
            f"Apply factor {factor:g} to the highest-ranked remaining congested corridor.",
        )
        for factor in (0.8, 0.7, 0.9)
        for edge_id in ranked_edges
    ]
    routing: List[Tuple[str, Dict[str, Any], List[str], str]] = []
    for fraction in (0.5, 0.75, 0.25, 1.0):
        for count in range(1, min(3, len(ranked_edges)) + 1):
            targets = ranked_edges[:count]
            routing.append(
                (
                    "reroute_flows",
                    {
                        "avoid_edges": targets,
                        "reroute_fraction": fraction,
                        "min_route_retention": 1.0,
                    },
                    ["diagnosis.top_congested_edges"],
                    f"Reroute around the top {count} diagnosed edge(s) at fraction {fraction:g}.",
                )
            )
    return {
        "signal_optimizer": signal,
        "network_optimizer": network,
        "demand_routing_optimizer": routing,
    }


def generate_deterministic_heuristic(
    diagnosis: Mapping[str, Any],
    artifacts: Mapping[str, str],
    *,
    attempt_budget: int,
    optimizer: str = "all",
) -> List[Dict[str, Any]]:
    roles = _active_roles(optimizer)
    specs = _round_robin_specs(
        _heuristic_role_specs(diagnosis),
        roles,
        attempt_budget,
    )
    proposals = [
        _make_proposal(
            proposal_source="deterministic_heuristic",
            proposal_index=index,
            agent=role,
            action=spec[0],
            args=spec[1],
            artifacts=artifacts,
            evidence_refs=spec[2],
            reason=spec[3],
        )
        for index, (role, spec) in enumerate(specs, 1)
    ]
    while len(proposals) < attempt_budget:
        role = roles[len(proposals) % len(roles)]
        proposals.append(
            _abstention(
                "deterministic_heuristic",
                len(proposals) + 1,
                role,
                artifacts,
                "The deterministic grounded candidate list is exhausted.",
            )
        )
    return proposals


def _grid_role_specs(
    diagnosis: Mapping[str, Any],
    edge_pool_size: int,
) -> Dict[str, List[Tuple[str, Dict[str, Any], List[str], str]]]:
    edge_ids = [
        str(row["edge_id"])
        for row in diagnosed_edge_rows(diagnosis)[: max(1, int(edge_pool_size))]
    ]
    signal = [
        (
            "tls_optimize_and_apply",
            {"tl_id": tls_id},
            ["diagnosis.bottleneck_subgraph.tls_ids"],
            "Enumerate a grounded TLS target.",
        )
        for tls_id in diagnosed_tls_ids(diagnosis)
    ]
    network: List[Tuple[str, Dict[str, Any], List[str], str]] = []
    for edge_id in edge_ids:
        for factor in SPEED_FACTORS:
            network.append(
                (
                    "set_corridor_speed",
                    {"edge_id": edge_id, "edge_count": 5, "speed_factor": factor},
                    ["diagnosis.top_congested_edges"],
                    "Enumerate a grounded corridor-speed configuration.",
                )
            )
        network.append(
            (
                "remove_edge",
                {"edge_id": edge_id},
                ["diagnosis.top_congested_edges"],
                "Enumerate a grounded edge-closure configuration.",
            )
        )
        network.append(
            (
                "make_road_one_way",
                {"keep_edge_id": edge_id},
                ["diagnosis.top_congested_edges"],
                "Enumerate a grounded one-way configuration.",
            )
        )
    routing: List[Tuple[str, Dict[str, Any], List[str], str]] = []
    for count in range(1, min(3, len(edge_ids)) + 1):
        for targets in combinations(edge_ids, count):
            for fraction in REROUTE_FRACTIONS:
                routing.append(
                    (
                        "reroute_flows",
                        {
                            "avoid_edges": list(targets),
                            "reroute_fraction": fraction,
                            "min_route_retention": 1.0,
                        },
                        ["diagnosis.top_congested_edges"],
                        "Enumerate grounded routing targets and rerouting intensity.",
                    )
                )
    return {
        "signal_optimizer": signal,
        "network_optimizer": network,
        "demand_routing_optimizer": routing,
    }


def generate_grounded_grid(
    diagnosis: Mapping[str, Any],
    artifacts: Mapping[str, str],
    *,
    attempt_budget: Optional[int],
    optimizer: str = "all",
    edge_pool_size: int = 3,
) -> List[Dict[str, Any]]:
    roles = _active_roles(optimizer)
    specs = _round_robin_specs(
        _grid_role_specs(diagnosis, edge_pool_size),
        roles,
        attempt_budget,
    )
    return [
        _make_proposal(
            proposal_source="grounded_grid",
            proposal_index=index,
            agent=role,
            action=spec[0],
            args=spec[1],
            artifacts=artifacts,
            evidence_refs=spec[2],
            reason=spec[3],
        )
        for index, (role, spec) in enumerate(specs, 1)
    ]


def generate_grounded_baseline(
    method: str,
    diagnosis: Mapping[str, Any],
    artifacts: Mapping[str, str],
    *,
    attempt_budget: int,
    seed: int = 42,
    optimizer: str = "all",
    grid_edge_pool_size: int = 3,
) -> Dict[str, Any]:
    if attempt_budget <= 0:
        raise ValueError("attempt_budget must be positive")
    if method in {"grounded_random", "random_initial_llm"}:
        proposals = generate_grounded_random(
            diagnosis,
            artifacts,
            attempt_budget=attempt_budget,
            seed=seed,
            optimizer=optimizer,
        )
        if method == "random_initial_llm":
            for proposal in proposals:
                proposal["proposal_id"] = str(proposal.get("proposal_id") or "").replace(
                    "grounded_random_", "random_initial_llm_", 1
                )
                proposal["proposal_source"] = "random_initial_llm"
                proposal["initialization_source"] = "grounded_random"
                proposal["claim"] = (
                    "Use a grounded-random initialization that will be refined "
                    "after measured SUMO feedback."
                )
    elif method == "deterministic_heuristic":
        proposals = generate_deterministic_heuristic(
            diagnosis,
            artifacts,
            attempt_budget=attempt_budget,
            optimizer=optimizer,
        )
    elif method == "grounded_grid":
        proposals = generate_grounded_grid(
            diagnosis,
            artifacts,
            attempt_budget=attempt_budget,
            optimizer=optimizer,
            edge_pool_size=grid_edge_pool_size,
        )
    else:
        raise ValueError(f"Unknown grounded baseline method: {method}")

    valid = [proposal for proposal in proposals if (proposal.get("validation") or {}).get("ok")]
    return {
        "status": "ok" if valid else "no_executable_proposal",
        "proposal_source": method,
        "active_optimizers": _active_roles(optimizer),
        "proposals": proposals,
        "selected": valid[0] if valid else None,
        "selected_action": (
            {
                "tool": valid[0]["tool"],
                "action": valid[0]["action"],
                "args": valid[0]["args"],
            }
            if valid
            else None
        ),
        "judge": {
            "used": False,
            "selected_proposal_id": "",
            "reason": (
                "Random initialization defers reasoning to the measured LLM feedback round."
                if method == "random_initial_llm"
                else "Non-LLM baselines use the common simulator-based selector."
            ),
        },
        "search_space": {
            "edge_candidate_count": len(diagnosed_edge_rows(diagnosis)),
            "objective_edge_candidate_count": len(objective_edge_rows(diagnosis)),
            "tls_candidate_count": len(diagnosed_tls_ids(diagnosis)),
            "speed_factors": list(SPEED_FACTORS),
            "reroute_fractions": list(REROUTE_FRACTIONS),
            "routing_edge_counts": [1, 2, 3],
            "initial_routing_edge_counts": (
                [1] if method in {"grounded_random", "random_initial_llm"} else None
            ),
            "legal_candidate_pool_enforced": method in {
                "grounded_random",
                "random_initial_llm",
            },
            "grid_edge_pool_size": grid_edge_pool_size if method == "grounded_grid" else None,
            "initialization_source": (
                "grounded_random" if method == "random_initial_llm" else method
            ),
        },
    }
