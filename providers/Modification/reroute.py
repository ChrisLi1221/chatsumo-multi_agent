from __future__ import annotations

import copy
import hashlib
import math
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def write_rerouted_sumocfg(source_cfg: str, out_dir: str, net_path: str, route_path: str) -> str:
    """Preserve simulation settings and additional inputs when changing routes."""
    from .tls_program import clone_cfg_for_run, attach_additional_file, split_sumo_file_list
    from .remove_edge import rewrite_sumocfg_inputs
    cfg = clone_cfg_for_run(source_cfg, out_dir)
    rewrite_sumocfg_inputs(cfg, net_path, route_path)
    tree = ET.parse(cfg); root = tree.getroot()
    add = root.find('./input/additional-files')
    paths = split_sumo_file_list(add.get('value')) if add is not None else []
    has_edges = False
    for path in paths:
        path = path if os.path.isabs(path) else os.path.join(os.path.dirname(cfg), path)
        if os.path.isfile(path) and ET.parse(path).find('.//edgeData') is not None:
            has_edges = True
    if root.find('.//device.emissions.probability') is None:
        report = root.find('report')
        if report is None: report = ET.SubElement(root, 'report')
        ET.SubElement(report, 'device.emissions.probability', {'value': '1.0'})
    tree.write(cfg, encoding='utf-8', xml_declaration=True)
    if not has_edges:
        additional = ET.Element('additional')
        ET.SubElement(additional, 'edgeData', {'id':'edge_performance','period':'60',
            'file':os.path.abspath(os.path.join(out_dir,'edgedata.xml')),'excludeEmpty':'true'})
        path = os.path.abspath(os.path.join(out_dir,'edge_data.add.xml'))
        ET.ElementTree(additional).write(path, encoding='utf-8', xml_declaration=True)
        attach_additional_file(cfg,path)
    return cfg


def write_soft_avoid_weights(
    path: str,
    avoid_edges: Iterable[str],
    penalty_s: float,
) -> str:
    """Write partial duarouter edge weights without changing network topology."""
    penalty = float(penalty_s)
    if penalty <= 0:
        raise ValueError("avoid_penalty_s must be > 0")

    edge_ids = list(dict.fromkeys(str(edge_id) for edge_id in avoid_edges if edge_id))
    if not edge_ids:
        raise ValueError("avoid_edges must be a non-empty list")

    output = os.path.abspath(path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    root = ET.Element("meandata")
    interval = ET.SubElement(
        root,
        "interval",
        {"id": "soft_avoid", "begin": "0", "end": "1000000000"},
    )
    for edge_id in edge_ids:
        ET.SubElement(
            interval,
            "edge",
            {"id": edge_id, "traveltime": f"{penalty:g}"},
        )
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    return output


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _split_edges(value: Optional[str]) -> List[str]:
    return [edge for edge in str(value or "").split() if edge]


def _route_definitions(root: ET.Element) -> Dict[str, List[str]]:
    definitions: Dict[str, List[str]] = {}
    for element in root:
        if _local_name(element.tag) != "route":
            continue
        route_id = element.get("id")
        edges = _split_edges(element.get("edges"))
        if route_id and edges:
            definitions[route_id] = edges
    return definitions


def _element_edges(element: ET.Element, definitions: Mapping[str, List[str]]) -> List[str]:
    for child in element:
        if _local_name(child.tag) == "route":
            edges = _split_edges(child.get("edges"))
            if edges:
                return edges
    route_ref = element.get("route")
    if route_ref and route_ref in definitions:
        return list(definitions[route_ref])
    return _split_edges(element.get("edges"))


def read_route_records(route_path: str) -> Dict[str, Any]:
    """Read exact trip/vehicle endpoints and realized route edges from a route file."""
    root = ET.parse(route_path).getroot()
    definitions = _route_definitions(root)
    records: Dict[str, Dict[str, Any]] = {}
    flow_count = 0
    duplicate_ids: List[str] = []

    for element in root:
        kind = _local_name(element.tag)
        if kind == "flow":
            flow_count += 1
            continue
        if kind not in {"trip", "vehicle"}:
            continue
        record_id = element.get("id")
        if not record_id:
            continue
        edges = _element_edges(element, definitions)
        from_edge = element.get("from") or (edges[0] if edges else None)
        to_edge = element.get("to") or (edges[-1] if edges else None)
        if record_id in records:
            duplicate_ids.append(record_id)
        records[record_id] = {
            "id": record_id,
            "kind": kind,
            "from_edge": from_edge,
            "to_edge": to_edge,
            "edges": edges,
        }

    return {
        "records": records,
        "flow_count": flow_count,
        "duplicate_ids": sorted(set(duplicate_ids)),
    }


def validate_od_preservation(source_path: str, output_path: str) -> Dict[str, Any]:
    """Verify that rerouting keeps every exact demand record and its edge-level OD."""
    source = read_route_records(source_path)
    output = read_route_records(output_path)
    source_records = source["records"]
    output_records = output["records"]

    missing_ids = sorted(set(source_records) - set(output_records))
    extra_ids = sorted(set(output_records) - set(source_records))
    endpoint_mismatches: List[Dict[str, Any]] = []
    unverifiable_source_ids: List[str] = []

    for record_id, source_record in source_records.items():
        source_from = source_record.get("from_edge")
        source_to = source_record.get("to_edge")
        if not source_from or not source_to:
            unverifiable_source_ids.append(record_id)
            continue
        output_record = output_records.get(record_id)
        if not output_record:
            continue
        output_from = output_record.get("from_edge")
        output_to = output_record.get("to_edge")
        if source_from != output_from or source_to != output_to:
            endpoint_mismatches.append(
                {
                    "id": record_id,
                    "source_from": source_from,
                    "source_to": source_to,
                    "output_from": output_from,
                    "output_to": output_to,
                }
            )

    # Expanded flow IDs are expected to differ from the source flow ID. Exact
    # trip/vehicle records are still checked; aggregate flow demand is checked by
    # the caller with route_demand_units.
    unexpected_ids = [] if source["flow_count"] else extra_ids
    ok = not (
        missing_ids
        or unexpected_ids
        or endpoint_mismatches
        or unverifiable_source_ids
        or source["duplicate_ids"]
        or output["duplicate_ids"]
    )
    return {
        "available": True,
        "ok": ok,
        "source_record_count": len(source_records),
        "output_record_count": len(output_records),
        "source_flow_count": source["flow_count"],
        "missing_ids": missing_ids,
        "extra_ids": extra_ids,
        "endpoint_mismatches": endpoint_mismatches,
        "unverifiable_source_ids": sorted(unverifiable_source_ids),
        "duplicate_source_ids": source["duplicate_ids"],
        "duplicate_output_ids": output["duplicate_ids"],
    }


def avoided_edge_usage(route_path: str, avoid_edges: Iterable[str]) -> Dict[str, Any]:
    """Count realized routes that still require each softly avoided edge."""
    targets = list(dict.fromkeys(str(edge_id) for edge_id in avoid_edges if edge_id))
    records = read_route_records(route_path)["records"]
    counts = {edge_id: 0 for edge_id in targets}
    vehicles_using_any = 0
    for record in records.values():
        used = set(record.get("edges") or [])
        matched = False
        for edge_id in targets:
            if edge_id in used:
                counts[edge_id] += 1
                matched = True
        if matched:
            vehicles_using_any += 1
    return {
        "edge_vehicle_counts": counts,
        "vehicles_using_any_avoid_edge": vehicles_using_any,
        "realized_route_count": len(records),
    }


def select_eligible_vehicle_ids(
    reference_route_path: str,
    target_edges: Iterable[str] | str,
    reroute_fraction: float,
) -> Dict[str, Any]:
    """Sample the union of vehicles using any of one to three target edges."""
    fraction = float(reroute_fraction)
    if fraction not in {0.25, 0.5, 0.75, 1.0}:
        raise ValueError("reroute_fraction must be one of 0.25, 0.5, 0.75, 1.0")
    if isinstance(target_edges, str):
        targets = [target_edges] if target_edges else []
    else:
        targets = list(
            dict.fromkeys(str(edge_id) for edge_id in target_edges if edge_id)
        )
    if not 1 <= len(targets) <= 3:
        raise ValueError("target_edges must contain between one and three edge IDs")

    records = read_route_records(reference_route_path)["records"]
    target_set = set(targets)
    eligible_ids: List[str] = []
    eligible_edge_vehicle_counts = {edge_id: 0 for edge_id in targets}
    vehicles_using_multiple_target_edges = 0
    for record_id, record in records.items():
        matched = target_set & set(record.get("edges") or [])
        if not matched:
            continue
        eligible_ids.append(record_id)
        for edge_id in matched:
            eligible_edge_vehicle_counts[edge_id] += 1
        if len(matched) > 1:
            vehicles_using_multiple_target_edges += 1

    target_signature = "|".join(sorted(targets))
    ranked_ids = sorted(
        eligible_ids,
        key=lambda record_id: hashlib.sha256(
            f"{target_signature}|{record_id}".encode("utf-8")
        ).hexdigest(),
    )
    selected_count = min(
        len(ranked_ids),
        int(math.ceil(len(ranked_ids) * fraction - 1e-12)),
    )
    selected_ids = ranked_ids[:selected_count]
    selected_set = set(selected_ids)
    selected_edge_vehicle_counts = {edge_id: 0 for edge_id in targets}
    for record_id, record in records.items():
        if record_id not in selected_set:
            continue
        used = set(record.get("edges") or [])
        for edge_id in targets:
            if edge_id in used:
                selected_edge_vehicle_counts[edge_id] += 1

    result = {
        "target_edges": targets,
        "requested_fraction": fraction,
        "eligible_vehicle_count": len(eligible_ids),
        "eligible_edge_vehicle_counts": eligible_edge_vehicle_counts,
        "vehicles_using_multiple_target_edges": vehicles_using_multiple_target_edges,
        "selected_vehicle_count": len(selected_ids),
        "selected_edge_vehicle_counts": selected_edge_vehicle_counts,
        "realized_fraction": (
            len(selected_ids) / len(eligible_ids) if eligible_ids else 0.0
        ),
        "selected_vehicle_ids": selected_ids,
    }
    if len(targets) == 1:
        result["target_edge"] = targets[0]
    return result


def write_selected_trip_demand(
    source_route_path: str,
    selected_ids: Sequence[str],
    output_path: str,
) -> str:
    """Write only selected exact demand records for a partial duarouter run."""
    selected = set(str(record_id) for record_id in selected_ids)
    source_root = ET.parse(source_route_path).getroot()
    source_records = read_route_records(source_route_path)["records"]
    output_root = ET.Element(source_root.tag, dict(source_root.attrib))

    for child in source_root:
        kind = _local_name(child.tag)
        if kind in {"vType", "vTypeDistribution", "routeDistribution"}:
            output_root.append(copy.deepcopy(child))

    written: List[str] = []
    for child in source_root:
        kind = _local_name(child.tag)
        record_id = child.get("id")
        if kind not in {"trip", "vehicle"} or record_id not in selected:
            continue
        if kind == "trip":
            output_root.append(copy.deepcopy(child))
        else:
            record = source_records[record_id]
            attrs = {
                key: value
                for key, value in child.attrib.items()
                if key not in {"route", "from", "to"}
            }
            attrs["from"] = str(record.get("from_edge") or "")
            attrs["to"] = str(record.get("to_edge") or "")
            if not attrs["from"] or not attrs["to"]:
                raise ValueError(f"Cannot resolve OD for selected vehicle '{record_id}'")
            output_root.append(ET.Element("trip", attrs))
        written.append(record_id)

    missing = sorted(selected - set(written))
    if missing:
        raise ValueError(f"Selected demand IDs were not found in source routes: {missing[:10]}")
    output = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    ET.ElementTree(output_root).write(output, encoding="utf-8", xml_declaration=True)
    return output


def merge_partial_routes(
    reference_route_path: str,
    selected_route_path: str,
    selected_ids: Sequence[str],
    output_path: str,
) -> Dict[str, Any]:
    """Replace selected reference vehicles with their newly routed counterparts."""
    selected = set(str(record_id) for record_id in selected_ids)
    reference_tree = ET.parse(reference_route_path)
    reference_root = reference_tree.getroot()
    selected_root = ET.parse(selected_route_path).getroot()
    replacements = {
        str(child.get("id")): child
        for child in selected_root
        if _local_name(child.tag) in {"vehicle", "trip"} and child.get("id") in selected
    }
    missing = sorted(selected - set(replacements))
    if missing:
        raise ValueError(f"duarouter omitted selected vehicle IDs: {missing[:10]}")

    changed_count = 0
    replaced_count = 0
    reference_records = read_route_records(reference_route_path)["records"]
    selected_records = read_route_records(selected_route_path)["records"]
    for index, child in enumerate(list(reference_root)):
        record_id = child.get("id")
        if _local_name(child.tag) not in {"vehicle", "trip"} or record_id not in replacements:
            continue
        reference_root.remove(child)
        reference_root.insert(index, copy.deepcopy(replacements[record_id]))
        replaced_count += 1
        if (reference_records.get(record_id) or {}).get("edges") != (
            selected_records.get(record_id) or {}
        ).get("edges"):
            changed_count += 1

    output = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    reference_tree.write(output, encoding="utf-8", xml_declaration=True)
    return {
        "path": output,
        "replaced_vehicle_count": replaced_count,
        "changed_route_count": changed_count,
    }
