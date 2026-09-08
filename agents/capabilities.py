from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from providers.Modification.remove_edge import (
    EdgeTargetError,
    check_edge_removal_feasibility,
    resolve_opposite_edge,
    resolve_edge_target,
)
from providers.Modification.edge_speed import (
    DEFAULT_CORRIDOR_EDGE_COUNT,
    MAX_CORRIDOR_EDGE_COUNT,
    MAX_CORRIDOR_SPEED_FACTOR,
    MIN_CORRIDOR_EDGE_COUNT,
    MIN_CORRIDOR_SPEED_FACTOR,
    validate_corridor_speed_change,
    validate_edge_speed_change,
)
from providers.Modification.tls_program import load_tls_inventory, read_sumocfg_paths


MODIFIER_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "set_edge_speed": {
        "category": "network_control",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "set_edge_speed",
        "required_artifacts": ["net"],
        "required_params": ["net_path", "edge_id", "speed_mps"],
        "optional_params": ["route_path", "cfg_path", "out_dir"],
        "aliases": {"street_name": "edge_id", "target_edge": "edge_id", "new_speed": "speed_mps"},
        "description": "Set every lane on one exact edge to a conservative speed in an isolated network.",
    },
    "set_corridor_speed": {
        "category": "network_control",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "set_corridor_speed",
        "required_artifacts": ["net"],
        "required_params": ["net_path", "edge_id", "speed_factor"],
        "optional_params": ["edge_count", "route_path", "cfg_path", "out_dir"],
        "aliases": {
            "anchor_edge_id": "edge_id",
            "target_edge": "edge_id",
            "corridor_edge_count": "edge_count",
            "speed_ratio": "speed_factor",
        },
        "description": (
            "Statically scale lane speed limits on up to five contiguous same-road "
            "edges around one evidenced anchor edge in an isolated network."
        ),
    },
    "remove_edge": {
        "category": "network_topology",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "remove_edge",
        "required_artifacts": ["net"],
        "required_params": ["net_path", "edge_id"],
        "optional_params": [
            "route_path",
            "cfg_path",
            "out_dir",
            "volume",
            "min_route_retention",
        ],
        "aliases": {"street_name": "edge_id", "target_edge": "edge_id"},
        "description": "Remove one exact edge into a new network and repair affected routes.",
    },
    "make_road_one_way": {
        "category": "network_topology",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "make_road_one_way",
        "required_artifacts": ["net"],
        "required_params": ["net_path", "keep_edge_id"],
        "optional_params": [
            "opposite_edge_id",
            "route_path",
            "cfg_path",
            "out_dir",
            "min_route_retention",
        ],
        "aliases": {"edge_id": "keep_edge_id", "keep_edge": "keep_edge_id"},
        "description": "Keep one exact directed edge and remove its unique reverse edge without adding lanes.",
    },
    "tls_apply_program": {
        "category": "signal_control",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "tls_apply_program",
        "required_artifacts": ["cfg"],
        "required_params": ["cfg_path", "tl_id", "cycle", "splits"],
        "optional_params": ["offset", "out_dir"],
        "aliases": {"tls_id": "tl_id"},
        "description": "Attach a fixed-time traffic signal program with explicit green splits.",
    },
    "tls_set_program": {
        "category": "signal_control",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "tls_set_program",
        "required_artifacts": ["cfg"],
        "required_params": ["cfg_path", "tls_id", "program_xml"],
        "optional_params": ["out_dir"],
        "description": "Attach a custom SUMO tlLogic XML program.",
    },
    "tls_optimize_and_apply": {
        "category": "signal_control",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "tls_optimize_and_apply",
        "required_artifacts": ["cfg"],
        "required_params": ["cfg_path", "tl_id"],
        "optional_params": [
            "cycle",
            "min_split",
            "offset",
            "route_path",
            "phase_edge_groups",
            "demands",
            "out_dir",
        ],
        "aliases": {"tls_id": "tl_id"},
        "description": "Optimize simple fixed-time green splits and attach the program.",
    },
    "reroute_flows": {
        "category": "routing",
        "supported": True,
        "tool": "provider_sumo_modifier",
        "action": "reroute_flows",
        "required_artifacts": ["net", "routes"],
        "required_params": ["net_path", "route_path", "avoid_edges", "reroute_fraction"],
        "optional_params": [
            "cfg_path",
            "out_dir",
            "routing_algorithm",
            "min_route_retention",
            "avoid_penalty_s",
        ],
        "aliases": {
            "routes": "route_path",
            "avoid_edge_ids": "avoid_edges",
            "bottleneck_edges": "avoid_edges",
            "reroute_ratio": "reroute_fraction",
            "reroute_percentage": "reroute_fraction",
        },
        "description": (
            "Jointly soft-reroute an LLM-selected fraction of vehicles using one to three "
            "bottleneck edges while preserving every vehicle and its original OD."
        ),
    },
}


KNOWN_UNSUPPORTED_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "add_lane": {
        "category": "network_capacity",
        "supported": False,
        "missing_tool": "lane_reconfiguration",
        "description": "Add a lane to an existing edge.",
    },
    "change_lane_direction": {
        "category": "network_capacity",
        "supported": False,
        "missing_tool": "lane_reconfiguration",
        "description": "Reverse or reassign lane direction.",
    },
    "demand_scaling": {
        "category": "demand_management",
        "supported": False,
        "missing_tool": "demand_optimizer",
        "description": "Scale or redistribute OD demand as an optimization action.",
    },
}


def modifier_capability_summary() -> str:
    lines = []
    for name, cap in MODIFIER_CAPABILITIES.items():
        required = ", ".join(cap.get("required_params", []))
        optional = ", ".join(cap.get("optional_params", []))
        lines.append(
            f"- {name}: required=[{required}] optional=[{optional}]"
        )
    return "\n".join(lines)


def normalize_capability_args(action: str, args: Dict[str, Any]) -> Dict[str, Any]:
    cap = MODIFIER_CAPABILITIES.get(action)
    if not cap:
        return dict(args or {})

    normalized = dict(args or {})
    for src, dst in (cap.get("aliases") or {}).items():
        if src in normalized and dst not in normalized:
            normalized[dst] = normalized.pop(src)
    if action == "reroute_flows" and isinstance(normalized.get("avoid_edges"), str):
        raw = normalized["avoid_edges"]
        normalized["avoid_edges"] = [
            item.strip() for item in raw.replace(";", ",").split(",") if item.strip()
        ]
    if action == "reroute_flows" and normalized.get("reroute_fraction") not in (None, ""):
        raw_fraction = normalized["reroute_fraction"]
        try:
            text = str(raw_fraction).strip()
            value = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
            if value in {25.0, 50.0, 75.0, 100.0}:
                value /= 100.0
            normalized["reroute_fraction"] = value
        except Exception:
            pass
    if action == "set_corridor_speed" and normalized.get("speed_factor") not in (None, ""):
        try:
            text = str(normalized["speed_factor"]).strip()
            value = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
            if value > 1.0:
                value /= 100.0
            normalized["speed_factor"] = value
        except Exception:
            pass
    return normalized


def _is_placeholder(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {
        "",
        "unknown",
        "unknown_from_evidence",
        "existing_tls",
        "current_tls",
        "default_tls",
        "tls_id",
        "tl_id",
        "target_id",
        "edge_id",
        "none",
        "null",
        "n/a",
    }


def _numeric_range_errors(action: str, args: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    def as_float(name: str) -> Optional[float]:
        if name not in args or args.get(name) in (None, ""):
            return None
        try:
            return float(args[name])
        except Exception:
            errors.append(f"{name} must be numeric")
            return None

    cycle = as_float("cycle")
    if cycle is not None and cycle <= 0:
        errors.append("cycle must be > 0")

    min_split = as_float("min_split")
    if min_split is not None and not (0.0 < min_split < 0.5):
        errors.append("min_split must be a fraction in (0.0, 0.5)")

    min_route_retention = as_float("min_route_retention")
    if action == "reroute_flows" and min_route_retention is not None and min_route_retention != 1.0:
        errors.append("reroute_flows requires min_route_retention=1.0")
    elif min_route_retention is not None and not (0.90 <= min_route_retention <= 1.0):
        errors.append("min_route_retention must be in [0.90, 1.0]")

    avoid_penalty_s = as_float("avoid_penalty_s")
    if avoid_penalty_s is not None and avoid_penalty_s <= 0:
        errors.append("avoid_penalty_s must be > 0")

    reroute_fraction = as_float("reroute_fraction")
    if action == "reroute_flows" and reroute_fraction is not None and reroute_fraction not in {
        0.25,
        0.5,
        0.75,
        1.0,
    }:
        errors.append("reroute_fraction must be one of [0.25, 0.5, 0.75, 1.0]")

    speed_factor = as_float("speed_factor")
    if action == "set_corridor_speed" and speed_factor is not None and not (
        MIN_CORRIDOR_SPEED_FACTOR <= speed_factor <= MAX_CORRIDOR_SPEED_FACTOR
    ):
        errors.append(
            f"speed_factor must be in [{MIN_CORRIDOR_SPEED_FACTOR}, "
            f"{MAX_CORRIDOR_SPEED_FACTOR}]"
        )

    if action == "tls_apply_program":
        splits = args.get("splits")
        if not isinstance(splits, list) or not splits:
            errors.append("splits must be a non-empty list")
        else:
            try:
                vals = [float(x) for x in splits]
                if any(x <= 0 for x in vals):
                    errors.append("splits must be strictly positive")
                if sum(vals) <= 0 or sum(vals) > 1.0:
                    errors.append("sum(splits) must be in (0, 1]")
            except Exception:
                errors.append("splits must contain numeric values")

    return errors


def _argument_shape_errors(action: str, args: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if action == "reroute_flows":
        avoid_edges = args.get("avoid_edges")
        if isinstance(avoid_edges, list):
            if not 1 <= len(avoid_edges) <= 3:
                errors.append("avoid_edges must contain between one and three edge IDs")
            elif len({str(edge_id) for edge_id in avoid_edges}) != len(avoid_edges):
                errors.append("avoid_edges must contain distinct edge IDs")
    if action == "set_corridor_speed" and args.get("edge_count") not in (None, ""):
        try:
            raw = float(args["edge_count"])
            edge_count = int(raw)
            if raw != edge_count:
                raise ValueError
            if not MIN_CORRIDOR_EDGE_COUNT <= edge_count <= MAX_CORRIDOR_EDGE_COUNT:
                errors.append(
                    f"edge_count must be in [{MIN_CORRIDOR_EDGE_COUNT}, "
                    f"{MAX_CORRIDOR_EDGE_COUNT}]"
                )
        except (TypeError, ValueError):
            errors.append("edge_count must be an integer")
    return errors


def _tls_inventory_errors(action: str, args: Dict[str, Any]) -> List[str]:
    if action not in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
        return []
    cfg_path = str(args.get("cfg_path") or "")
    if not os.path.isfile(cfg_path):
        return [f"cfg_path not found: {cfg_path}"]
    tls_id = str(args.get("tl_id") or args.get("tls_id") or "")
    try:
        cfg_info = read_sumocfg_paths(cfg_path)
        net_path = cfg_info.get("net")
        if not net_path or not os.path.isfile(str(net_path)):
            return [f"net-file referenced by cfg was not found: {net_path}"]
        inventory = load_tls_inventory(str(net_path), tls_id)
    except Exception as exc:
        return [str(exc)]

    green_count = len(inventory.get("green_phase_indices") or [])
    if action in {"tls_apply_program", "tls_optimize_and_apply"} and green_count < 2:
        return [f"TLS '{tls_id}' has fewer than two adjustable green phases"]
    values = args.get("splits") if action == "tls_apply_program" else args.get("demands")
    if values is not None and isinstance(values, list) and len(values) != green_count:
        return [f"TLS '{tls_id}' requires {green_count} phase values, got {len(values)}"]
    groups = args.get("phase_edge_groups")
    if groups is not None and isinstance(groups, list) and len(groups) != green_count:
        return [f"TLS '{tls_id}' requires {green_count} phase edge groups, got {len(groups)}"]
    return []


def _edge_target_errors(action: str, args: Dict[str, Any]) -> List[str]:
    if action not in {
        "remove_edge",
        "set_edge_speed",
        "set_corridor_speed",
        "make_road_one_way",
    }:
        return []
    net_path = str(args.get("net_path") or "")
    if not os.path.isfile(net_path):
        return [f"net_path not found: {net_path}"]
    try:
        if action == "make_road_one_way":
            direction = resolve_opposite_edge(
                net_path,
                str(args.get("keep_edge_id") or ""),
                str(args.get("opposite_edge_id") or "") or None,
            )
            feasibility = check_edge_removal_feasibility(
                net_path, str(direction["opposite_edge"]["edge_id"])
            )
            if not feasibility.get("ok"):
                return [str(feasibility.get("message") or "reverse edge removal has no safe bypass")]
        else:
            target = resolve_edge_target(net_path, str(args.get("edge_id") or ""))
        if action == "remove_edge":
            feasibility = check_edge_removal_feasibility(net_path, str(target["edge_id"]))
            if not feasibility.get("ok"):
                return [str(feasibility.get("message") or "edge removal has no safe bypass")]
        elif action == "set_edge_speed":
            validation = validate_edge_speed_change(net_path, str(target["edge_id"]), args.get("speed_mps"))
            if not validation.get("ok"):
                return list(validation.get("errors") or [])
        elif action == "set_corridor_speed":
            validation = validate_corridor_speed_change(
                net_path,
                str(target["edge_id"]),
                args.get("speed_factor"),
                args.get("edge_count", DEFAULT_CORRIDOR_EDGE_COUNT),
            )
            if not validation.get("ok"):
                return list(validation.get("errors") or [])
    except EdgeTargetError as exc:
        return [str(exc)]
    except Exception as exc:
        return [str(exc)]
    route_path = args.get("route_path")
    cfg_path = args.get("cfg_path")
    if route_path and not os.path.isfile(str(route_path)):
        return [f"route_path not found: {route_path}"]
    if cfg_path and not os.path.isfile(str(cfg_path)):
        return [f"cfg_path not found: {cfg_path}"]
    if cfg_path and not route_path:
        return ["cfg_path requires route_path to create a runnable scenario"]
    return []


def validate_modifier_capability(action: str, args: Dict[str, Any], artifacts: Any) -> Dict[str, Any]:
    if action in KNOWN_UNSUPPORTED_CAPABILITIES:
        cap = KNOWN_UNSUPPORTED_CAPABILITIES[action]
        return {
            "ok": False,
            "error_type": "UNSUPPORTED_CAPABILITY",
            "message": f"Modifier action '{action}' is known but not currently executable.",
            "action": action,
            "missing_tool": cap.get("missing_tool"),
            "capability": cap,
        }

    cap = MODIFIER_CAPABILITIES.get(action)
    if not cap:
        return {
            "ok": False,
            "error_type": "UNKNOWN_MODIFIER_ACTION",
            "message": f"Modifier action '{action}' is not registered.",
            "action": action,
            "available_actions": sorted(MODIFIER_CAPABILITIES.keys()),
            "known_unsupported": sorted(KNOWN_UNSUPPORTED_CAPABILITIES.keys()),
        }

    if not cap.get("supported", False):
        return {
            "ok": False,
            "error_type": "UNSUPPORTED_CAPABILITY",
            "message": f"Modifier action '{action}' is not currently executable.",
            "action": action,
            "missing_tool": cap.get("missing_tool"),
            "capability": cap,
        }

    def artifact_value(name: str) -> Optional[Any]:
        if isinstance(artifacts, dict):
            return artifacts.get(name)
        return getattr(artifacts, name, None)

    missing_artifacts = [
        name for name in cap.get("required_artifacts", [])
        if not artifact_value(name)
    ]
    missing_params = [
        name for name in cap.get("required_params", [])
        if args.get(name) in (None, "")
    ]

    if missing_artifacts or missing_params:
        return {
            "ok": False,
            "error_type": "MISSING_CAPABILITY_REQUIREMENTS",
            "message": f"Modifier action '{action}' is missing required inputs.",
            "action": action,
            "missing_artifacts": missing_artifacts,
            "missing_params": missing_params,
            "capability": cap,
        }

    placeholder_fields = []
    if action in {"remove_edge", "set_edge_speed", "set_corridor_speed"} and _is_placeholder(
        args.get("edge_id")
    ):
        placeholder_fields.append("edge_id")
    if action == "make_road_one_way":
        if _is_placeholder(args.get("keep_edge_id")):
            placeholder_fields.append("keep_edge_id")
        if "opposite_edge_id" in args and _is_placeholder(args.get("opposite_edge_id")):
            placeholder_fields.append("opposite_edge_id")
    if action in {"tls_apply_program", "tls_optimize_and_apply"} and _is_placeholder(args.get("tl_id")):
        placeholder_fields.append("tl_id")
    if action == "tls_set_program" and _is_placeholder(args.get("tls_id")):
        placeholder_fields.append("tls_id")
    if action == "reroute_flows":
        avoid_edges = args.get("avoid_edges")
        if not isinstance(avoid_edges, list) or not avoid_edges:
            placeholder_fields.append("avoid_edges")
        elif any(_is_placeholder(edge_id) for edge_id in avoid_edges):
            placeholder_fields.append("avoid_edges")

    range_errors = _numeric_range_errors(action, args)
    argument_shape_errors = _argument_shape_errors(action, args)
    tls_inventory_errors = _tls_inventory_errors(action, args)
    edge_target_errors = _edge_target_errors(action, args)
    if (
        placeholder_fields
        or range_errors
        or argument_shape_errors
        or tls_inventory_errors
        or edge_target_errors
    ):
        return {
            "ok": False,
            "error_type": "INVALID_CAPABILITY_ARGUMENTS",
            "message": f"Modifier action '{action}' has invalid arguments.",
            "action": action,
            "placeholder_fields": placeholder_fields,
            "range_errors": range_errors,
            "argument_shape_errors": argument_shape_errors,
            "tls_inventory_errors": tls_inventory_errors,
            "edge_target_errors": edge_target_errors,
            "capability": cap,
        }

    allowed = set(cap.get("required_params", [])) | set(cap.get("optional_params", []))
    extra_params = sorted(k for k in args if k not in allowed)
    return {
        "ok": True,
        "action": action,
        "capability": cap,
        "extra_params": extra_params,
    }
