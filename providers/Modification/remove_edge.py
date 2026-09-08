from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from difflib import get_close_matches
from typing import Any, Dict, List, Optional, Set


class EdgeTargetError(ValueError):
    def __init__(self, error_type: str, message: str, candidates: Optional[List[Any]] = None):
        super().__init__(message)
        self.error_type = error_type
        self.candidates = candidates or []


def list_edge_records(net_path: str) -> List[Dict[str, Optional[str]]]:
    net_abs = os.path.abspath(net_path)
    root = ET.parse(net_abs).getroot()
    records: List[Dict[str, Optional[str]]] = []
    for edge in root.findall("edge"):
        edge_id = edge.get("id")
        if not edge_id or edge_id.startswith(":"):
            continue
        records.append(
            {
                "edge_id": edge_id,
                "name": edge.get("name") or None,
                "from_junction": edge.get("from") or None,
                "to_junction": edge.get("to") or None,
            }
        )
    return records


def resolve_edge_target(net_path: str, target: str) -> Dict[str, Any]:
    """Resolve an exact SUMO edge ID first, then a unique exact street name."""
    target_text = str(target or "").strip()
    if not target_text:
        raise EdgeTargetError("EMPTY_EDGE_TARGET", "edge target is empty")
    records = list_edge_records(net_path)

    by_id = {str(record["edge_id"]): record for record in records}
    if target_text in by_id:
        return {**by_id[target_text], "matched_by": "edge_id", "requested_target": target_text}

    name_matches = [record for record in records if record.get("name") == target_text]
    if len(name_matches) == 1:
        return {**name_matches[0], "matched_by": "street_name", "requested_target": target_text}
    if len(name_matches) > 1:
        raise EdgeTargetError(
            "AMBIGUOUS_EDGE_NAME",
            f"Street name '{target_text}' matches {len(name_matches)} SUMO edges; provide one edge_id.",
            candidates=name_matches[:50],
        )

    ids = sorted(by_id)
    names = sorted({str(record["name"]) for record in records if record.get("name")})
    close_ids = get_close_matches(target_text, ids, n=8, cutoff=0.55)
    close_names = get_close_matches(target_text, names, n=8, cutoff=0.55)
    candidates: List[Dict[str, Any]] = []
    for edge_id in close_ids:
        candidates.append(dict(by_id[edge_id]))
    for name in close_names:
        candidates.extend(record for record in records if record.get("name") == name)
    unique = {str(record["edge_id"]): record for record in candidates}
    raise EdgeTargetError(
        "EDGE_NOT_FOUND",
        f"No edge ID or unique street name matched '{target_text}'.",
        candidates=list(unique.values())[:20],
    )


def resolve_opposite_edge(
    net_path: str,
    keep_edge_id: str,
    opposite_edge_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the directed edge opposite to an exact edge that will be kept.

    Direction is determined from reversed junction endpoints.  We deliberately do
    not infer an OSM naming convention (for example, ``-edge_id``), and ambiguous
    parallel reverse edges require an explicit ``opposite_edge_id``.
    """
    records = list_edge_records(net_path)
    by_id = {str(record["edge_id"]): record for record in records}
    keep_text = str(keep_edge_id or "").strip()
    keep = by_id.get(keep_text)
    if keep is None:
        raise EdgeTargetError(
            "KEEP_EDGE_NOT_FOUND",
            f"No exact edge ID matched keep_edge_id '{keep_text}'.",
            candidates=get_close_matches(keep_text, sorted(by_id), n=8, cutoff=0.55),
        )
    if not keep.get("from_junction") or not keep.get("to_junction"):
        raise EdgeTargetError(
            "KEEP_EDGE_DIRECTION_UNDEFINED",
            f"Edge '{keep_text}' has no usable from/to junction direction.",
        )

    reverse_candidates = [
        record
        for record in records
        if record.get("from_junction") == keep.get("to_junction")
        and record.get("to_junction") == keep.get("from_junction")
    ]
    explicit = str(opposite_edge_id or "").strip()
    if explicit:
        opposite = by_id.get(explicit)
        if opposite is None:
            raise EdgeTargetError(
                "OPPOSITE_EDGE_NOT_FOUND",
                f"No exact edge ID matched opposite_edge_id '{explicit}'.",
                candidates=reverse_candidates[:50],
            )
        if opposite not in reverse_candidates:
            raise EdgeTargetError(
                "EDGE_IS_NOT_OPPOSITE",
                f"Edge '{explicit}' does not run opposite to kept edge '{keep_text}'.",
                candidates=reverse_candidates[:50],
            )
    elif not reverse_candidates:
        raise EdgeTargetError(
            "OPPOSITE_EDGE_NOT_FOUND",
            f"No reverse edge was found for kept edge '{keep_text}'.",
        )
    elif len(reverse_candidates) > 1:
        raise EdgeTargetError(
            "AMBIGUOUS_OPPOSITE_EDGE",
            f"Kept edge '{keep_text}' has {len(reverse_candidates)} reverse candidates; "
            "provide opposite_edge_id.",
            candidates=reverse_candidates[:50],
        )
    else:
        opposite = reverse_candidates[0]

    return {
        "keep_edge": dict(keep),
        "opposite_edge": dict(opposite),
        "matched_by": "explicit_edge_id" if explicit else "reversed_junctions",
    }


def _edge_adjacency(net_path: str) -> Dict[str, Set[str]]:
    root = ET.parse(os.path.abspath(net_path)).getroot()
    adjacency: Dict[str, Set[str]] = {}
    for connection in root.findall("connection"):
        source = connection.get("from")
        target = connection.get("to")
        if not source or not target or source.startswith(":") or target.startswith(":"):
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set())
    return adjacency


def _has_path(adjacency: Dict[str, Set[str]], source: str, target: str, removed: str) -> bool:
    if source == removed or target == removed:
        return False
    queue = deque([source])
    visited = {source, removed}
    while queue:
        current = queue.popleft()
        if current == target:
            return True
        for neighbor in adjacency.get(current, set()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return False


def check_edge_removal_feasibility(net_path: str, edge_id: str) -> Dict[str, Any]:
    """Check whether every predecessor-successor pair has a bypass around the edge."""
    adjacency = _edge_adjacency(net_path)
    predecessors = sorted(source for source, targets in adjacency.items() if edge_id in targets)
    successors = sorted(adjacency.get(edge_id, set()))
    blocked_pairs = [
        {"from_edge": source, "to_edge": target}
        for source in predecessors
        for target in successors
        if not _has_path(adjacency, source, target, edge_id)
    ]
    return {
        "ok": not blocked_pairs,
        "edge_id": edge_id,
        "predecessors": predecessors,
        "successors": successors,
        "blocked_pairs": blocked_pairs,
        "message": (
            "All local predecessor-successor movements have an alternative path."
            if not blocked_pairs
            else f"Removing edge '{edge_id}' blocks {len(blocked_pairs)} local movement pair(s)."
        ),
    }


def route_demand_units(route_path: str) -> float:
    """Return a comparable vehicle-equivalent count for route/trip/flow files."""
    root = ET.parse(os.path.abspath(route_path)).getroot()
    total = float(len(root.findall(".//vehicle")) + len(root.findall(".//trip")))
    for flow in root.findall(".//flow"):
        if flow.get("number"):
            total += max(0.0, float(flow.get("number") or 0.0))
            continue
        begin = float(flow.get("begin") or 0.0)
        end = float(flow.get("end") or begin)
        duration = max(0.0, end - begin)
        if flow.get("vehsPerHour"):
            total += duration * max(0.0, float(flow.get("vehsPerHour") or 0.0)) / 3600.0
        elif flow.get("period"):
            period = float(flow.get("period") or 0.0)
            total += duration / period if period > 0 else 0.0
        else:
            total += 1.0
    return total


def rewrite_sumocfg_inputs(cfg_path: str, net_path: str, route_path: str) -> str:
    cfg_abs = os.path.abspath(cfg_path)
    tree = ET.parse(cfg_abs)
    root = tree.getroot()
    input_node = root.find("./input")
    if input_node is None:
        input_node = ET.SubElement(root, "input")
    net_node = input_node.find("./net-file")
    if net_node is None:
        net_node = ET.SubElement(input_node, "net-file")
    route_node = input_node.find("./route-files")
    if route_node is None:
        route_node = ET.SubElement(input_node, "route-files")
    net_node.set("value", os.path.abspath(net_path))
    route_node.set("value", os.path.abspath(route_path))
    tree.write(cfg_abs, encoding="utf-8", xml_declaration=True)
    return cfg_abs


class EdgeRemove:
    """Safe netconvert wrapper retained for compatibility with the modifier provider."""

    def resolve_target(self, net_file_path: str, target: str) -> Dict[str, Any]:
        return resolve_edge_target(net_file_path, target)

    def check_feasibility(self, net_file_path: str, edge_id: str) -> Dict[str, Any]:
        return check_edge_removal_feasibility(net_file_path, edge_id)

    def remove_edge_or_lane(
        self,
        net_file_path: str,
        edge_name: str,
        remove_lane: bool = False,
        lane_index: Optional[int] = None,
        out_dir: str = "./runs",
        output_net_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        if remove_lane:
            raise NotImplementedError("lane removal is not supported by remove_edge")
        target = self.resolve_target(net_file_path, edge_name)
        net_abs = os.path.abspath(net_file_path)
        out_abs = os.path.abspath(out_dir)
        os.makedirs(out_abs, exist_ok=True)
        if output_net_path:
            output_abs = os.path.abspath(output_net_path)
        else:
            base = os.path.basename(net_abs)
            prefix = base[:-8] if base.endswith(".net.xml") else os.path.splitext(base)[0]
            output_abs = os.path.join(out_abs, f"{prefix}.edge_removed.net.xml")
        if output_abs == net_abs:
            raise ValueError("output network must differ from the source network")

        log_path = os.path.join(out_abs, "remove_edge_netconvert.log")
        command = [
            "netconvert",
            "--sumo-net-file",
            net_abs,
            "--remove-edges.explicit",
            str(target["edge_id"]),
            "-o",
            output_abs,
        ]
        with open(log_path, "w", encoding="utf-8") as log_file:
            subprocess.run(command, check=True, stdout=log_file, stderr=log_file)
        remaining_ids = {str(record["edge_id"]) for record in list_edge_records(output_abs)}
        if str(target["edge_id"]) in remaining_ids:
            raise RuntimeError(f"netconvert did not remove edge '{target['edge_id']}'")
        return {
            "net": output_abs,
            "target": target,
            "log": os.path.abspath(log_path),
            "command": command,
        }
