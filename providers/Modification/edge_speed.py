from __future__ import annotations

import os
import math
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .remove_edge import resolve_edge_target


MIN_SPEED_MPS = 5.0
MAX_SPEED_MPS = 25.0
MAX_RELATIVE_CHANGE = 0.30
MIN_CORRIDOR_EDGE_COUNT = 2
MAX_CORRIDOR_EDGE_COUNT = 10
DEFAULT_CORRIDOR_EDGE_COUNT = 5
MIN_CORRIDOR_SPEED_FACTOR = 0.70
MAX_CORRIDOR_SPEED_FACTOR = 0.95


def edge_speed_inventory(net_path: str, edge_id: str) -> Dict[str, Any]:
    """Resolve one edge and return the speed of each of its lanes."""
    net_abs = os.path.abspath(net_path)
    target = resolve_edge_target(net_abs, edge_id)
    root = ET.parse(net_abs).getroot()
    edge = next((node for node in root.findall("edge") if node.get("id") == target["edge_id"]), None)
    if edge is None:
        raise ValueError(f"Edge '{target['edge_id']}' was not found in {net_abs}")
    lanes = edge.findall("lane")
    if not lanes:
        raise ValueError(f"Edge '{target['edge_id']}' has no lanes")
    speeds: List[float] = []
    for lane in lanes:
        raw = lane.get("speed")
        if raw in (None, ""):
            raise ValueError(f"Lane '{lane.get('id')}' has no speed")
        speeds.append(float(raw))
    return {**target, "lane_count": len(lanes), "lane_speeds_mps": speeds}


def validate_edge_speed_change(net_path: str, edge_id: str, speed_mps: float) -> Dict[str, Any]:
    inventory = edge_speed_inventory(net_path, edge_id)
    requested = float(speed_mps)
    errors: List[str] = []
    if not MIN_SPEED_MPS <= requested <= MAX_SPEED_MPS:
        errors.append(f"speed_mps must be in [{MIN_SPEED_MPS}, {MAX_SPEED_MPS}]")
    for current in inventory["lane_speeds_mps"]:
        lower = current * (1.0 - MAX_RELATIVE_CHANGE)
        upper = current * (1.0 + MAX_RELATIVE_CHANGE)
        if not lower <= requested <= upper:
            errors.append(
                f"speed_mps {requested:g} changes lane speed {current:g} by more than "
                f"{MAX_RELATIVE_CHANGE:.0%}; allowed range is [{lower:.3f}, {upper:.3f}]"
            )
            break
    if all(abs(requested - current) < 1e-9 for current in inventory["lane_speeds_mps"]):
        errors.append("speed_mps must change the existing lane speed")
    return {"ok": not errors, "errors": errors, "target": inventory, "speed_mps": requested}


def write_edge_speed_network(net_path: str, edge_id: str, speed_mps: float, out_dir: str) -> Dict[str, Any]:
    """Write an isolated SUMO network with all lanes of one edge set to one speed."""
    net_abs = os.path.abspath(net_path)
    validation = validate_edge_speed_change(net_abs, edge_id, speed_mps)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))
    target_id = str(validation["target"]["edge_id"])
    tree = ET.parse(net_abs)
    edge = next(node for node in tree.getroot().findall("edge") if node.get("id") == target_id)
    for lane in edge.findall("lane"):
        lane.set("speed", f"{float(speed_mps):.6g}")

    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    base = os.path.basename(net_abs)
    prefix = base[:-8] if base.endswith(".net.xml") else os.path.splitext(base)[0]
    output = os.path.join(out_abs, f"{prefix}.speed_modified.net.xml")
    if os.path.abspath(output) == net_abs:
        raise ValueError("output network must differ from the source network")
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return {
        "net": output,
        "target": validation["target"],
        "old_speeds_mps": validation["target"]["lane_speeds_mps"],
        "new_speed_mps": float(speed_mps),
    }


def _road_key(edge: ET.Element) -> Tuple[str, str]:
    name = " ".join(str(edge.get("name") or "").split()).casefold()
    if name:
        return ("name", name)
    edge_id = str(edge.get("id") or "").lstrip("-")
    return ("segment", edge_id.split("#", 1)[0])


def _edge_shape(edge: ET.Element) -> List[Tuple[float, float]]:
    raw = edge.get("shape")
    if not raw:
        lane = edge.find("lane")
        raw = lane.get("shape") if lane is not None else None
    points: List[Tuple[float, float]] = []
    for item in str(raw or "").split():
        try:
            x, y = item.split(",")[:2]
            points.append((float(x), float(y)))
        except (TypeError, ValueError):
            continue
    return points


def _direction(edge: ET.Element) -> Optional[Tuple[float, float]]:
    points = _edge_shape(edge)
    if len(points) < 2:
        return None
    dx = points[-1][0] - points[0][0]
    dy = points[-1][1] - points[0][1]
    norm = math.hypot(dx, dy)
    return (dx / norm, dy / norm) if norm > 0 else None


def _continuation_score(current: ET.Element, candidate: ET.Element) -> Tuple[float, int, float, str]:
    current_direction = _direction(current)
    candidate_direction = _direction(candidate)
    straightness = -2.0
    if current_direction and candidate_direction:
        straightness = (
            current_direction[0] * candidate_direction[0]
            + current_direction[1] * candidate_direction[1]
        )
    lane_count = len(candidate.findall("lane"))
    length = sum(float(lane.get("length") or 0.0) for lane in candidate.findall("lane"))
    return (straightness, lane_count, length, str(candidate.get("id") or ""))


def corridor_speed_inventory(
    net_path: str,
    edge_id: str,
    edge_count: int = DEFAULT_CORRIDOR_EDGE_COUNT,
) -> Dict[str, Any]:
    """Resolve a directed, contiguous road corridor around one evidenced anchor edge."""
    requested_count = int(edge_count)
    if not MIN_CORRIDOR_EDGE_COUNT <= requested_count <= MAX_CORRIDOR_EDGE_COUNT:
        raise ValueError(
            f"edge_count must be in [{MIN_CORRIDOR_EDGE_COUNT}, {MAX_CORRIDOR_EDGE_COUNT}]"
        )

    net_abs = os.path.abspath(net_path)
    target = resolve_edge_target(net_abs, edge_id)
    root = ET.parse(net_abs).getroot()
    edges = {
        str(edge.get("id")): edge
        for edge in root.findall("edge")
        if edge.get("id") and not str(edge.get("id")).startswith(":")
    }
    anchor = edges.get(str(target["edge_id"]))
    if anchor is None:
        raise ValueError(f"Edge '{target['edge_id']}' was not found in {net_abs}")
    key = _road_key(anchor)
    eligible = [edge for edge in edges.values() if _road_key(edge) == key]

    upstream: List[ET.Element] = []
    downstream: List[ET.Element] = []
    selected = {str(anchor.get("id"))}
    up_cursor = anchor
    down_cursor = anchor
    prefer_downstream = True
    while len(selected) < requested_count:
        directions = ("down", "up") if prefer_downstream else ("up", "down")
        added = False
        for direction in directions:
            cursor = down_cursor if direction == "down" else up_cursor
            junction = cursor.get("to") if direction == "down" else cursor.get("from")
            if not junction:
                continue
            if direction == "down":
                candidates = [
                    edge for edge in eligible
                    if edge.get("from") == junction
                    and edge.get("to") != cursor.get("from")
                    and str(edge.get("id")) not in selected
                ]
            else:
                candidates = [
                    edge for edge in eligible
                    if edge.get("to") == junction
                    and edge.get("from") != cursor.get("to")
                    and str(edge.get("id")) not in selected
                ]
            if not candidates:
                continue
            candidate = max(candidates, key=lambda edge: _continuation_score(cursor, edge))
            selected.add(str(candidate.get("id")))
            if direction == "down":
                downstream.append(candidate)
                down_cursor = candidate
            else:
                upstream.append(candidate)
                up_cursor = candidate
            prefer_downstream = not prefer_downstream
            added = True
            break
        if not added:
            break

    corridor = list(reversed(upstream)) + [anchor] + downstream
    rows: List[Dict[str, Any]] = []
    for edge in corridor:
        lanes = edge.findall("lane")
        if not lanes:
            raise ValueError(f"Edge '{edge.get('id')}' has no lanes")
        speeds: List[float] = []
        lengths: List[float] = []
        for lane in lanes:
            if lane.get("speed") in (None, ""):
                raise ValueError(f"Lane '{lane.get('id')}' has no speed")
            speeds.append(float(lane.get("speed")))
            lengths.append(float(lane.get("length") or 0.0))
        rows.append(
            {
                "edge_id": str(edge.get("id")),
                "name": str(edge.get("name") or ""),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "lane_count": len(lanes),
                "lane_speeds_mps": speeds,
                "length_m": max(lengths, default=0.0),
            }
        )
    if len(rows) < MIN_CORRIDOR_EDGE_COUNT:
        raise ValueError(
            f"Anchor edge '{target['edge_id']}' has no contiguous same-road corridor "
            f"with at least {MIN_CORRIDOR_EDGE_COUNT} edges"
        )
    return {
        "anchor": target,
        "road_key": {"kind": key[0], "value": key[1]},
        "requested_edge_count": requested_count,
        "resolved_edge_count": len(rows),
        "edge_ids": [row["edge_id"] for row in rows],
        "total_length_m": sum(row["length_m"] for row in rows),
        "edges": rows,
    }


def validate_corridor_speed_change(
    net_path: str,
    edge_id: str,
    speed_factor: float,
    edge_count: int = DEFAULT_CORRIDOR_EDGE_COUNT,
) -> Dict[str, Any]:
    inventory = corridor_speed_inventory(net_path, edge_id, edge_count)
    factor = float(speed_factor)
    errors: List[str] = []
    if not MIN_CORRIDOR_SPEED_FACTOR <= factor <= MAX_CORRIDOR_SPEED_FACTOR:
        errors.append(
            f"speed_factor must be in [{MIN_CORRIDOR_SPEED_FACTOR}, {MAX_CORRIDOR_SPEED_FACTOR}]"
        )
    for edge in inventory["edges"]:
        for current in edge["lane_speeds_mps"]:
            new_speed = current * factor
            if new_speed < MIN_SPEED_MPS:
                errors.append(
                    f"speed_factor {factor:g} would lower a lane on edge '{edge['edge_id']}' "
                    f"below {MIN_SPEED_MPS:g} m/s"
                )
                break
        if errors:
            break
    return {
        "ok": not errors,
        "errors": errors,
        "corridor": inventory,
        "speed_factor": factor,
    }


def write_corridor_speed_network(
    net_path: str,
    edge_id: str,
    speed_factor: float,
    out_dir: str,
    edge_count: int = DEFAULT_CORRIDOR_EDGE_COUNT,
) -> Dict[str, Any]:
    """Write an isolated network with proportional speed control on a road corridor."""
    net_abs = os.path.abspath(net_path)
    validation = validate_corridor_speed_change(net_abs, edge_id, speed_factor, edge_count)
    if not validation["ok"]:
        raise ValueError("; ".join(validation["errors"]))

    corridor = validation["corridor"]
    factor = float(validation["speed_factor"])
    tree = ET.parse(net_abs)
    by_id = {
        str(edge.get("id")): edge
        for edge in tree.getroot().findall("edge")
        if edge.get("id")
    }
    changes: List[Dict[str, Any]] = []
    for row in corridor["edges"]:
        edge = by_id[row["edge_id"]]
        old_speeds = list(row["lane_speeds_mps"])
        new_speeds = [speed * factor for speed in old_speeds]
        for lane, new_speed in zip(edge.findall("lane"), new_speeds):
            lane.set("speed", f"{new_speed:.6g}")
        changes.append(
            {
                "edge_id": row["edge_id"],
                "old_speeds_mps": old_speeds,
                "new_speeds_mps": new_speeds,
            }
        )

    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    base = os.path.basename(net_abs)
    prefix = base[:-8] if base.endswith(".net.xml") else os.path.splitext(base)[0]
    output = os.path.join(out_abs, f"{prefix}.corridor_speed_modified.net.xml")
    if os.path.abspath(output) == net_abs:
        raise ValueError("output network must differ from the source network")
    tree.write(output, encoding="utf-8", xml_declaration=True)
    return {
        "net": output,
        "anchor": corridor["anchor"],
        "corridor": corridor,
        "speed_factor": factor,
        "changes": changes,
    }
