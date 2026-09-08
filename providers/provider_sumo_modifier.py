# providers/provider_sumo_modifier.py
import sys
import os
import logging
import xml.etree.ElementTree as ET
import subprocess
from typing import Optional, List, Dict, Any
from difflib import get_close_matches

from fastmcp import FastMCP

from Modification.remove_edge import (
    EdgeRemove,
    EdgeTargetError,
    resolve_opposite_edge,
    route_demand_units,
    rewrite_sumocfg_inputs,
)
from Modification.edge_speed import write_corridor_speed_network, write_edge_speed_network
from Modification.reroute import (
    avoided_edge_usage,
    merge_partial_routes,
    read_route_records,
    select_eligible_vehicle_ids,
    validate_od_preservation,
    write_selected_trip_demand,
    write_soft_avoid_weights,
)
from Modification.tls_program import (
    attach_additional_file,
    build_optimized_tllogic,
    clone_cfg_for_run,
    estimate_phase_demands_from_routes,
    load_tls_inventory,
    read_sumocfg_paths,
    validate_tllogic_phase_durations,
    write_tls_additional,
)
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("sumo-modifier")

def _open_log(out_dir: str, name: str):
    os.makedirs(out_dir, exist_ok=True)
    return open(os.path.join(out_dir, name), "w", encoding="utf-8")


def _list_edge_ids(net_path: str, limit: int = 50000):
    """List non-internal edge ids from SUMO net.xml (best-effort)."""
    ids = []
    tree = ET.parse(net_path)
    root = tree.getroot()
    for e in root.findall("edge"):
        eid = e.get("id")
        if not eid:
            continue
        if eid.startswith(":"):   # internal edges
            continue
        ids.append(eid)
        if len(ids) >= limit:
            break
    return ids

def _write_sumocfg_like(
    source_cfg: str,
    out_dir: str,
    net_path: str,
    route_path: str,
) -> str:
    """Write a SUMO config in out_dir for a rerouted scenario."""
    os.makedirs(out_dir, exist_ok=True)
    net_abs = os.path.abspath(net_path)
    route_abs = os.path.abspath(route_path)
    edge_data_abs = os.path.abspath(os.path.join(out_dir, "edgedata.xml"))
    tripinfo_abs = os.path.abspath(os.path.join(out_dir, "tripinfo.xml"))

    additional_abs = os.path.abspath(os.path.join(out_dir, "edge_data.add.xml"))
    with open(additional_abs, "w", encoding="utf-8") as f:
        f.write(
            f"""<additional>
    <edgeData id="edge_performance" period="60" file="{edge_data_abs}" excludeEmpty="true"/>
</additional>"""
        )

    cfg_out = os.path.abspath(os.path.join(out_dir, "sim.sumocfg"))
    with open(cfg_out, "w", encoding="utf-8") as f:
        f.write(f"""<configuration>
    <input>
        <net-file value="{net_abs}"/>
        <route-files value="{route_abs}"/>
        <additional-files value="{additional_abs}"/>
    </input>
    <report>
        <tripinfo-output value="{tripinfo_abs}"/>
        <device.emissions.probability value="1.0"/>
    </report>
</configuration>""")
    return cfg_out


def _sumocfg_add_additional(cfg_path: str, add_path: str) -> str:
    return attach_additional_file(cfg_path, add_path)


def _write_program_xml_as_additional(
    add_path: str,
    program_xml: str,
    expected_tls_id: Optional[str] = None,
) -> str:
    """Wrap a raw <tlLogic ...>...</tlLogic> program into an <additional> file."""
    os.makedirs(os.path.dirname(add_path), exist_ok=True)

    program_xml = (program_xml or "").strip()
    if not program_xml:
        raise ValueError("program_xml is empty")

    try:
        parsed = ET.fromstring(program_xml)
    except ET.ParseError as exc:
        raise ValueError(f"program_xml is not valid XML: {exc}") from exc
    if parsed.tag == "tlLogic":
        root = ET.Element("additional")
        root.append(parsed)
    elif parsed.tag == "additional":
        root = parsed
    else:
        raise ValueError("program_xml root must be <tlLogic> or <additional>")
    programs = list(root.findall("tlLogic"))
    if not programs:
        raise ValueError("program_xml must contain a <tlLogic ...> element")
    if expected_tls_id and any(node.get("id") != expected_tls_id for node in programs):
        raise ValueError(f"program_xml must target TLS id '{expected_tls_id}'")
    for program in programs:
        validate_tllogic_phase_durations(program)

    ET.ElementTree(root).write(add_path, encoding="utf-8", xml_declaration=True)

    return add_path


def _estimate_phase_demands_from_routes(
    route_path: str,
    phase_edge_groups: List[List[str]],
) -> List[float]:
    return estimate_phase_demands_from_routes(route_path, phase_edge_groups)


# -------------------------
# MCP tools
# -------------------------

def _tls_apply_program_impl(
    cfg_path: str,
    tl_id: str,
    cycle: float,
    splits: list[float],
    offset: float = 0.0,
    out_dir: Optional[str] = None,
) -> dict:
    """Apply explicit green shares while preserving the existing phase states."""
    cfg_abs = clone_cfg_for_run(cfg_path, out_dir) if out_dir else os.path.abspath(cfg_path)
    cfg_info = read_sumocfg_paths(cfg_abs)
    net_path = cfg_info.get("net")
    if not net_path:
        raise ValueError(f"SUMO cfg does not reference a net-file: {cfg_abs}")
    inventory = load_tls_inventory(str(net_path), tl_id)
    tl_logic, optimization = build_optimized_tllogic(
        inventory,
        demands=splits,
        cycle=float(cycle),
        min_split=0.0,
        offset=float(offset),
    )
    add_path = os.path.join(os.path.dirname(cfg_abs), f"tls_{tl_id}_opt.add.xml")
    tlf = write_tls_additional(add_path, tl_logic)
    _sumocfg_add_additional(cfg_abs, tlf)
    return {
        "status": "ok",
        "success": True,
        "artifacts": {
            "net": str(net_path),
            "routes": cfg_info.get("routes"),
            "cfg": cfg_abs,
            "tripinfo": cfg_info.get("tripinfo"),
            "additional": tlf,
            "tl_id": tl_id,
        },
        "optimization": optimization,
    }


@mcp.tool()
def tls_apply_program(
    cfg_path: str,
    tl_id: str,
    cycle: float,
    splits: list[float],
    offset: float = 0.0,
    out_dir: Optional[str] = None,
) -> dict:
    return _tls_apply_program_impl(
        cfg_path,
        tl_id,
        cycle,
        splits,
        offset=offset,
        out_dir=out_dir,
    )


@mcp.tool()
def tls_set_program(
    cfg_path: str,
    tls_id: str,
    program_xml: str,
    out_dir: Optional[str] = None,
) -> dict:
    """Attach a custom SUMO <tlLogic> program to a .sumocfg.

    This tool matches the agent-side MODIFY op `tls_set_program` expectation.
    """
    cfg_abs = clone_cfg_for_run(cfg_path, out_dir) if out_dir else os.path.abspath(cfg_path)
    cfg_info = read_sumocfg_paths(cfg_abs)
    net_path = cfg_info.get("net")
    if not net_path:
        raise ValueError(f"SUMO cfg does not reference a net-file: {cfg_abs}")
    load_tls_inventory(str(net_path), tls_id)
    add_path = os.path.join(os.path.dirname(cfg_abs), f"tls_{tls_id}_custom.add.xml")
    addf = _write_program_xml_as_additional(
        add_path,
        program_xml,
        expected_tls_id=tls_id,
    )
    _sumocfg_add_additional(cfg_abs, addf)
    return {
        "status": "ok",
        "success": True,
        "artifacts": {
            "net": str(net_path),
            "routes": cfg_info.get("routes"),
            "cfg": cfg_abs,
            "tripinfo": cfg_info.get("tripinfo"),
            "additional": addf,
            "tl_id": tls_id,
        },
    }


@mcp.tool()
def tls_optimize_and_apply(
    cfg_path: str,
    tl_id: str,
    cycle: float = 90.0,
    min_split: float = 0.08,
    offset: float = 0.0,
    route_path: Optional[str] = None,
    phase_edge_groups: Optional[List[List[str]]] = None,
    demands: Optional[List[float]] = None,
    out_dir: Optional[str] = None,
) -> dict:
    """Optimize green durations while preserving the network's real phase states."""
    cfg_abs = clone_cfg_for_run(cfg_path, out_dir) if out_dir else os.path.abspath(cfg_path)
    cfg_info = read_sumocfg_paths(cfg_abs)
    net_path = cfg_info.get("net")
    if not net_path:
        raise ValueError(f"SUMO cfg does not reference a net-file: {cfg_abs}")
    inventory = load_tls_inventory(str(net_path), tl_id)
    if not inventory.get("optimizable"):
        raise ValueError(
            f"TLS '{tl_id}' has fewer than two adjustable green phases; split optimization is not meaningful"
        )

    route_path = route_path or cfg_info.get("routes")
    demand_source = "explicit"
    if demands is not None:
        phase_demands = [float(x) for x in demands]
    else:
        groups = phase_edge_groups or inventory.get("phase_edge_groups") or []
        phase_demands = (
            _estimate_phase_demands_from_routes(str(route_path), groups)
            if route_path and groups
            else []
        )
        demand_source = "routes"
        if not phase_demands or sum(phase_demands) <= 0:
            phase_demands = [
                float(inventory["phases"][index]["duration"])
                for index in inventory["green_phase_indices"]
            ]
            demand_source = "existing_green_durations_fallback"

    tl_logic, optimization = build_optimized_tllogic(
        inventory,
        demands=phase_demands,
        cycle=float(cycle),
        min_split=float(min_split),
        offset=float(offset),
    )
    add_path = os.path.join(os.path.dirname(cfg_abs), f"tls_{tl_id}_opt.add.xml")
    addf = write_tls_additional(add_path, tl_logic)
    _sumocfg_add_additional(cfg_abs, addf)
    optimization.update({
        "demand_source": demand_source,
        "route_path": route_path,
        "net_path": net_path,
    })
    return {
        "status": "ok",
        "success": True,
        "message": f"Optimized TLS '{tl_id}' while preserving {len(inventory['phases'])} phase states.",
        "artifacts": {
            "net": str(net_path),
            "routes": cfg_info.get("routes"),
            "cfg": cfg_abs,
            "tripinfo": cfg_info.get("tripinfo"),
            "additional": addf,
            "tl_id": tl_id,
        },
        "optimization": optimization,
    }


def _reroute_flows_impl(
    net_path: str,
    route_path: str,
    avoid_edges: List[str],
    cfg_path: Optional[str] = None,
    out_dir: str = "./runs/tmp",
    routing_algorithm: str = "dijkstra",
    min_route_retention: float = 1.0,
    avoid_penalty_s: float = 3600.0,
    reroute_fraction: float = 1.0,
) -> dict:
    """Recompute routes with a high cost on bottleneck edges and preserve demand/OD."""
    try:
        if not avoid_edges:
            raise ValueError("avoid_edges must be a non-empty list")
        avoid_edges = list(dict.fromkeys(str(edge_id) for edge_id in avoid_edges if edge_id))
        if not 1 <= len(avoid_edges) <= 3:
            raise ValueError("reroute_flows requires between one and three edge IDs in avoid_edges")
        if float(min_route_retention) != 1.0:
            raise ValueError("reroute_flows requires min_route_retention=1.0")
        if float(avoid_penalty_s) <= 0:
            raise ValueError("avoid_penalty_s must be > 0")
        reroute_fraction = float(reroute_fraction)
        if reroute_fraction not in {0.25, 0.5, 0.75, 1.0}:
            raise ValueError("reroute_fraction must be one of 0.25, 0.5, 0.75, 1.0")

        os.makedirs(out_dir, exist_ok=True)
        net_abs = os.path.abspath(net_path)
        route_abs = os.path.abspath(route_path)
        cfg_abs = os.path.abspath(cfg_path) if cfg_path else None
        _list_edge_ids(net_abs, limit=1)
        if not os.path.exists(route_abs):
            raise FileNotFoundError(f"route_path not found: {route_abs}")
        before_demand = route_demand_units(route_abs)
        if before_demand <= 0:
            return {
                "status": "error",
                "success": False,
                "error_type": "SOURCE_DEMAND_EMPTY",
                "message": "The source route file contains no measurable vehicle demand.",
                "route_validation": {
                    "before_demand_units": before_demand,
                    "after_demand_units": 0.0,
                    "retention": 0.0,
                    "minimum_retention": float(min_route_retention),
                },
                "artifacts": {"net": net_abs, "routes": route_abs},
            }
        source_records = read_route_records(route_abs)
        if source_records.get("flow_count"):
            raise ValueError(
                "Fractional rerouting currently requires explicit <trip> or <vehicle> demand; "
                "source <flow> elements must be expanded first."
            )

        valid_edges = set(_list_edge_ids(net_abs))
        missing = [eid for eid in avoid_edges if eid not in valid_edges]
        if missing:
            return {
                "status": "error",
                "success": False,
                "error_type": "AVOID_EDGE_NOT_FOUND",
                "message": f"Cannot reroute around unknown edge ids: {missing}",
                "missing_edges": missing,
                "candidates": {
                    edge_id: get_close_matches(edge_id, sorted(valid_edges), n=5, cutoff=0.6)
                    for edge_id in missing
                },
                "artifacts": {"net": net_abs, "routes": route_abs},
            }

        reference_routes = os.path.abspath(os.path.join(out_dir, "routes.reference.rou.xml"))
        reference_cmd = [
            "duarouter",
            "-n", net_abs,
            "-r", route_abs,
            "-o", reference_routes,
            "--routing-algorithm", routing_algorithm,
        ]
        reference_log = os.path.abspath(os.path.join(out_dir, "reroute_reference_duarouter.log"))
        with open(reference_log, "w", encoding="utf-8") as logf:
            subprocess.run(reference_cmd, check=True, stdout=logf, stderr=logf)

        reference_demand = route_demand_units(reference_routes)
        reference_od_validation = validate_od_preservation(route_abs, reference_routes)
        if reference_demand != before_demand or not reference_od_validation["ok"]:
            raise ValueError(
                "Could not materialize a demand- and OD-preserving reference route set."
            )

        selection = select_eligible_vehicle_ids(
            reference_routes,
            avoid_edges,
            reroute_fraction,
        )
        selected_ids = selection["selected_vehicle_ids"]
        if not selected_ids:
            return {
                "status": "error",
                "success": False,
                "error_type": "NO_ELIGIBLE_VEHICLES",
                "message": f"No reference route uses any target edge: {avoid_edges}.",
                "reroute_selection": selection,
                "artifacts": {"net": net_abs, "routes": route_abs, "cfg": cfg_abs},
            }

        selected_demand = write_selected_trip_demand(
            route_abs,
            selected_ids,
            os.path.join(out_dir, "routes.selected.rou.xml"),
        )
        weight_file = write_soft_avoid_weights(
            os.path.join(out_dir, "soft_avoid_weights.xml"),
            avoid_edges,
            float(avoid_penalty_s),
        )

        selected_routes = os.path.abspath(os.path.join(out_dir, "routes.selected.rerouted.rou.xml"))
        duarouter_cmd = [
            "duarouter",
            "-n", net_abs,
            "-r", selected_demand,
            "-o", selected_routes,
            "--weight-files", weight_file,
            "--weight-attribute", "traveltime",
            "--routing-algorithm", routing_algorithm,
        ]
        duarouter_log = os.path.abspath(os.path.join(out_dir, "reroute_duarouter.log"))
        with open(duarouter_log, "w", encoding="utf-8") as logf:
            subprocess.run(duarouter_cmd, check=True, stdout=logf, stderr=logf)

        rerouted_routes = os.path.abspath(os.path.join(out_dir, "routes.rerouted.rou.xml"))
        merge_summary = merge_partial_routes(
            reference_routes,
            selected_routes,
            selected_ids,
            rerouted_routes,
        )
        selection.update(
            {
                "changed_route_count": merge_summary["changed_route_count"],
                "unchanged_selected_count": (
                    selection["selected_vehicle_count"] - merge_summary["changed_route_count"]
                ),
            }
        )

        after_demand = route_demand_units(rerouted_routes)
        retention = after_demand / before_demand
        od_validation = validate_od_preservation(route_abs, rerouted_routes)
        avoid_usage = avoided_edge_usage(rerouted_routes, avoid_edges)
        with open(duarouter_log, "r", encoding="utf-8", errors="replace") as logf:
            log_text = logf.read()
        warning_patterns = {
            "unknown_edge": " is not known",
            "no_valid_route": "has no valid route",
            "not_reachable": "not reachable",
            "no_connection": "No connection between edge",
        }
        warning_counts = {
            name: log_text.count(pattern) for name, pattern in warning_patterns.items()
        }
        route_validation = {
            "available": True,
            "before_demand_units": before_demand,
            "after_demand_units": after_demand,
            "dropped_demand_units": max(0.0, before_demand - after_demand),
            "retention": retention,
            "minimum_retention": float(min_route_retention),
            "demand_preserved": after_demand == before_demand,
            "od_preserved": bool(od_validation["ok"]),
            "od_validation": od_validation,
            "reference_od_validation": reference_od_validation,
            "reroute_selection": selection,
            "avoid_edge_usage": avoid_usage,
            "warning_counts": warning_counts,
            "log": duarouter_log,
        }
        if after_demand != before_demand or not od_validation["ok"]:
            error_type = "DEMAND_NOT_PRESERVED" if after_demand != before_demand else "OD_NOT_PRESERVED"
            return {
                "status": "error",
                "success": False,
                "error_type": error_type,
                "message": (
                    "Soft rerouting did not preserve every demand record and its original OD; "
                    "refusing to create an optimized scenario."
                ),
                "avoid_edges": avoid_edges,
                "route_validation": route_validation,
                "candidate_artifacts": {
                    "routes": rerouted_routes,
                    "reference_routes": reference_routes,
                    "selected_demand": selected_demand,
                    "selected_routes": selected_routes,
                    "weight_file": weight_file,
                },
                "artifacts": {"net": net_abs, "routes": route_abs, "cfg": cfg_abs},
            }

        if int(selection.get("changed_route_count") or 0) == 0:
            return {
                "status": "error",
                "success": False,
                "error_type": "NO_ROUTES_CHANGED",
                "message": (
                    "Soft rerouting found no alternative route for any selected vehicle; "
                    "refusing to evaluate a routing no-op."
                ),
                "avoid_edges": avoid_edges,
                "reroute_fraction": reroute_fraction,
                "reroute_selection": selection,
                "route_validation": route_validation,
                "candidate_artifacts": {
                    "routes": rerouted_routes,
                    "reference_routes": reference_routes,
                    "selected_demand": selected_demand,
                    "selected_routes": selected_routes,
                    "weight_file": weight_file,
                },
                "artifacts": {"net": net_abs, "routes": route_abs, "cfg": cfg_abs},
            }

        result = {
            "status": "ok",
            "success": True,
            "message": (
                f"Soft-rerouted {selection['selected_vehicle_count']}/"
                f"{selection['eligible_vehicle_count']} eligible vehicles "
                f"({reroute_fraction:.0%}) around {len(avoid_edges)} target edge(s) "
                f"{avoid_edges} while preserving all demand and OD."
            ),
            "artifacts": {
                "net": net_abs,
                "routes": rerouted_routes,
                "reference_routes": reference_routes,
                "selected_demand": selected_demand,
                "selected_routes": selected_routes,
                "weight_file": weight_file,
            },
            "net": net_abs,
            "routes": rerouted_routes,
            "route_path": rerouted_routes,
            "avoid_edges": avoid_edges,
            "avoid_penalty_s": float(avoid_penalty_s),
            "reroute_fraction": reroute_fraction,
            "reroute_selection": selection,
            "routing_algorithm": routing_algorithm,
            "route_validation": route_validation,
            "logs": {"reference_duarouter": reference_log, "duarouter": duarouter_log},
        }
        if cfg_abs:
            cfg_out = _write_sumocfg_like(cfg_abs, out_dir, net_abs, rerouted_routes)
            result["cfg"] = cfg_out
            result["artifacts"]["cfg"] = cfg_out
            result["artifacts"]["tripinfo"] = os.path.abspath(os.path.join(out_dir, "tripinfo.xml"))
        return result
    except Exception as e:
        logging.exception("reroute_flows failed")
        return {
            "status": "error",
            "success": False,
            "error_type": "REROUTE_FLOWS_FAILED",
            "message": str(e),
            "artifacts": {"net": net_path, "routes": route_path},
        }


@mcp.tool()
def reroute_flows(
    net_path: str,
    route_path: str,
    avoid_edges: List[str],
    cfg_path: Optional[str] = None,
    out_dir: str = "./runs/tmp",
    routing_algorithm: str = "dijkstra",
    min_route_retention: float = 1.0,
    avoid_penalty_s: float = 3600.0,
    reroute_fraction: float = 1.0,
) -> dict:
    return _reroute_flows_impl(
        net_path=net_path,
        route_path=route_path,
        avoid_edges=avoid_edges,
        cfg_path=cfg_path,
        out_dir=out_dir,
        routing_algorithm=routing_algorithm,
        min_route_retention=min_route_retention,
        avoid_penalty_s=avoid_penalty_s,
        reroute_fraction=reroute_fraction,
    )


@mcp.tool()
def set_edge_speed(
    net_path: str,
    edge_id: str,
    speed_mps: float,
    out_dir: str = "./runs/tmp",
    route_path: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> dict:
    """Set one edge's lane speeds and create an isolated runnable scenario."""
    net_abs = os.path.abspath(net_path)
    try:
        change = write_edge_speed_network(net_abs, edge_id, float(speed_mps), out_dir)
        modified_net = str(change["net"])
        route_abs = os.path.abspath(route_path) if route_path else None
        if route_abs and not os.path.isfile(route_abs):
            raise FileNotFoundError(f"route_path not found: {route_abs}")
        cfg_out = None
        tripinfo_out = None
        if cfg_path:
            if not route_abs:
                raise ValueError("cfg_path requires route_path to create a runnable scenario")
            cfg_out = clone_cfg_for_run(cfg_path, out_dir)
            rewrite_sumocfg_inputs(cfg_out, modified_net, route_abs)
            tripinfo_out = read_sumocfg_paths(cfg_out).get("tripinfo")
        artifacts = {
            "net": modified_net,
            "routes": route_abs,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
            "edge_speed_changed": str(change["target"]["edge_id"]),
        }
        return {
            "status": "ok",
            "success": True,
            "message": f"Set edge '{change['target']['edge_id']}' speed to {float(speed_mps):g} m/s.",
            "requested_target": edge_id,
            "target": change["target"],
            "old_speeds_mps": change["old_speeds_mps"],
            "new_speed_mps": change["new_speed_mps"],
            "artifacts": artifacts,
            "net": modified_net,
            "routes": route_abs,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
        }
    except Exception as exc:
        logging.exception("set_edge_speed failed")
        return {
            "status": "error",
            "success": False,
            "error_type": "SET_EDGE_SPEED_FAILED",
            "message": str(exc),
            "requested_target": edge_id,
            "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
        }


@mcp.tool()
def set_corridor_speed(
    net_path: str,
    edge_id: str,
    speed_factor: float,
    edge_count: int = 5,
    out_dir: str = "./runs/tmp",
    route_path: Optional[str] = None,
    cfg_path: Optional[str] = None,
) -> dict:
    """Apply proportional speed control to a contiguous same-road corridor."""
    net_abs = os.path.abspath(net_path)
    try:
        change = write_corridor_speed_network(
            net_abs,
            edge_id,
            float(speed_factor),
            out_dir,
            int(edge_count),
        )
        modified_net = str(change["net"])
        route_abs = os.path.abspath(route_path) if route_path else None
        if route_abs and not os.path.isfile(route_abs):
            raise FileNotFoundError(f"route_path not found: {route_abs}")
        cfg_out = None
        tripinfo_out = None
        if cfg_path:
            if not route_abs:
                raise ValueError("cfg_path requires route_path to create a runnable scenario")
            cfg_out = clone_cfg_for_run(cfg_path, out_dir)
            rewrite_sumocfg_inputs(cfg_out, modified_net, route_abs)
            tripinfo_out = read_sumocfg_paths(cfg_out).get("tripinfo")
        edge_ids = list(change["corridor"]["edge_ids"])
        artifacts = {
            "net": modified_net,
            "routes": route_abs,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
            "corridor_edges_speed_changed": edge_ids,
        }
        return {
            "status": "ok",
            "success": True,
            "message": (
                f"Scaled speeds on {len(edge_ids)} corridor edges by "
                f"{float(speed_factor):g}."
            ),
            "requested_anchor": edge_id,
            "anchor": change["anchor"],
            "corridor": change["corridor"],
            "speed_factor": change["speed_factor"],
            "changes": change["changes"],
            "artifacts": artifacts,
            "net": modified_net,
            "routes": route_abs,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
        }
    except Exception as exc:
        logging.exception("set_corridor_speed failed")
        return {
            "status": "error",
            "success": False,
            "error_type": "SET_CORRIDOR_SPEED_FAILED",
            "message": str(exc),
            "requested_anchor": edge_id,
            "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
        }


def _remove_edge_impl(
    net_path: str,
    edge_id: str,
    out_dir: str = "./runs/tmp",
    route_path: Optional[str] = None,
    cfg_path: Optional[str] = None,
    volume: Optional[int] = None,
    min_route_retention: float = 0.98,
) -> dict:
    """Safely remove one edge, repair routes, and create an isolated scenario config."""
    net_abs = os.path.abspath(net_path)
    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    try:
        if not 0.90 <= float(min_route_retention) <= 1.0:
            raise ValueError("min_route_retention must be in [0.90, 1.0]")
        edge = EdgeRemove()
        target = edge.resolve_target(net_abs, edge_id)
        feasibility = edge.check_feasibility(net_abs, str(target["edge_id"]))
        if not feasibility.get("ok"):
            return {
                "status": "error",
                "success": False,
                "error_type": "EDGE_REMOVAL_DISCONNECTS_NETWORK",
                "message": feasibility.get("message"),
                "requested_target": edge_id,
                "target": target,
                "feasibility": feasibility,
                "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
            }

        removal = edge.remove_edge_or_lane(
            net_abs,
            str(target["edge_id"]),
            remove_lane=False,
            lane_index=None,
            out_dir=out_abs,
        )
        modified_net = str(removal["net"])

        repaired_routes: Optional[str] = None
        route_validation: Dict[str, Any] = {"available": False}
        if route_path:
            route_abs = os.path.abspath(route_path)
            if not os.path.isfile(route_abs):
                raise FileNotFoundError(f"route_path not found: {route_abs}")
            repaired_routes = os.path.join(out_abs, "routes.edge_removed.rou.xml")
            route_log = os.path.join(out_abs, "remove_edge_duarouter.log")
            command = [
                "duarouter",
                "-n",
                modified_net,
                "-r",
                route_abs,
                "-o",
                repaired_routes,
                "--routing-algorithm",
                "dijkstra",
                "--repair",
                "--ignore-errors",
            ]
            with open(route_log, "w", encoding="utf-8") as log_file:
                subprocess.run(command, check=True, stdout=log_file, stderr=log_file)
            before = route_demand_units(route_abs)
            after = route_demand_units(repaired_routes)
            retention = (after / before) if before > 0 else 1.0
            route_validation = {
                "available": True,
                "before_demand_units": before,
                "after_demand_units": after,
                "retention": retention,
                "minimum_retention": float(min_route_retention),
                "log": os.path.abspath(route_log),
            }
            if retention < float(min_route_retention):
                return {
                    "status": "error",
                    "success": False,
                    "error_type": "ROUTE_RETENTION_BELOW_THRESHOLD",
                    "message": (
                        f"Route retention {retention:.3f} is below the required "
                        f"{float(min_route_retention):.3f}."
                    ),
                    "requested_target": edge_id,
                    "target": target,
                    "feasibility": feasibility,
                    "route_validation": route_validation,
                    "candidate_artifacts": {
                        "net": modified_net,
                        "routes": repaired_routes,
                    },
                    "artifacts": {"net": net_abs, "routes": route_abs, "cfg": cfg_path},
                }

        cfg_out: Optional[str] = None
        tripinfo_out: Optional[str] = None
        if cfg_path:
            if not repaired_routes:
                raise ValueError("cfg_path requires route_path so routes can be rebuilt safely")
            cfg_out = clone_cfg_for_run(cfg_path, out_abs)
            rewrite_sumocfg_inputs(cfg_out, modified_net, repaired_routes)
            tripinfo_out = read_sumocfg_paths(cfg_out).get("tripinfo")

        artifacts = {
            "net": modified_net,
            "routes": repaired_routes,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
            "edge_removed": str(target["edge_id"]),
        }
        return {
            "status": "ok",
            "success": True,
            "message": f"Removed edge '{target['edge_id']}' into an isolated scenario.",
            "requested_target": edge_id,
            "target": target,
            "feasibility": feasibility,
            "route_validation": route_validation,
            "artifacts": artifacts,
            "net": modified_net,
            "routes": repaired_routes,
            "route_path": repaired_routes,
            "cfg": cfg_out,
            "tripinfo": tripinfo_out,
            "logs": {"netconvert": removal.get("log")},
            "deprecated_volume": volume,
        }
    except EdgeTargetError as exc:
        return {
            "status": "error",
            "success": False,
            "error_type": exc.error_type,
            "message": str(exc),
            "requested_target": edge_id,
            "candidates": exc.candidates,
            "requires_user_input": exc.error_type == "AMBIGUOUS_EDGE_NAME",
            "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
        }
    except Exception as e:
        logging.exception("remove_edge failed")
        return {
            "status": "error",
            "success": False,
            "error_type": "REMOVE_EDGE_FAILED",
            "message": str(e),
            "requested_target": edge_id,
            "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
        }


@mcp.tool()
def remove_edge(
    net_path: str,
    edge_id: str,
    out_dir: str = "./runs/tmp",
    route_path: Optional[str] = None,
    cfg_path: Optional[str] = None,
    volume: Optional[int] = None,
    min_route_retention: float = 0.98,
) -> dict:
    """Safely remove one edge, repair routes, and create an isolated scenario config."""
    return _remove_edge_impl(
        net_path=net_path,
        edge_id=edge_id,
        out_dir=out_dir,
        route_path=route_path,
        cfg_path=cfg_path,
        volume=volume,
        min_route_retention=min_route_retention,
    )


@mcp.tool()
def make_road_one_way(
    net_path: str,
    keep_edge_id: str,
    out_dir: str = "./runs/tmp",
    opposite_edge_id: Optional[str] = None,
    route_path: Optional[str] = None,
    cfg_path: Optional[str] = None,
    min_route_retention: float = 0.98,
) -> dict:
    """Keep one directed edge and safely remove its reverse edge without adding lanes."""
    net_abs = os.path.abspath(net_path)
    try:
        direction = resolve_opposite_edge(net_abs, keep_edge_id, opposite_edge_id)
    except EdgeTargetError as exc:
        return {
            "status": "error",
            "success": False,
            "error_type": exc.error_type,
            "message": str(exc),
            "keep_edge_id": keep_edge_id,
            "opposite_edge_id": opposite_edge_id,
            "candidates": exc.candidates,
            "requires_user_input": exc.error_type == "AMBIGUOUS_OPPOSITE_EDGE",
            "artifacts": {"net": net_abs, "routes": route_path, "cfg": cfg_path},
        }

    kept = direction["keep_edge"]
    opposite = direction["opposite_edge"]
    result = _remove_edge_impl(
        net_path=net_abs,
        edge_id=str(opposite["edge_id"]),
        out_dir=out_dir,
        route_path=route_path,
        cfg_path=cfg_path,
        min_route_retention=min_route_retention,
    )
    result["keep_edge"] = kept
    result["opposite_edge"] = opposite
    result["direction_resolution"] = direction["matched_by"]
    if result.get("success"):
        result["message"] = (
            f"Kept edge '{kept['edge_id']}' and removed reverse edge "
            f"'{opposite['edge_id']}' without changing lane count."
        )
        result["artifacts"]["edge_kept"] = str(kept["edge_id"])
        result["artifacts"]["one_way_conversion"] = True
    return result


if __name__ == "__main__":
    # stdio 运行（本地开发最简单）；也可改 mcp.run_http(host, port)
    mcp.run()
