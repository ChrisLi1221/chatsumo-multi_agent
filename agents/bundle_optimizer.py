from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from providers.Modification.edge_speed import (
    DEFAULT_CORRIDOR_EDGE_COUNT,
    corridor_speed_inventory,
)


TOPOLOGY_ACTIONS = {"remove_edge", "make_road_one_way"}
SPEED_ACTIONS = {"set_edge_speed", "set_corridor_speed"}
ROUTING_ACTIONS = {"reroute_flows"}
SIGNAL_ACTIONS = {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}

ACTION_PRIORITY = {
    "remove_edge": 10,
    "make_road_one_way": 10,
    "set_edge_speed": 20,
    "set_corridor_speed": 20,
    "reroute_flows": 30,
    "tls_apply_program": 40,
    "tls_set_program": 40,
    "tls_optimize_and_apply": 40,
}


def action_category(action: str) -> str:
    if action in TOPOLOGY_ACTIONS:
        return "topology"
    if action in SPEED_ACTIONS:
        return "speed"
    if action in ROUTING_ACTIONS:
        return "routing"
    if action in SIGNAL_ACTIONS:
        return "signal"
    return "unknown"


def _edge_targets(proposal: Mapping[str, Any]) -> Dict[str, Set[str]]:
    action = str(proposal.get("action") or "")
    args = dict(proposal.get("args") or {})
    removed: Set[str] = set()
    kept: Set[str] = set()
    modified: Set[str] = set()
    avoided: Set[str] = set()
    if action == "remove_edge" and args.get("edge_id"):
        removed.add(str(args["edge_id"]))
    elif action == "make_road_one_way":
        if args.get("keep_edge_id"):
            kept.add(str(args["keep_edge_id"]))
        if args.get("opposite_edge_id"):
            removed.add(str(args["opposite_edge_id"]))
    elif action == "set_edge_speed" and args.get("edge_id"):
        modified.add(str(args["edge_id"]))
    elif action == "set_corridor_speed" and args.get("edge_id"):
        modified.add(str(args["edge_id"]))
        net_path = args.get("net_path")
        if net_path:
            try:
                corridor = corridor_speed_inventory(
                    str(net_path),
                    str(args["edge_id"]),
                    int(args.get("edge_count", DEFAULT_CORRIDOR_EDGE_COUNT)),
                )
                modified.update(str(edge_id) for edge_id in corridor["edge_ids"])
            except Exception:
                # Canonical validation reports resolution failures. Keeping the
                # anchor here still provides a conservative compatibility check.
                pass
    elif action == "reroute_flows":
        raw = args.get("avoid_edges") or []
        if isinstance(raw, str):
            raw = [raw]
        avoided.update(str(edge_id) for edge_id in raw if edge_id)
    return {"removed": removed, "kept": kept, "modified": modified, "avoided": avoided}


def _tls_target(proposal: Mapping[str, Any]) -> Optional[str]:
    args = dict(proposal.get("args") or {})
    value = args.get("tl_id") or args.get("tls_id")
    return str(value) if value else None


def check_bundle_compatibility(proposals: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Apply conservative deterministic compatibility rules for a v1 bundle."""
    reasons: List[str] = []
    ids = [str(p.get("proposal_id") or "") for p in proposals]
    if len(set(ids)) != len(ids):
        reasons.append("bundle contains the same proposal more than once")

    categories: Dict[str, int] = {}
    for proposal in proposals:
        action = str(proposal.get("action") or "")
        validation = proposal.get("validation") or {}
        if not action:
            reasons.append("bundle contains an empty action")
        if validation and not validation.get("ok"):
            reasons.append(f"proposal '{proposal.get('proposal_id')}' is not executable")
        category = action_category(action)
        if category == "unknown":
            reasons.append(f"action '{action}' has no bundle compatibility rule")
        categories[category] = categories.get(category, 0) + 1

    # The first version allows at most one action from each intervention family.
    for category, count in categories.items():
        if category != "unknown" and count > 1:
            reasons.append(f"v1 bundles allow at most one {category} action")

    for left, right in combinations(proposals, 2):
        left_targets = _edge_targets(left)
        right_targets = _edge_targets(right)
        removed = left_targets["removed"] | right_targets["removed"]
        modified = left_targets["modified"] | right_targets["modified"]
        avoided = left_targets["avoided"] | right_targets["avoided"]
        if removed & modified:
            reasons.append(
                "an edge cannot be removed and speed-modified in the same bundle: "
                + ", ".join(sorted(removed & modified))
            )
        if removed & avoided:
            reasons.append(
                "rerouting around an edge already removed by the bundle is redundant: "
                + ", ".join(sorted(removed & avoided))
            )
        if modified & avoided:
            reasons.append(
                "an edge cannot be speed-modified and softly avoided in the same bundle: "
                + ", ".join(sorted(modified & avoided))
            )
        if action_category(str(left.get("action") or "")) == "signal" and action_category(
            str(right.get("action") or "")
        ) == "signal":
            left_tls = _tls_target(left)
            right_tls = _tls_target(right)
            if left_tls and left_tls == right_tls:
                reasons.append(f"multiple signal actions target TLS '{left_tls}'")

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "ok": not unique_reasons,
        "reasons": unique_reasons,
        "categories": categories,
    }


def order_bundle_actions(proposals: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return a stable domain-safe order: topology, speed, routing, then TLS."""
    indexed: List[Tuple[int, Mapping[str, Any]]] = list(enumerate(proposals))
    indexed.sort(
        key=lambda item: (
            ACTION_PRIORITY.get(str(item[1].get("action") or ""), 999),
            item[0],
        )
    )
    return [dict(proposal) for _, proposal in indexed]


def generate_pair_bundles(
    proposals: Sequence[Mapping[str, Any]],
    *,
    top_k: int = 4,
    max_bundles: int = 3,
    quality_by_id: Optional[Mapping[str, float]] = None,
) -> List[Dict[str, Any]]:
    """Generate compatible size-two bundles from the highest-quality proposals.

    ``quality_by_id`` is higher-is-better.  When omitted, proposal-stage scores
    are used.  The caller may therefore pass negative measured KPI scores after
    evaluating single actions.
    """
    if top_k < 2 or max_bundles <= 0:
        return []
    quality = dict(quality_by_id or {})

    def proposal_quality(proposal: Mapping[str, Any]) -> float:
        proposal_id = str(proposal.get("proposal_id") or "")
        if proposal_id in quality:
            return float(quality[proposal_id])
        return float(proposal.get("score") or 0.0)

    executable = [
        dict(proposal)
        for proposal in proposals
        if proposal.get("action") and (proposal.get("validation") or {}).get("ok", True)
    ]
    ranked = sorted(executable, key=proposal_quality, reverse=True)[:top_k]
    bundles: List[Dict[str, Any]] = []
    for left, right in combinations(ranked, 2):
        compatibility = check_bundle_compatibility([left, right])
        if not compatibility["ok"]:
            continue
        ordered = order_bundle_actions([left, right])
        proposal_ids = [str(p.get("proposal_id") or p.get("agent") or "proposal") for p in ordered]
        actions = [
            {
                "tool": proposal.get("tool") or "provider_sumo_modifier",
                "action": proposal.get("action"),
                "args": dict(proposal.get("args") or {}),
                "source_proposal_id": proposal.get("proposal_id"),
            }
            for proposal in ordered
        ]
        bundles.append(
            {
                "bundle_id": "bundle__" + "__".join(proposal_ids),
                "proposal_id": "bundle__" + "__".join(proposal_ids),
                "agent": "bundle_optimizer",
                "action": "bundle",
                "proposals": ordered,
                "actions": actions,
                "bundle_size": 2,
                "compatibility": compatibility,
                "heuristic_score": sum(proposal_quality(p) for p in ordered),
                "score": sum(float(p.get("score") or 0.0) for p in ordered) / 2.0,
                "risk": [str(p.get("risk") or "") for p in ordered if p.get("risk")],
            }
        )
    bundles.sort(key=lambda bundle: float(bundle["heuristic_score"]), reverse=True)
    return bundles[:max_bundles]
