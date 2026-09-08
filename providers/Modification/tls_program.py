from __future__ import annotations

import copy
import os
import re
import shutil
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


_GREEN = {"G", "g"}
_YELLOW = {"Y", "y"}


def split_sumo_file_list(value: Optional[str]) -> List[str]:
    """Split SUMO file-list attributes, which commonly use commas or whitespace."""
    return [item for item in re.split(r"[,\s]+", (value or "").strip()) if item]


def resolve_cfg_reference(cfg_path: str, value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value if os.path.isabs(value) else os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(cfg_path)), value)
    )


def read_sumocfg_paths(cfg_path: str) -> Dict[str, Any]:
    cfg_abs = os.path.abspath(cfg_path)
    root = ET.parse(cfg_abs).getroot()

    def values(tag: str) -> List[str]:
        node = root.find(f"./input/{tag}")
        if node is None:
            node = root.find(f".//{tag}")
        return split_sumo_file_list(node.get("value") if node is not None else None)

    net_values = values("net-file")
    route_values = values("route-files")
    additional_values = values("additional-files")
    tripinfo_values = values("tripinfo-output")
    return {
        "cfg": cfg_abs,
        "net": resolve_cfg_reference(cfg_abs, net_values[0]) if net_values else None,
        "routes": resolve_cfg_reference(cfg_abs, route_values[0]) if route_values else None,
        "route_files": [resolve_cfg_reference(cfg_abs, x) for x in route_values],
        "additional_files": [resolve_cfg_reference(cfg_abs, x) for x in additional_values],
        "tripinfo": resolve_cfg_reference(cfg_abs, tripinfo_values[0]) if tripinfo_values else None,
    }


def _phase_is_adjustable(state: str) -> bool:
    # Transitional phases can still contain a permissive green. Keep them fixed.
    return any(ch in _GREEN for ch in state) and not any(ch in _YELLOW for ch in state)


def load_tls_inventory(
    net_path: str,
    tl_id: str,
    *,
    program_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Read an existing TLS program and map each green phase to incoming edges."""
    net_abs = os.path.abspath(net_path)
    root = ET.parse(net_abs).getroot()
    programs = [node for node in root.findall("tlLogic") if node.get("id") == tl_id]
    if program_id is not None:
        programs = [node for node in programs if node.get("programID") == program_id]
    if not programs:
        available = sorted({node.get("id") for node in root.findall("tlLogic") if node.get("id")})
        raise ValueError(
            f"TLS id '{tl_id}' was not found in {net_abs}. "
            f"Available TLS count: {len(available)}"
        )

    program = programs[0]
    phases: List[Dict[str, Any]] = []
    for index, phase in enumerate(program.findall("phase")):
        attrs = dict(phase.attrib)
        state = attrs.get("state", "")
        try:
            duration = float(attrs.get("duration", "0"))
        except ValueError as exc:
            raise ValueError(f"TLS '{tl_id}' phase {index} has invalid duration") from exc
        phases.append(
            {
                "index": index,
                "duration": duration,
                "state": state,
                "adjustable_green": _phase_is_adjustable(state),
                "attributes": attrs,
            }
        )
    if not phases:
        raise ValueError(f"TLS '{tl_id}' has no phases")

    connections: List[Dict[str, Any]] = []
    for conn in root.findall("connection"):
        if conn.get("tl") != tl_id:
            continue
        try:
            link_index = int(conn.get("linkIndex", ""))
        except ValueError:
            continue
        connections.append(
            {
                "from_edge": conn.get("from"),
                "to_edge": conn.get("to"),
                "from_lane": conn.get("fromLane"),
                "to_lane": conn.get("toLane"),
                "link_index": link_index,
            }
        )

    green_phase_indices: List[int] = []
    phase_edge_groups: List[List[str]] = []
    for phase in phases:
        if not phase["adjustable_green"]:
            continue
        state = str(phase["state"])
        edges = sorted(
            {
                str(conn["from_edge"])
                for conn in connections
                if conn.get("from_edge")
                and int(conn["link_index"]) < len(state)
                and state[int(conn["link_index"])] in _GREEN
            }
        )
        green_phase_indices.append(int(phase["index"]))
        phase_edge_groups.append(edges)

    return {
        "net_path": net_abs,
        "tl_id": tl_id,
        "program_id": program.get("programID") or "0",
        "type": program.get("type") or "static",
        "offset": float(program.get("offset") or 0.0),
        "cycle": sum(float(phase["duration"]) for phase in phases),
        "phases": phases,
        "connections": connections,
        "green_phase_indices": green_phase_indices,
        "phase_edge_groups": phase_edge_groups,
        "optimizable": len(green_phase_indices) >= 2,
    }


def normalize_green_shares(demands: Sequence[float], min_split: float) -> List[float]:
    vals = [max(0.0, float(value)) for value in demands]
    if not vals or sum(vals) <= 0:
        raise ValueError("phase demands must contain at least one positive value")
    floor = float(min_split)
    if floor < 0 or floor * len(vals) >= 1.0:
        raise ValueError("min_split must be >= 0 and min_split * phase_count must be < 1")
    total = sum(vals)
    residual = 1.0 - floor * len(vals)
    return [floor + residual * (value / total) for value in vals]


def _flow_weight(node: ET.Element) -> float:
    if node.tag != "flow":
        return 1.0
    if node.get("number"):
        return max(0.0, float(node.get("number") or 0.0))
    begin = float(node.get("begin") or 0.0)
    end = float(node.get("end") or begin)
    duration = max(0.0, end - begin)
    if node.get("vehsPerHour"):
        return duration * max(0.0, float(node.get("vehsPerHour") or 0.0)) / 3600.0
    if node.get("period"):
        period = float(node.get("period") or 0.0)
        return duration / period if period > 0 else 0.0
    return 1.0


def estimate_phase_demands_from_routes(
    route_path: str,
    phase_edge_groups: Sequence[Sequence[str]],
) -> List[float]:
    """Estimate approach demand from explicit routes, route refs, trips, and flows."""
    route_abs = os.path.abspath(route_path)
    if not os.path.isfile(route_abs):
        raise FileNotFoundError(f"route_path not found: {route_abs}")
    groups = [set(group) for group in phase_edge_groups]
    if not groups:
        return []

    root = ET.parse(route_abs).getroot()
    route_defs = {
        str(route.get("id")): (route.get("edges") or "").split()
        for route in root.findall("./route")
        if route.get("id")
    }
    counts = [0.0 for _ in groups]
    for node in list(root.findall(".//vehicle")) + list(root.findall(".//trip")) + list(root.findall(".//flow")):
        edges: List[str] = []
        inline_route = node.find("./route")
        if inline_route is not None:
            edges = (inline_route.get("edges") or "").split()
        elif node.get("route"):
            edges = list(route_defs.get(str(node.get("route")), []))
        else:
            edges = [
                value
                for value in [node.get("from"), *((node.get("via") or "").split()), node.get("to")]
                if value
            ]
        edge_set = set(edges)
        weight = _flow_weight(node)
        for index, group in enumerate(groups):
            if edge_set.intersection(group):
                counts[index] += weight
                break
    return counts


def build_optimized_tllogic(
    inventory: Mapping[str, Any],
    *,
    demands: Optional[Sequence[float]] = None,
    cycle: Optional[float] = None,
    min_split: float = 0.08,
    offset: Optional[float] = None,
    program_id: str = "opt",
) -> Tuple[ET.Element, Dict[str, Any]]:
    phases = [copy.deepcopy(dict(phase)) for phase in inventory.get("phases") or []]
    green_indices = [int(index) for index in inventory.get("green_phase_indices") or []]
    if len(green_indices) < 2:
        raise ValueError(
            f"TLS '{inventory.get('tl_id')}' has fewer than two adjustable green phases"
        )

    target_cycle = float(cycle if cycle is not None else inventory.get("cycle") or 0.0)
    fixed_time = sum(
        float(phase["duration"])
        for phase in phases
        if int(phase["index"]) not in green_indices
    )
    available_green = target_cycle - fixed_time
    if available_green <= 0:
        raise ValueError(
            f"cycle={target_cycle} is not larger than fixed intergreen time={fixed_time}"
        )

    if demands is None:
        demands = [float(phases[index]["duration"]) for index in green_indices]
    if len(demands) != len(green_indices):
        raise ValueError(
            f"demands count {len(demands)} does not match adjustable green phase count {len(green_indices)}"
        )
    shares = normalize_green_shares(demands, min_split)
    durations = [round(available_green * share, 3) for share in shares]
    durations[-1] = round(available_green - sum(durations[:-1]), 3)

    tl_logic = ET.Element(
        "tlLogic",
        {
            "id": str(inventory["tl_id"]),
            "type": str(inventory.get("type") or "static"),
            "programID": str(program_id),
            "offset": _format_number(offset if offset is not None else inventory.get("offset") or 0.0),
        },
    )
    duration_by_index = dict(zip(green_indices, durations))
    for phase in phases:
        attrs = dict(phase.get("attributes") or {})
        index = int(phase["index"])
        duration = duration_by_index.get(index, float(phase["duration"]))
        attrs["duration"] = _format_number(duration)
        attrs["state"] = str(phase["state"])
        ET.SubElement(tl_logic, "phase", attrs)

    validate_tllogic_phase_durations(tl_logic, tls_id=str(inventory.get("tl_id") or ""))

    return tl_logic, {
        "method": "proportional_green_split_preserving_phase_states",
        "source_program_id": inventory.get("program_id"),
        "program_id": program_id,
        "cycle": target_cycle,
        "fixed_intergreen_time": fixed_time,
        "available_green_time": available_green,
        "green_phase_indices": green_indices,
        "phase_edge_groups": inventory.get("phase_edge_groups") or [],
        "demands": [float(value) for value in demands],
        "splits": shares,
        "green_durations": durations,
    }


def _format_number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".")


def validate_tllogic_phase_durations(
    tl_logic: ET.Element,
    *,
    tls_id: Optional[str] = None,
) -> None:
    """Reject TLS programs that SUMO cannot execute because a phase is non-positive."""
    phases = list(tl_logic.findall("phase"))
    resolved_id = str(tls_id or tl_logic.get("id") or "<unknown>")
    if not phases:
        raise ValueError(f"TLS '{resolved_id}' has no phases")
    for index, phase in enumerate(phases):
        raw = phase.get("duration")
        try:
            duration = float(raw or "")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"TLS '{resolved_id}' phase {index} has invalid duration: {raw!r}"
            ) from exc
        if duration <= 0:
            raise ValueError(
                f"TLS '{resolved_id}' phase {index} duration must be > 0, got {duration:g}"
            )


def write_tls_additional(add_path: str, tl_logic: ET.Element) -> str:
    add_abs = os.path.abspath(add_path)
    os.makedirs(os.path.dirname(add_abs), exist_ok=True)
    validate_tllogic_phase_durations(tl_logic)
    root = ET.Element("additional")
    root.append(copy.deepcopy(tl_logic))
    ET.ElementTree(root).write(add_abs, encoding="utf-8", xml_declaration=True)
    return add_abs


def attach_additional_file(cfg_path: str, add_path: str) -> str:
    """Attach an additional file under SUMO's <configuration><input> section."""
    cfg_abs = os.path.abspath(cfg_path)
    add_abs = os.path.abspath(add_path)
    tree = ET.parse(cfg_abs)
    root = tree.getroot()
    input_node = root.find("./input")
    if input_node is None:
        input_node = ET.SubElement(root, "input")
    add_node = input_node.find("./additional-files")
    if add_node is None:
        add_node = ET.SubElement(input_node, "additional-files")

    existing = split_sumo_file_list(add_node.get("value"))
    rel = os.path.relpath(add_abs, os.path.dirname(cfg_abs))
    replaced = False
    for index, value in enumerate(existing):
        resolved = os.path.abspath(resolve_cfg_reference(cfg_abs, value) or value)
        if resolved == add_abs:
            replaced = True
            break
        if os.path.basename(resolved) == os.path.basename(add_abs):
            existing[index] = rel
            replaced = True
            break
    if not replaced:
        existing.append(rel)
    add_node.set("value", ",".join(existing))
    tree.write(cfg_abs, encoding="utf-8", xml_declaration=True)
    return add_abs


def clone_cfg_for_run(cfg_path: str, out_dir: str) -> str:
    """Copy a cfg for a new scenario and redirect run outputs into that scenario."""
    cfg_abs = os.path.abspath(cfg_path)
    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    cfg_out = os.path.join(out_abs, os.path.basename(cfg_abs) or "sim.sumocfg")
    if cfg_abs == os.path.abspath(cfg_out):
        cfg_out = os.path.join(out_abs, f"candidate.{os.path.basename(cfg_abs) or 'sim.sumocfg'}")
    shutil.copy2(cfg_abs, cfg_out)

    tree = ET.parse(cfg_out)
    root = tree.getroot()
    report = root.find("./report")
    if report is None:
        report = ET.SubElement(root, "report")
    tripinfo = report.find("./tripinfo-output")
    if tripinfo is None:
        tripinfo = ET.SubElement(report, "tripinfo-output")
    tripinfo.set("value", os.path.join(out_abs, "tripinfo.xml"))

    input_node = root.find("./input")
    add_node = input_node.find("./additional-files") if input_node is not None else None
    if add_node is not None:
        rewritten: List[str] = []
        for index, value in enumerate(split_sumo_file_list(add_node.get("value"))):
            source = resolve_cfg_reference(cfg_abs, value)
            cloned = _clone_output_additional(source, out_abs, index)
            rewritten.append(cloned or str(source or value))
        add_node.set("value", ",".join(rewritten))
    tree.write(cfg_out, encoding="utf-8", xml_declaration=True)
    return cfg_out


def _clone_output_additional(source: Optional[str], out_dir: str, index: int) -> Optional[str]:
    if not source or not os.path.isfile(source):
        return source
    try:
        tree = ET.parse(source)
    except ET.ParseError:
        return source
    output_nodes = list(tree.getroot().iter("edgeData"))
    if not output_nodes:
        return source
    name = os.path.basename(source) or f"additional_{index}.xml"
    target = os.path.abspath(os.path.join(out_dir, name))
    if os.path.abspath(source) == target:
        name = f"cloned_{index}_{name}"
        target = os.path.abspath(os.path.join(out_dir, name))
    for node_index, node in enumerate(output_nodes):
        suffix = "" if node_index == 0 else f"_{node_index}"
        node.set("file", os.path.join(out_dir, f"edgedata{suffix}.xml"))
    tree.write(target, encoding="utf-8", xml_declaration=True)
    return target
