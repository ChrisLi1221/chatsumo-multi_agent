#!/usr/bin/env python3
"""Run only the closed-loop optimization stage on existing SUMO scenarios.

This intentionally skips scenario generation, the conversational planner, repair
loops, analyst narration, and SQLite bookkeeping. Each run is isolated and emits
machine-readable stage files suitable for later experiments.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROVIDERS_DIR = ROOT / "providers"
if str(PROVIDERS_DIR) not in sys.path:
    sys.path.insert(0, str(PROVIDERS_DIR))

from agents.bundle_optimizer import generate_pair_bundles
from agents.capabilities import normalize_capability_args, validate_modifier_capability
from agents.debate_optimizer import (
    OPTIMIZER_ALIASES,
    run_debate_optimizer,
    run_feedback_refinement,
)
from agents.grounded_baselines import generate_grounded_baseline
from agents.llm_agents import call_llm, resolved_llm_config
from agents.objectives import get_weight_profile, infer_weight_profile, objective_delta
from agents.state_refactored import AgentState


SELECTION_METHOD = "budgeted_pair_bundle_search_with_baseline_guard"
MIN_OBJECTIVE_IMPROVEMENT = 0.001
MIN_PRIMARY_RELATIVE_IMPROVEMENT = 0.001
MIN_TOP10_RELATIVE_IMPROVEMENT = 0.01
MAX_TOP10_GLOBAL_DELAY_INCREASE = 0.01
MAX_TOP10_NEIGHBOR_DELAY_INCREASE = 0.05
TOP10_EDGE_COUNT = 10
PRIMARY_METRIC_BY_PROFILE = {
    "delay": ("delay", "lower"),
    "throughput": ("throughput", "higher"),
    "emissions": ("co2", "lower"),
}
LLM_FEEDBACK_PROPOSAL_SOURCES = {"llm", "random_initial_llm"}


@dataclass(frozen=True)
class ExperimentCase:
    case_id: str
    cfg: str
    goal: str = "reduce traffic delay"
    profile: Optional[str] = None
    seed: Optional[int] = None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class LocalToolBridge:
    """Call provider functions directly, avoiding an unnecessary MCP client lifecycle."""

    def __init__(self) -> None:
        from providers import provider_metrics, provider_sumo_modifier, provider_sumo_runner

        self.modules = {
            "metrics": provider_metrics,
            "modifier": provider_sumo_modifier,
            "runner": provider_sumo_runner,
        }

    def call(self, server: str, action: str, arguments: Mapping[str, Any]) -> str:
        module = self.modules.get(server)
        tool = getattr(module, action, None) if module is not None else None
        function = tool if callable(tool) else getattr(tool, "fn", None)
        if not callable(function):
            raise RuntimeError(f"Unknown local provider tool: {server}.{action}")
        return json.dumps(function(**dict(arguments)), ensure_ascii=False, default=str)


def _tool(bridge: LocalToolBridge, server: str, action: str, args: Mapping[str, Any]) -> Dict[str, Any]:
    raw = bridge.call(server, action, dict(args))
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{server}.{action} returned a non-object result")
    if parsed.get("status") == "error" or parsed.get("success") is False:
        raise RuntimeError(f"{server}.{action} failed: {parsed.get('message') or parsed}")
    return parsed


def _resolve_list(cfg_dir: Path, value: str) -> List[Path]:
    paths = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        path = Path(item)
        paths.append((path if path.is_absolute() else cfg_dir / path).resolve())
    return paths


def _cfg_value(root: ET.Element, tag: str) -> str:
    node = root.find(f".//{tag}")
    return (node.get("value") or "").strip() if node is not None else ""


def _set_cfg_value(root: ET.Element, tag: str, value: str, section: str) -> None:
    node = root.find(f".//{tag}")
    if node is None:
        parent = root.find(section)
        if parent is None:
            parent = ET.SubElement(root, section)
        node = ET.SubElement(parent, tag)
    node.set("value", value)


def prepare_baseline_sandbox(source_cfg: str, baseline_dir: Path) -> Dict[str, str]:
    """Create an isolated cfg while keeping network and demand inputs read-only."""
    source_cfg_path = Path(source_cfg).expanduser().resolve()
    if not source_cfg_path.is_file():
        raise FileNotFoundError(f"SUMO cfg not found: {source_cfg_path}")

    source_root = ET.parse(source_cfg_path).getroot()
    source_dir = source_cfg_path.parent
    nets = _resolve_list(source_dir, _cfg_value(source_root, "net-file"))
    routes = _resolve_list(source_dir, _cfg_value(source_root, "route-files"))
    additionals = _resolve_list(source_dir, _cfg_value(source_root, "additional-files"))
    if not nets or not nets[0].is_file():
        raise FileNotFoundError("The cfg does not reference an existing net-file")
    if not routes or not all(path.is_file() for path in routes):
        raise FileNotFoundError("The cfg does not reference existing route-files")

    baseline_dir.mkdir(parents=True, exist_ok=True)
    root = ET.fromstring(ET.tostring(source_root, encoding="unicode"))
    _set_cfg_value(root, "net-file", str(nets[0]), "input")
    _set_cfg_value(root, "route-files", ",".join(map(str, routes)), "input")

    copied_additionals: List[str] = []
    edge_data_count = 0
    for index, add_source in enumerate(additionals):
        if not add_source.is_file():
            raise FileNotFoundError(f"additional-file not found: {add_source}")
        add_root = ET.parse(add_source).getroot()
        for edge_data in add_root.findall(".//edgeData"):
            suffix = "" if edge_data_count == 0 else f"_{edge_data_count}"
            edge_data.set("file", str((baseline_dir / f"edgedata{suffix}.xml").resolve()))
            edge_data_count += 1
        add_target = baseline_dir / f"additional_{index:02d}.add.xml"
        ET.ElementTree(add_root).write(add_target, encoding="utf-8", xml_declaration=True)
        copied_additionals.append(str(add_target.resolve()))

    # Guarantee edgeData for diagnosis even if the original scenario omitted it.
    if edge_data_count == 0:
        add_target = baseline_dir / "edge_data.add.xml"
        add_root = ET.Element("additional")
        ET.SubElement(
            add_root,
            "edgeData",
            id="optimization_edge_performance",
            period="60",
            file=str((baseline_dir / "edgedata.xml").resolve()),
            excludeEmpty="true",
        )
        ET.ElementTree(add_root).write(add_target, encoding="utf-8", xml_declaration=True)
        copied_additionals.append(str(add_target.resolve()))

    _set_cfg_value(root, "additional-files", ",".join(copied_additionals), "input")
    tripinfo = (baseline_dir / "tripinfo.xml").resolve()
    _set_cfg_value(root, "tripinfo-output", str(tripinfo), "report")
    _set_cfg_value(root, "device.emissions.probability", "1.0", "report")
    cfg_target = baseline_dir / "sim.sumocfg"
    ET.ElementTree(root).write(cfg_target, encoding="utf-8", xml_declaration=True)
    return {
        "cfg": str(cfg_target.resolve()),
        "net": str(nets[0]),
        "routes": str(routes[0]),
        "tripinfo": str(tripinfo),
        "edgedata": str((baseline_dir / "edgedata.xml").resolve()),
    }


def _teleport_count(artifacts: Mapping[str, str]) -> int:
    cfg = str(artifacts.get("cfg") or "")
    log_path = Path(cfg).resolve().parent / "sumo.run.log" if cfg else None
    if not log_path or not log_path.is_file():
        return 0
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return len(re.findall(r"Teleporting vehicle", text))


def _collect_scope_metrics(
    bridge: LocalToolBridge,
    artifacts: Mapping[str, str],
    focus_scope: Mapping[str, Any],
) -> Dict[str, Any]:
    target_ids = list(focus_scope.get("target_edge_ids") or [])
    neighbor_ids = list(focus_scope.get("neighbor_edge_ids") or [])
    target = _tool(
        bridge,
        "metrics",
        "edge_group_performance",
        {"edgedata_xml": artifacts["edgedata"], "edge_ids": target_ids},
    )
    neighbors = _tool(
        bridge,
        "metrics",
        "edge_group_performance",
        {"edgedata_xml": artifacts["edgedata"], "edge_ids": neighbor_ids},
    )
    return {
        "name": "top10",
        "frozen_from_baseline": True,
        "target": target,
        "neighbors": neighbors,
    }


def collect_kpi(
    bridge: LocalToolBridge,
    artifacts: Mapping[str, str],
    top_n: int,
    focus_scope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    speed = _tool(bridge, "metrics", "tripinfo_mean_speed", {"path": artifacts["tripinfo"], "unit": "km/h"})
    emissions = _tool(bridge, "metrics", "emissions_total", {"path": artifacts["tripinfo"], "unit": "kg"})
    diagnosis = _tool(
        bridge,
        "metrics",
        "network_diagnosis",
        {
            "edgedata_xml": artifacts["edgedata"],
            "net_file": artifacts["net"],
            "tripinfo_xml": artifacts["tripinfo"],
            "top_n": top_n,
        },
    )
    delay = diagnosis.get("delay") or {}
    completed = int(delay.get("completed_trips") or speed.get("count") or 0)
    departed = int(delay.get("departed_trips") or completed)
    result = {
        "mean_speed": float(speed.get("value") or 0.0),
        "mean_delay": float(delay.get("penalized_avg_time_loss_s") or delay.get("avg_time_loss_s") or 0.0),
        "throughput": (completed / departed) if departed > 0 else 0.0,
        "co2": float(((emissions.get("totals") or {}).get("CO2_abs")) or 0.0),
        "completed_trips": completed,
        "departed_trips": departed,
        "unfinished_trips": max(0, departed - completed),
        "teleport_count": _teleport_count(artifacts),
        "diagnosis": diagnosis,
    }
    if focus_scope:
        result["focus_scope"] = _collect_scope_metrics(bridge, artifacts, focus_scope)
    return result


def add_bottleneck_subgraph(
    bridge: LocalToolBridge,
    diagnosis: Dict[str, Any],
    net_file: str,
    seed_n: int,
    hops: int,
    max_edges: int,
) -> Dict[str, Any]:
    seeds: List[str] = []
    for section in ("bottlenecks", "top_congested_edges", "critical_roads"):
        for row in diagnosis.get(section) or []:
            edge_id = row.get("edge_id") if isinstance(row, dict) else None
            if edge_id and edge_id not in seeds:
                seeds.append(str(edge_id))
            if len(seeds) >= seed_n:
                break
        if len(seeds) >= seed_n:
            break
    if seeds:
        diagnosis = dict(diagnosis)
        diagnosis["bottleneck_subgraph"] = _tool(
            bridge,
            "metrics",
            "bottleneck_subgraph",
            {"net_file": net_file, "edge_ids": seeds, "hops": hops, "max_edges": max_edges},
        )
    return diagnosis


def freeze_top10_scope(
    bridge: LocalToolBridge,
    diagnosis: Mapping[str, Any],
    net_file: str,
) -> Dict[str, Any]:
    rows = [
        dict(row)
        for row in (diagnosis.get("top_congested_edges") or [])[:TOP10_EDGE_COUNT]
        if isinstance(row, Mapping) and row.get("edge_id")
    ]
    target_ids = [str(row["edge_id"]) for row in rows]
    if not target_ids:
        raise RuntimeError("top10 objective requires at least one diagnosed congested edge")
    neighborhood = _tool(
        bridge,
        "metrics",
        "bottleneck_subgraph",
        {
            "net_file": net_file,
            "edge_ids": target_ids,
            "hops": 1,
            "max_edges": max(200, len(target_ids)),
        },
    )
    target_set = set(target_ids)
    neighbor_ids = [
        str(row["edge_id"])
        for row in neighborhood.get("edges") or []
        if row.get("edge_id") and str(row["edge_id"]) not in target_set
    ]
    return {
        "name": "top10",
        "frozen_from": "baseline.top_congested_edges",
        "top_k": len(target_ids),
        "target_edge_ids": target_ids,
        "neighbor_edge_ids": neighbor_ids,
        "baseline_edge_ranking": rows,
        "neighborhood_hops": 1,
        "selection_constraints": {
            "minimum_top10_relative_improvement": MIN_TOP10_RELATIVE_IMPROVEMENT,
            "maximum_global_delay_relative_increase": MAX_TOP10_GLOBAL_DELAY_INCREASE,
            "maximum_neighbor_delay_relative_increase": MAX_TOP10_NEIGHBOR_DELAY_INCREASE,
            "throughput_must_not_decrease": True,
            "teleports_must_not_increase": True,
        },
    }


def _optimized_artifacts(modification: Mapping[str, Any], fallback: Mapping[str, str], out_dir: Path) -> Dict[str, str]:
    produced = dict(modification.get("artifacts") or {})
    cfg = str(produced.get("cfg") or fallback["cfg"])
    cfg_dir = Path(cfg).resolve().parent
    return {
        "cfg": cfg,
        "net": str(produced.get("net") or fallback["net"]),
        "routes": str(produced.get("routes") or fallback["routes"]),
        "tripinfo": str(produced.get("tripinfo") or (cfg_dir / "tripinfo.xml")),
        "edgedata": str(cfg_dir / "edgedata.xml"),
    }


def _adjust_kpi_for_source_demand(
    baseline_kpi: Mapping[str, Any],
    optimized_kpi: Mapping[str, Any],
    modification: Mapping[str, Any],
    *,
    missing_trip_penalty_s: float = 300.0,
) -> Dict[str, Any]:
    """Use source demand as the denominator and reject demand-dropping candidates."""
    # Kept only for API compatibility with older callers. Missing trips are now a
    # feasibility violation instead of being converted into an arbitrary delay.
    _ = missing_trip_penalty_s
    adjusted = dict(optimized_kpi)
    route_validation = dict(modification.get("route_validation") or {})
    baseline_demand = float(baseline_kpi.get("departed_trips") or 0.0)
    demand_before = float(route_validation.get("before_demand_units") or 0.0)
    demand_after = float(route_validation.get("after_demand_units") or demand_before)
    if route_validation and abs(demand_after - demand_before) > 1e-9:
        raise ValueError(
            "Candidate is infeasible because route demand was not preserved: "
            f"{demand_before:g} -> {demand_after:g}."
        )
    source_demand = max(baseline_demand, demand_before)
    completed = float(adjusted.get("completed_trips") or 0.0)
    raw_throughput = float(adjusted.get("throughput") or 0.0)

    adjusted["simulation_completion_rate"] = raw_throughput
    adjusted["source_demand_units"] = source_demand
    adjusted["dropped_before_departure"] = max(0.0, demand_before - demand_after)
    if source_demand > 0:
        adjusted["throughput"] = completed / source_demand
    return adjusted


def _evaluate_proposal(
    bridge: LocalToolBridge,
    proposal: Mapping[str, Any],
    candidate_dir: Path,
    baseline_artifacts: Mapping[str, str],
    baseline_kpi: Mapping[str, Any],
    weights: Mapping[str, float],
    top_n: int,
    focus_scope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    action = str(proposal.get("action") or "")
    selected_action = {
        "tool": proposal.get("tool") or "provider_sumo_modifier",
        "action": action,
        "args": dict(proposal.get("args") or {}),
    }
    sumo_evaluated = False
    try:
        modifier_args = _bind_bundle_action_args(
            action,
            selected_action["args"],
            baseline_artifacts,
            candidate_dir,
        )
        validation = validate_modifier_capability(action, modifier_args, baseline_artifacts)
        if not validation.get("ok"):
            raise RuntimeError(
                "single proposal failed canonical artifact validation: "
                + json.dumps(validation, ensure_ascii=False, default=str)
            )
        selected_action["args"] = modifier_args
        modification = _tool(bridge, "modifier", action, modifier_args)
        artifacts = _optimized_artifacts(modification, baseline_artifacts, candidate_dir)
        _write_json(candidate_dir / "04_modification.json", {"selected_action": selected_action, "result": modification})

        sumo_evaluated = True
        simulation = _tool(bridge, "runner", "run", {"cfg_path": artifacts["cfg"], "gui": False})
        measured_kpi = (
            collect_kpi(bridge, artifacts, top_n, focus_scope)
            if focus_scope
            else collect_kpi(bridge, artifacts, top_n)
        )
        diagnosis = measured_kpi.pop("diagnosis")
        kpi = _adjust_kpi_for_source_demand(baseline_kpi, measured_kpi, modification)
        optimized_record = {
            "artifacts": artifacts,
            "run": simulation,
            "kpi": kpi,
            "diagnosis": diagnosis,
        }
        _write_json(candidate_dir / "05_optimized.json", optimized_record)
        objective = objective_delta(baseline_kpi, kpi, baseline=baseline_kpi, weights=weights)
        scope_objective = _top10_scope_objective(baseline_kpi, kpi) if focus_scope else None
        comparison_record = {
            "proposal_id": proposal.get("proposal_id"),
            "agent": proposal.get("agent"),
            "selected_action": selected_action,
            "baseline_kpi": baseline_kpi,
            "optimized_kpi": kpi,
            "objective": objective,
            "scope_objective": scope_objective,
            "kpi_score": float(objective["current"]["value"]),
        }
        _write_json(candidate_dir / "06_comparison.json", comparison_record)
        result = {
            "status": "ok",
            "sumo_evaluated": sumo_evaluated,
            "proposal": dict(proposal),
            "selected_action": selected_action,
            "modification": modification,
            "optimized": optimized_record,
            "objective": objective,
            "kpi_score": float(objective["current"]["value"]),
        }
        if scope_objective is not None:
            result["scope_objective"] = scope_objective
        return result
    except Exception as exc:
        error = {"status": "error", "error": str(exc), "traceback": traceback.format_exc()}
        _write_json(candidate_dir / "error.json", error)
        return {
            "status": "error",
            "sumo_evaluated": sumo_evaluated,
            "proposal": dict(proposal),
            "selected_action": selected_action,
            **error,
        }


def _bind_bundle_action_args(
    action: str,
    raw_args: Mapping[str, Any],
    artifacts: Mapping[str, str],
    out_dir: Path,
) -> Dict[str, Any]:
    """Bind an action to artifacts produced by the preceding bundle step."""
    args = dict(raw_args or {})
    args["out_dir"] = str(out_dir.resolve())
    network_actions = {
        "remove_edge",
        "make_road_one_way",
        "set_edge_speed",
        "set_corridor_speed",
        "reroute_flows",
    }
    if action in network_actions:
        args["net_path"] = artifacts["net"]
        if artifacts.get("routes"):
            args["route_path"] = artifacts["routes"]
        if artifacts.get("cfg"):
            args["cfg_path"] = artifacts["cfg"]
    if action in {"tls_apply_program", "tls_set_program", "tls_optimize_and_apply"}:
        args["cfg_path"] = artifacts["cfg"]
        if action == "tls_optimize_and_apply" and artifacts.get("routes"):
            args["route_path"] = artifacts["routes"]
    return normalize_capability_args(action, args)


def _aggregate_route_validation(modifications: List[Mapping[str, Any]]) -> Dict[str, Any]:
    validations = [
        dict(modification.get("route_validation") or {})
        for modification in modifications
        if (modification.get("route_validation") or {}).get("available")
    ]
    if not validations:
        return {"available": False}
    return {
        "available": True,
        "before_demand_units": max(float(item.get("before_demand_units") or 0.0) for item in validations),
        "after_demand_units": float(validations[-1].get("after_demand_units") or 0.0),
        "minimum_retention": max(float(item.get("minimum_retention") or 0.0) for item in validations),
        "step_validations": validations,
    }


def _evaluate_bundle(
    bridge: LocalToolBridge,
    bundle: Mapping[str, Any],
    candidate_dir: Path,
    baseline_artifacts: Mapping[str, str],
    baseline_kpi: Mapping[str, Any],
    weights: Mapping[str, float],
    top_n: int,
    single_gain_by_id: Optional[Mapping[str, float]] = None,
    focus_scope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Incrementally validate and execute an ordered two-action bundle."""
    selected_action = {
        "tool": "bundle_optimizer",
        "action": "bundle",
        "actions": [dict(action) for action in bundle.get("actions") or []],
    }
    sumo_evaluated = False
    try:
        current_artifacts = dict(baseline_artifacts)
        modifications: List[Dict[str, Any]] = []
        executed_actions: List[Dict[str, Any]] = []
        for index, action_spec in enumerate(bundle.get("actions") or [], 1):
            action = str(action_spec.get("action") or "")
            step_dir = candidate_dir / f"step_{index:02d}_{action}"
            modifier_args = _bind_bundle_action_args(
                action,
                action_spec.get("args") or {},
                current_artifacts,
                step_dir,
            )
            validation = validate_modifier_capability(action, modifier_args, current_artifacts)
            if not validation.get("ok"):
                raise RuntimeError(
                    f"bundle step {index} ({action}) failed incremental validation: "
                    + json.dumps(validation, ensure_ascii=False, default=str)
                )
            modification = _tool(bridge, "modifier", action, modifier_args)
            current_artifacts = _optimized_artifacts(modification, current_artifacts, step_dir)
            modifications.append(modification)
            executed_actions.append(
                {
                    "index": index,
                    "action": action,
                    "args": modifier_args,
                    "validation": validation,
                    "result": modification,
                    "artifacts_after": current_artifacts,
                }
            )

        if not executed_actions:
            raise RuntimeError("bundle contains no executable actions")
        aggregate_modification = {
            "status": "ok",
            "success": True,
            "bundle_id": bundle.get("bundle_id"),
            "steps": executed_actions,
            "route_validation": _aggregate_route_validation(modifications),
            "artifacts": current_artifacts,
        }
        _write_json(
            candidate_dir / "04_modification.json",
            {"selected_action": selected_action, "result": aggregate_modification},
        )

        sumo_evaluated = True
        simulation = _tool(bridge, "runner", "run", {"cfg_path": current_artifacts["cfg"], "gui": False})
        measured_kpi = (
            collect_kpi(bridge, current_artifacts, top_n, focus_scope)
            if focus_scope
            else collect_kpi(bridge, current_artifacts, top_n)
        )
        diagnosis = measured_kpi.pop("diagnosis")
        kpi = _adjust_kpi_for_source_demand(
            baseline_kpi,
            measured_kpi,
            aggregate_modification,
        )
        optimized_record = {
            "artifacts": current_artifacts,
            "run": simulation,
            "kpi": kpi,
            "diagnosis": diagnosis,
        }
        _write_json(candidate_dir / "05_optimized.json", optimized_record)
        objective = objective_delta(baseline_kpi, kpi, baseline=baseline_kpi, weights=weights)
        scope_objective = _top10_scope_objective(baseline_kpi, kpi) if focus_scope else None
        source_ids = [
            str(action.get("source_proposal_id") or "")
            for action in bundle.get("actions") or []
        ]
        gains = dict(single_gain_by_id or {})
        bundle_gain = -float(objective["delta"])
        additive_single_gain = sum(float(gains.get(proposal_id) or 0.0) for proposal_id in source_ids)
        interaction = {
            "source_proposal_ids": source_ids,
            "bundle_gain": bundle_gain,
            "additive_single_gain": additive_single_gain,
            "interaction_gain": bundle_gain - additive_single_gain,
            "interpretation": (
                "synergy"
                if bundle_gain - additive_single_gain > 0
                else "interference"
                if bundle_gain - additive_single_gain < 0
                else "additive"
            ),
        }
        comparison_record = {
            "proposal_id": bundle.get("bundle_id"),
            "agent": "bundle_optimizer",
            "selected_action": selected_action,
            "baseline_kpi": baseline_kpi,
            "optimized_kpi": kpi,
            "objective": objective,
            "scope_objective": scope_objective,
            "interaction": interaction,
            "kpi_score": float(objective["current"]["value"]),
        }
        _write_json(candidate_dir / "06_comparison.json", comparison_record)
        result = {
            "status": "ok",
            "sumo_evaluated": sumo_evaluated,
            "proposal": dict(bundle),
            "selected_action": selected_action,
            "modification": aggregate_modification,
            "optimized": optimized_record,
            "objective": objective,
            "interaction": interaction,
            "kpi_score": float(objective["current"]["value"]),
        }
        if scope_objective is not None:
            result["scope_objective"] = scope_objective
        return result
    except Exception as exc:
        error = {"status": "error", "error": str(exc), "traceback": traceback.format_exc()}
        _write_json(candidate_dir / "error.json", error)
        return {
            "status": "error",
            "sumo_evaluated": sumo_evaluated,
            "proposal": dict(bundle),
            "selected_action": selected_action,
            **error,
        }


def _relative_change(current: float, baseline: float) -> Optional[float]:
    if baseline > 0:
        return current / baseline - 1.0
    if current <= 0:
        return 0.0
    return None


def _top10_scope_objective(
    baseline_kpi: Mapping[str, Any],
    current_kpi: Mapping[str, Any],
) -> Dict[str, Any]:
    baseline_scope = dict(baseline_kpi.get("focus_scope") or {})
    current_scope = dict(current_kpi.get("focus_scope") or {})
    baseline_target = dict(baseline_scope.get("target") or {})
    current_target = dict(current_scope.get("target") or {})
    baseline_neighbors = dict(baseline_scope.get("neighbors") or {})
    current_neighbors = dict(current_scope.get("neighbors") or {})

    baseline_target_loss = float(baseline_target.get("total_time_loss_s") or 0.0)
    current_target_loss = float(current_target.get("total_time_loss_s") or 0.0)
    target_change = _relative_change(current_target_loss, baseline_target_loss)
    target_improvement = -target_change if target_change is not None else None

    baseline_neighbor_loss = float(baseline_neighbors.get("total_time_loss_s") or 0.0)
    current_neighbor_loss = float(current_neighbors.get("total_time_loss_s") or 0.0)
    neighbor_change = _relative_change(current_neighbor_loss, baseline_neighbor_loss)

    baseline_global_delay = float(baseline_kpi.get("mean_delay") or 0.0)
    current_global_delay = float(current_kpi.get("mean_delay") or 0.0)
    global_delay_change = _relative_change(current_global_delay, baseline_global_delay)

    baseline_throughput = float(baseline_kpi.get("throughput") or 0.0)
    current_throughput = float(current_kpi.get("throughput") or 0.0)
    baseline_teleports = int(baseline_kpi.get("teleport_count") or 0)
    current_teleports = int(current_kpi.get("teleport_count") or 0)

    checks = {
        "top10_improvement": (
            target_improvement is not None
            and target_improvement >= MIN_TOP10_RELATIVE_IMPROVEMENT
        ),
        "global_delay": (
            global_delay_change is not None
            and global_delay_change <= MAX_TOP10_GLOBAL_DELAY_INCREASE + 1e-12
        ),
        "neighbor_spillover": (
            neighbor_change is not None
            and neighbor_change <= MAX_TOP10_NEIGHBOR_DELAY_INCREASE + 1e-12
        ),
        "throughput": current_throughput + 1e-12 >= baseline_throughput,
        "teleports": current_teleports <= baseline_teleports,
    }
    available = target_change is not None and global_delay_change is not None
    return {
        "scope": "top10",
        "available": available,
        "lower_is_better": True,
        "score": (current_target_loss / baseline_target_loss) if baseline_target_loss > 0 else None,
        "baseline": {
            "top10_total_time_loss_s": baseline_target_loss,
            "neighbor_total_time_loss_s": baseline_neighbor_loss,
            "global_mean_delay_s": baseline_global_delay,
            "throughput": baseline_throughput,
            "teleport_count": baseline_teleports,
        },
        "current": {
            "top10_total_time_loss_s": current_target_loss,
            "neighbor_total_time_loss_s": current_neighbor_loss,
            "global_mean_delay_s": current_global_delay,
            "throughput": current_throughput,
            "teleport_count": current_teleports,
        },
        "top10_relative_improvement": target_improvement,
        "global_delay_relative_change": global_delay_change,
        "neighbor_delay_relative_change": neighbor_change,
        "throughput_change": current_throughput - baseline_throughput,
        "teleport_change": current_teleports - baseline_teleports,
        "constraint_checks": checks,
        "constraints_passed": available and all(checks.values()),
    }


def _selection_policy(
    profile: Optional[str],
    objective_scope: str = "global",
) -> Dict[str, Any]:
    if objective_scope == "top10":
        return {
            "profile": profile,
            "objective_scope": "top10",
            "minimum_top10_relative_improvement": MIN_TOP10_RELATIVE_IMPROVEMENT,
            "maximum_global_delay_relative_increase": MAX_TOP10_GLOBAL_DELAY_INCREASE,
            "maximum_neighbor_delay_relative_increase": MAX_TOP10_NEIGHBOR_DELAY_INCREASE,
            "throughput_must_not_decrease": True,
            "teleports_must_not_increase": True,
            "rule": "top10_minimization_with_global_and_spillover_constraints",
        }
    primary = PRIMARY_METRIC_BY_PROFILE.get(str(profile or "").strip().lower())
    return {
        "profile": profile,
        "objective_scope": "global",
        "minimum_objective_improvement": MIN_OBJECTIVE_IMPROVEMENT,
        "primary_metric": primary[0] if primary else None,
        "primary_direction": primary[1] if primary else None,
        "minimum_primary_relative_improvement": (
            MIN_PRIMARY_RELATIVE_IMPROVEMENT if primary else None
        ),
        "rule": (
            "objective_and_primary_thresholds" if primary else "objective_threshold"
        ),
    }


def _candidate_selection_assessment(
    candidate: Mapping[str, Any],
    profile: Optional[str],
    objective_scope: str = "global",
) -> Dict[str, Any]:
    """Decide whether a measured candidate clears the practical-effect guard."""
    policy = _selection_policy(profile, objective_scope)
    if candidate.get("status") != "ok":
        return {
            **policy,
            "selection_eligible": False,
            "practical_improvement": False,
            "reason": "candidate_failed",
        }
    if candidate.get("is_baseline"):
        return {
            **policy,
            "selection_eligible": True,
            "practical_improvement": False,
            "objective_improvement": 0.0,
            "primary_relative_improvement": 0.0,
            "reason": "baseline_reference",
        }
    if objective_scope == "top10":
        scope_objective = dict(candidate.get("scope_objective") or {})
        if not scope_objective.get("available"):
            return {
                **policy,
                "selection_eligible": False,
                "practical_improvement": False,
                "reason": "missing_top10_scope_metrics",
                "scope_objective": scope_objective,
            }
        checks = dict(scope_objective.get("constraint_checks") or {})
        eligible = bool(scope_objective.get("constraints_passed"))
        failed = [name for name, passed in checks.items() if not passed]
        return {
            **policy,
            "selection_eligible": eligible,
            "practical_improvement": eligible,
            "top10_relative_improvement": scope_objective.get("top10_relative_improvement"),
            "global_delay_relative_change": scope_objective.get("global_delay_relative_change"),
            "neighbor_delay_relative_change": scope_objective.get("neighbor_delay_relative_change"),
            "throughput_change": scope_objective.get("throughput_change"),
            "teleport_change": scope_objective.get("teleport_change"),
            "constraint_checks": checks,
            "reason": "practical_improvement" if eligible else "constraint_failed:" + ",".join(failed),
        }
    if profile is None:
        objective = dict(candidate.get("objective") or {})
        return {
            **policy,
            "selection_eligible": True,
            "practical_improvement": bool(objective.get("improved")),
            "reason": "threshold_policy_not_requested",
        }

    objective = dict(candidate.get("objective") or {})
    delta = objective.get("delta")
    if delta is None:
        return {
            **policy,
            "selection_eligible": False,
            "practical_improvement": False,
            "reason": "missing_objective_delta",
        }

    objective_improvement = -float(delta)
    objective_pass = objective_improvement >= MIN_OBJECTIVE_IMPROVEMENT
    primary_metric = policy["primary_metric"]
    primary_improvement: Optional[float] = None
    primary_pass = True
    if primary_metric:
        term = ((objective.get("current") or {}).get("terms") or {}).get(primary_metric) or {}
        normalized = term.get("normalized")
        if normalized is None:
            primary_pass = False
        elif policy["primary_direction"] == "lower":
            primary_improvement = 1.0 - float(normalized)
        else:
            primary_improvement = float(normalized) - 1.0
        if primary_improvement is not None:
            primary_pass = primary_improvement >= MIN_PRIMARY_RELATIVE_IMPROVEMENT

    eligible = objective_pass and primary_pass
    if not objective_pass:
        reason = "objective_improvement_below_threshold"
    elif not primary_pass:
        reason = "primary_improvement_below_threshold"
    else:
        reason = "practical_improvement"
    return {
        **policy,
        "selection_eligible": eligible,
        "practical_improvement": eligible,
        "objective_improvement": objective_improvement,
        "objective_threshold_passed": objective_pass,
        "primary_relative_improvement": primary_improvement,
        "primary_threshold_passed": primary_pass,
        "reason": reason,
    }


def _candidate_summary(
    candidate: Mapping[str, Any],
    profile: Optional[str] = None,
    objective_scope: str = "global",
) -> Dict[str, Any]:
    proposal = dict(candidate.get("proposal") or {})
    objective = dict(candidate.get("objective") or {})
    assessment = _candidate_selection_assessment(candidate, profile, objective_scope)
    scope_objective = dict(candidate.get("scope_objective") or {})
    return {
        "status": candidate.get("status"),
        "sumo_evaluated": bool(candidate.get("sumo_evaluated")),
        "proposal_id": proposal.get("proposal_id"),
        "agent": proposal.get("agent"),
        "action": proposal.get("action"),
        "bundle_size": proposal.get("bundle_size", 1),
        "actions": [action.get("action") for action in proposal.get("actions") or []],
        "predicted_score": proposal.get("score"),
        "kpi_score": candidate.get("kpi_score"),
        "objective_delta": objective.get("delta"),
        "scope_score": scope_objective.get("score"),
        "top10_relative_improvement": scope_objective.get("top10_relative_improvement"),
        "global_delay_relative_change": scope_objective.get("global_delay_relative_change"),
        "neighbor_delay_relative_change": scope_objective.get("neighbor_delay_relative_change"),
        "interaction_gain": (candidate.get("interaction") or {}).get("interaction_gain"),
        "improved": assessment["practical_improvement"],
        "objective_improved": objective.get("improved"),
        "selection_eligible": assessment["selection_eligible"],
        "selection_assessment": assessment,
        "is_baseline": bool(candidate.get("is_baseline")),
        "error": candidate.get("error"),
    }


def _select_best_candidate(
    candidates: List[Mapping[str, Any]],
    profile: Optional[str] = None,
    objective_scope: str = "global",
) -> Optional[Mapping[str, Any]]:
    successful = [
        candidate
        for candidate in candidates
        if _candidate_selection_assessment(candidate, profile, objective_scope)["selection_eligible"]
    ]
    if objective_scope == "top10":
        def scope_score(candidate: Mapping[str, Any]) -> float:
            value = (candidate.get("scope_objective") or {}).get("score")
            return float(value) if value is not None else 1.0

        return min(
            successful,
            key=lambda candidate: (
                scope_score(candidate),
                float(candidate["kpi_score"]),
                0 if candidate.get("is_baseline") else 1,
            ),
            default=None,
        )
    return min(
        successful,
        key=lambda candidate: (
            float(candidate["kpi_score"]),
            0 if candidate.get("is_baseline") else 1,
        ),
        default=None,
    )


def _feedback_parent_candidates(
    candidates: List[Mapping[str, Any]],
    *,
    profile: Optional[str],
    objective_scope: str,
    beam_width: int,
) -> List[Mapping[str, Any]]:
    """Select a feasible-first, role-diverse beam of measured single actions."""
    if beam_width <= 0:
        return []
    successful = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "ok"
        and not candidate.get("is_baseline")
        and (candidate.get("proposal") or {}).get("action") != "bundle"
    ]

    def measured_score(candidate: Mapping[str, Any]) -> float:
        value = candidate.get("kpi_score")
        return float(value) if value is not None else float("inf")

    def key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
        assessment = _candidate_selection_assessment(candidate, profile, objective_scope)
        proposal_id = str((candidate.get("proposal") or {}).get("proposal_id") or "")
        if objective_scope == "top10":
            checks = dict(assessment.get("constraint_checks") or {})
            passed_count = sum(bool(value) for value in checks.values())
            scope_score = (candidate.get("scope_objective") or {}).get("score")
            return (
                not bool(assessment.get("selection_eligible")),
                -passed_count,
                float(scope_score) if scope_score is not None else float("inf"),
                measured_score(candidate),
                proposal_id,
            )
        return (
            not bool(assessment.get("selection_eligible")),
            measured_score(candidate),
            proposal_id,
        )

    ranked = sorted(successful, key=key)
    feasible = [
        candidate
        for candidate in ranked
        if _candidate_selection_assessment(candidate, profile, objective_scope).get(
            "selection_eligible"
        )
    ]
    infeasible = [candidate for candidate in ranked if candidate not in feasible]
    selected: List[Mapping[str, Any]] = []
    selected_ids = set()
    selected_agents = set()
    for group in (feasible, infeasible):
        for candidate in group:
            proposal = candidate.get("proposal") or {}
            agent = str(proposal.get("agent") or "")
            proposal_id = str(proposal.get("proposal_id") or "")
            if agent in selected_agents:
                continue
            selected.append(candidate)
            selected_ids.add(proposal_id)
            selected_agents.add(agent)
            if len(selected) >= beam_width:
                return selected
        for candidate in group:
            proposal_id = str((candidate.get("proposal") or {}).get("proposal_id") or "")
            if proposal_id in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(proposal_id)
            if len(selected) >= beam_width:
                return selected
    return selected


def _compact_proposal_args(proposal: Mapping[str, Any]) -> Dict[str, Any]:
    args = dict(proposal.get("args") or {})
    for key in ("net_path", "route_path", "cfg_path", "out_dir"):
        args.pop(key, None)
    return args


def _candidate_feedback(
    candidate: Mapping[str, Any],
    baseline_kpi: Mapping[str, Any],
    *,
    profile: Optional[str],
    objective_scope: str,
) -> Dict[str, Any]:
    assessment = _candidate_selection_assessment(candidate, profile, objective_scope)
    current_kpi = dict((candidate.get("optimized") or {}).get("kpi") or {})
    metric_keys = (
        "mean_delay",
        "mean_speed",
        "throughput",
        "co2",
        "completed_trips",
        "teleport_count",
    )
    feedback = {
        "status": candidate.get("status"),
        "baseline_kpi": {key: baseline_kpi.get(key) for key in metric_keys},
        "measured_kpi": {key: current_kpi.get(key) for key in metric_keys},
        "objective_delta": (candidate.get("objective") or {}).get("delta"),
        "selection_assessment": assessment,
    }
    if objective_scope == "top10":
        scope = dict(candidate.get("scope_objective") or {})
        feedback["scope_objective"] = scope
        failed = [
            name
            for name, passed in (scope.get("constraint_checks") or {}).items()
            if not passed
        ]
        feedback["failed_constraints"] = failed
    return feedback


def _feedback_history(
    candidates: List[Mapping[str, Any]],
    *,
    profile: Optional[str],
    objective_scope: str,
) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("is_baseline") or candidate.get("status") != "ok":
            continue
        proposal = dict(candidate.get("proposal") or {})
        assessment = _candidate_selection_assessment(candidate, profile, objective_scope)
        history.append(
            {
                "proposal_id": proposal.get("proposal_id"),
                "agent": proposal.get("agent"),
                "action": proposal.get("action"),
                "args": _compact_proposal_args(proposal),
                "objective_delta": (candidate.get("objective") or {}).get("delta"),
                "top10_relative_improvement": (
                    candidate.get("scope_objective") or {}
                ).get("top10_relative_improvement"),
                "selection_eligible": assessment.get("selection_eligible"),
                "reason": assessment.get("reason"),
            }
        )
    return history


def _best_so_far_trace(
    candidates: List[Mapping[str, Any]],
    *,
    profile: Optional[str],
    objective_scope: str,
) -> List[Dict[str, Any]]:
    """Return the incumbent after each candidate SUMO evaluation."""
    pool: List[Mapping[str, Any]] = [
        candidate for candidate in candidates if candidate.get("is_baseline")
    ]
    trace: List[Dict[str, Any]] = []
    evaluation_index = 0
    for candidate in candidates:
        if candidate.get("is_baseline"):
            continue
        pool.append(candidate)
        if not candidate.get("sumo_evaluated"):
            continue
        evaluation_index += 1
        best = _select_best_candidate(pool, profile, objective_scope)
        if best is None:
            continue
        proposal = dict(best.get("proposal") or {})
        scope = dict(best.get("scope_objective") or {})
        trace.append(
            {
                "sumo_evaluation": evaluation_index,
                "evaluated_proposal_id": (candidate.get("proposal") or {}).get(
                    "proposal_id"
                ),
                "incumbent_proposal_id": proposal.get("proposal_id"),
                "incumbent_action": proposal.get("action"),
                "incumbent_is_baseline": bool(best.get("is_baseline")),
                "incumbent_kpi_score": best.get("kpi_score"),
                "incumbent_scope_score": scope.get("score"),
                "incumbent_top10_relative_improvement": scope.get(
                    "top10_relative_improvement"
                ),
            }
        )
    return trace


def _baseline_candidate(
    artifacts: Mapping[str, str],
    baseline_run: Mapping[str, Any],
    baseline_kpi: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    weights: Mapping[str, float],
    objective_scope: str = "global",
) -> Dict[str, Any]:
    """Represent retaining the original scenario as a measured no-op candidate."""
    proposal = {
        "proposal_id": "baseline_noop",
        "agent": "baseline_guard",
        "action": "no_op",
        "capability": "retain_baseline",
        "tool": "baseline_guard",
        "args": {},
        "bundle_size": 0,
        "score": None,
        "risk": "No intervention is applied.",
        "validation": {"ok": True, "action": "no_op"},
    }
    selected_action = {"tool": "baseline_guard", "action": "no_op", "args": {}}
    objective = objective_delta(
        baseline_kpi,
        baseline_kpi,
        baseline=baseline_kpi,
        weights=weights,
    )
    modification = {
        "status": "ok",
        "success": True,
        "message": "Retained baseline because no measured candidate had a lower objective.",
        "artifacts": dict(artifacts),
    }
    optimized = {
        "artifacts": dict(artifacts),
        "run": dict(baseline_run),
        "kpi": dict(baseline_kpi),
        "diagnosis": dict(diagnosis),
        "reused_baseline_run": True,
    }
    candidate = {
        "status": "ok",
        "is_baseline": True,
        "proposal": proposal,
        "selected_action": selected_action,
        "modification": modification,
        "optimized": optimized,
        "objective": objective,
        "kpi_score": float(objective["current"]["value"]),
    }
    if objective_scope == "top10":
        candidate["scope_objective"] = _top10_scope_objective(baseline_kpi, baseline_kpi)
    return candidate


def _parse_routing_edge_counts(value: str) -> List[int]:
    counts: List[int] = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        count = int(item)
        if count not in {1, 2, 3}:
            raise argparse.ArgumentTypeError("routing edge counts must be selected from 1, 2, 3")
        if count not in counts:
            counts.append(count)
    if not counts:
        raise argparse.ArgumentTypeError("provide at least one routing edge count")
    return counts


def _expand_routing_variants(
    proposals: List[Mapping[str, Any]],
    edge_counts: Optional[List[int]],
    reroute_fraction: Optional[float],
    artifacts: Mapping[str, str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Create nested routing ablations from one model proposal.

    The ordered LLM edge list is generated once. Controlled variants use the
    first N targets and, optionally, one fixed fraction, so edge count is the
    only routing parameter that changes within the run.
    """
    expanded: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    for raw_proposal in proposals:
        proposal = dict(raw_proposal)
        if proposal.get("action") != "reroute_flows":
            expanded.append(proposal)
            continue

        args = dict(proposal.get("args") or {})
        raw_edges = args.get("avoid_edges") or []
        edges = [str(edge_id) for edge_id in raw_edges if edge_id]
        requested_counts = list(edge_counts or [len(edges)])
        base_id = str(proposal.get("proposal_id") or "demand_routing_optimizer_proposal")
        for count in requested_counts:
            if len(edges) < count:
                audit.append(
                    {
                        "source_proposal_id": base_id,
                        "requested_edge_count": count,
                        "available_edge_count": len(edges),
                        "status": "skipped",
                        "reason": "model proposal did not provide enough ordered edge targets",
                    }
                )
                continue
            variant = dict(proposal)
            variant_args = dict(args)
            variant_args["avoid_edges"] = edges[:count]
            if reroute_fraction is not None:
                variant_args["reroute_fraction"] = float(reroute_fraction)
            variant["args"] = normalize_capability_args("reroute_flows", variant_args)
            if edge_counts is not None:
                variant["proposal_id"] = f"{base_id}__edges_{count}"
            variant["experimental_control"] = {
                "source_proposal_id": base_id,
                "routing_edge_count": count,
                "edge_prefix_of_model_output": True,
                "fixed_reroute_fraction": reroute_fraction,
            }
            variant["validation"] = validate_modifier_capability(
                "reroute_flows",
                variant["args"],
                artifacts,
            )
            if not variant["validation"].get("ok"):
                audit.append(
                    {
                        "source_proposal_id": base_id,
                        "proposal_id": variant["proposal_id"],
                        "requested_edge_count": count,
                        "status": "skipped",
                        "reason": "controlled variant failed validation",
                        "validation": variant["validation"],
                    }
                )
                continue
            expanded.append(variant)
            audit.append(
                {
                    "source_proposal_id": base_id,
                    "proposal_id": variant["proposal_id"],
                    "requested_edge_count": count,
                    "avoid_edges": variant["args"]["avoid_edges"],
                    "reroute_fraction": variant["args"].get("reroute_fraction"),
                    "status": "ready",
                }
            )
    return expanded, audit


def _proposal_semantic_signature(proposal: Mapping[str, Any]) -> str:
    args = dict(proposal.get("args") or {})
    for key in ("net_path", "route_path", "cfg_path", "out_dir"):
        args.pop(key, None)
    return json.dumps(
        {"action": proposal.get("action"), "args": args},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _deduplicate_evaluation_proposals(
    proposals: List[Mapping[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    unique: List[Dict[str, Any]] = []
    audit: List[Dict[str, Any]] = []
    by_signature: Dict[str, str] = {}
    for raw in proposals:
        proposal = dict(raw)
        signature = _proposal_semantic_signature(proposal)
        first_id = by_signature.get(signature)
        if first_id is not None:
            audit.append(
                {
                    "proposal_id": proposal.get("proposal_id"),
                    "status": "duplicate_skipped",
                    "duplicate_of": first_id,
                }
            )
            continue
        proposal_id = str(proposal.get("proposal_id") or f"candidate_{len(unique) + 1:03d}")
        by_signature[signature] = proposal_id
        unique.append(proposal)
        audit.append({"proposal_id": proposal_id, "status": "unique"})
    return unique, audit


def _resolved_proposal_attempt_budget(
    args: argparse.Namespace,
    source: Optional[str] = None,
    role_count: Optional[int] = None,
) -> int:
    """Resolve a fair method-specific initial proposal budget.

    Feedback methods reserve candidate simulations for measured refinement;
    one-shot baselines may generate up to the full SUMO candidate budget.
    An explicit --proposal-attempt-budget still overrides this policy.
    """
    proposal_source = str(source or getattr(args, "proposal_source", "llm"))
    if role_count is None:
        role_count = 3 if getattr(args, "optimizer", "all") == "all" else 1
    requested_attempts = getattr(args, "proposal_attempt_budget", None)
    if requested_attempts is None:
        if (
            int(getattr(args, "search_rounds", 1)) > 1
            and proposal_source in LLM_FEEDBACK_PROPOSAL_SOURCES
        ):
            requested_attempts = role_count
        else:
            requested_attempts = (
                int(args.sumo_budget)
                if getattr(args, "sumo_budget", None) is not None
                else role_count
            )
    return max(1, int(requested_attempts))


def _generate_proposal_source(
    state: AgentState,
    diagnosis: Mapping[str, Any],
    baseline_artifacts: Mapping[str, str],
    case: ExperimentCase,
    profile: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    source = str(getattr(args, "proposal_source", "llm"))
    active_agents = None if args.optimizer == "all" else [OPTIMIZER_ALIASES[args.optimizer]]
    role_count = 3 if active_agents is None else len(active_agents)
    attempt_budget = _resolved_proposal_attempt_budget(args, source, role_count)

    proposal_seed = getattr(args, "proposal_seed", None)
    if proposal_seed is None:
        proposal_seed = case.seed if case.seed is not None else 42
    proposal_seed = int(proposal_seed) + int(getattr(args, "_repetition", 1)) - 1

    if source != "llm":
        generated = generate_grounded_baseline(
            source,
            diagnosis,
            baseline_artifacts,
            attempt_budget=attempt_budget,
            seed=proposal_seed,
            optimizer=args.optimizer,
            grid_edge_pool_size=int(getattr(args, "grid_edge_pool_size", 3)),
        )
        generated["generation"] = {
            "proposal_source": source,
            "attempt_budget": attempt_budget,
            "proposal_seed": proposal_seed,
            "llm_calls_used": False,
            "feedback_llm_calls_enabled": (
                source == "random_initial_llm"
                and int(getattr(args, "search_rounds", 1)) > 1
            ),
        }
        return generated

    rounds = max(1, (attempt_budget + role_count - 1) // role_count)
    proposals: List[Dict[str, Any]] = []
    round_records: List[Dict[str, Any]] = []
    for round_index in range(1, rounds + 1):
        debate = run_debate_optimizer(
            state,
            diagnosis=dict(diagnosis),
            user_request=case.goal,
            objective_profile=profile,
            optimizer_agents=active_agents,
            call_llm_fn=call_llm,
        )
        round_proposals: List[Dict[str, Any]] = []
        id_map: Dict[str, str] = {}
        for raw in debate.get("proposals") or []:
            proposal = dict(raw)
            original_id = str(proposal.get("proposal_id") or "proposal")
            proposal_id = (
                original_id
                if rounds == 1
                else f"{original_id}__round_{round_index:02d}"
            )
            id_map[original_id] = proposal_id
            proposal["proposal_id"] = proposal_id
            proposal["proposal_source"] = "llm"
            proposal["generation_round"] = round_index
            proposal["search_round"] = 1
            proposals.append(proposal)
            round_proposals.append(proposal)
            if len(proposals) >= attempt_budget:
                break
        judge = dict(debate.get("judge") or {})
        judge_id = str(judge.get("selected_proposal_id") or "")
        if judge_id in id_map:
            judge["selected_proposal_id"] = id_map[judge_id]
        round_records.append(
            {
                "round": round_index,
                "proposal_ids": [proposal.get("proposal_id") for proposal in round_proposals],
                "judge": judge,
            }
        )
        if len(proposals) >= attempt_budget:
            break

    valid = [proposal for proposal in proposals if (proposal.get("validation") or {}).get("ok")]
    return {
        "status": "ok" if valid else "no_executable_proposal",
        "proposal_source": "llm",
        "active_optimizers": active_agents or list(OPTIMIZER_ALIASES.values()),
        "objective_profile": profile,
        "objective_weights": get_weight_profile(profile),
        "proposals": proposals,
        "selected": valid[0] if valid else None,
        "selected_action": (
            {
                "tool": valid[0].get("tool") or "provider_sumo_modifier",
                "action": valid[0].get("action"),
                "args": valid[0].get("args") or {},
            }
            if valid
            else None
        ),
        "judge": (
            round_records[0]["judge"]
            if len(round_records) == 1
            else {"used": True, "rounds": round_records}
        ),
        "generation": {
            "proposal_source": "llm",
            "attempt_budget": attempt_budget,
            "round_count": len(round_records),
            "llm_calls_used": True,
            "rounds": round_records,
        },
    }


def _relative_improvement(lower_current: float, lower_baseline: float) -> Optional[float]:
    change = _relative_change(lower_current, lower_baseline)
    return -change if change is not None else None


def _proposal_source_metrics(
    raw_proposals: List[Mapping[str, Any]],
    evaluated_candidates: List[Mapping[str, Any]],
    baseline_kpi: Mapping[str, Any],
    best: Mapping[str, Any],
    *,
    proposal_source: str,
    sumo_budget: Optional[int],
    objective_scope: str,
) -> Dict[str, Any]:
    raw_count = len(raw_proposals)
    valid_count = sum(
        1
        for proposal in raw_proposals
        if proposal.get("action") and (proposal.get("validation") or {}).get("ok")
    )
    noop_count = sum(1 for proposal in raw_proposals if not proposal.get("action"))
    rows: List[Dict[str, Any]] = []
    for candidate in evaluated_candidates:
        proposal = dict(candidate.get("proposal") or {})
        sumo_evaluated = bool(candidate.get("sumo_evaluated"))
        status_ok = candidate.get("status") == "ok"
        optimized_kpi = dict((candidate.get("optimized") or {}).get("kpi") or {})
        global_improvement = None
        if status_ok:
            global_improvement = _relative_improvement(
                float(optimized_kpi.get("mean_delay") or 0.0),
                float(baseline_kpi.get("mean_delay") or 0.0),
            )
        scope = dict(candidate.get("scope_objective") or {})
        top10_improvement = scope.get("top10_relative_improvement")

        route_validation = dict((candidate.get("modification") or {}).get("route_validation") or {})
        route_preserved = True
        if route_validation.get("available"):
            before = float(route_validation.get("before_demand_units") or 0.0)
            after = float(route_validation.get("after_demand_units") or 0.0)
            route_preserved = bool(route_validation.get("ok", True)) and abs(before - after) <= 1e-9

        throughput_ok = (
            status_ok
            and float(optimized_kpi.get("throughput") or 0.0) + 1e-12
            >= float(baseline_kpi.get("throughput") or 0.0)
        )
        teleports_ok = (
            status_ok
            and int(optimized_kpi.get("teleport_count") or 0)
            <= int(baseline_kpi.get("teleport_count") or 0)
        )
        global_delay_ok = (
            status_ok
            and global_improvement is not None
            and global_improvement
            >= -MAX_TOP10_GLOBAL_DELAY_INCREASE - 1e-12
        )
        neighbor_ok = True
        if scope.get("available"):
            neighbor_change = scope.get("neighbor_delay_relative_change")
            neighbor_ok = (
                neighbor_change is not None
                and float(neighbor_change) <= MAX_TOP10_NEIGHBOR_DELAY_INCREASE + 1e-12
            )
        feasible = status_ok and route_preserved
        safety_constraints_passed = bool(
            feasible and throughput_ok and teleports_ok and global_delay_ok and neighbor_ok
        )
        primary_improvement = (
            float(top10_improvement)
            if objective_scope == "top10" and top10_improvement is not None
            else global_improvement
        )
        improving_1pct = bool(
            primary_improvement is not None and float(primary_improvement) >= 0.01
        )
        all_constraints_passed = (
            bool(scope.get("constraints_passed"))
            if objective_scope == "top10" and scope.get("available")
            else bool(safety_constraints_passed and improving_1pct)
        )
        feasible_improving = bool(safety_constraints_passed and improving_1pct)
        rows.append(
            {
                "proposal_id": proposal.get("proposal_id"),
                "agent": proposal.get("agent"),
                "action": proposal.get("action"),
                "sumo_evaluated": sumo_evaluated,
                "simulation_success": status_ok,
                "route_preserved": route_preserved,
                "safety_constraints_passed": safety_constraints_passed,
                "improving_1pct": improving_1pct,
                "all_constraints_passed": all_constraints_passed,
                "feasible_improving": feasible_improving,
                "top10_relative_improvement": top10_improvement,
                "global_delay_relative_improvement": global_improvement,
                "error": candidate.get("error"),
            }
        )

    evaluated_rows = [row for row in rows if row["sumo_evaluated"]]
    evaluation_count = len(evaluated_rows)
    successful_count = sum(1 for row in evaluated_rows if row["simulation_success"])
    safety_count = sum(1 for row in evaluated_rows if row["safety_constraints_passed"])
    improving_count = sum(1 for row in evaluated_rows if row["improving_1pct"])
    all_constraints_count = sum(1 for row in evaluated_rows if row["all_constraints_passed"])
    feasible_improving_count = sum(1 for row in evaluated_rows if row["feasible_improving"])

    selected_kpi = dict((best.get("optimized") or {}).get("kpi") or {})
    selected_scope = dict(best.get("scope_objective") or {})
    final_global_improvement = _relative_improvement(
        float(selected_kpi.get("mean_delay") or 0.0),
        float(baseline_kpi.get("mean_delay") or 0.0),
    )

    def rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    return {
        "definition": {
            "simulation_efficiency": "feasible_improving_candidates / SUMO_evaluations",
            "improving_candidate": (
                "at least 1% Top-10 time-loss reduction"
                if objective_scope == "top10"
                else "at least 1% global mean-delay reduction"
            ),
            "safety_constraints": [
                "route demand and OD preserved",
                "throughput not reduced",
                "teleports not increased",
                "global mean delay increase <= 1%",
                "one-hop time loss increase <= 5% when Top-10 scope is available",
            ],
        },
        "proposal_source": proposal_source,
        "objective_scope": objective_scope,
        "sumo_budget": sumo_budget,
        "counts": {
            "raw_proposals": raw_count,
            "validated_proposals": valid_count,
            "proposal_noops": noop_count,
            "candidate_attempts_after_validation": len(rows),
            "sumo_evaluations": evaluation_count,
            "successful_simulations": successful_count,
            "safety_feasible_candidates": safety_count,
            "improving_1pct_candidates": improving_count,
            "all_constraints_passed_candidates": all_constraints_count,
            "feasible_improving_candidates": feasible_improving_count,
        },
        "rates": {
            "proposal_validation_rate": rate(valid_count, raw_count),
            "proposal_noop_rate": rate(noop_count, raw_count),
            "simulation_success_rate": rate(successful_count, evaluation_count),
            "feasible_plan_probability_per_sumo_evaluation": rate(safety_count, evaluation_count),
            "improving_1pct_rate_per_sumo_evaluation": rate(improving_count, evaluation_count),
            "all_constraints_pass_rate": rate(all_constraints_count, evaluation_count),
            "simulation_efficiency": rate(feasible_improving_count, evaluation_count),
            "sumo_budget_utilization": (
                rate(evaluation_count, int(sumo_budget)) if sumo_budget else 1.0
            ),
        },
        "final_selection": {
            "proposal_id": (best.get("proposal") or {}).get("proposal_id"),
            "action": (best.get("proposal") or {}).get("action"),
            "no_op": bool(best.get("is_baseline")),
            "top10_relative_improvement": selected_scope.get("top10_relative_improvement"),
            "global_delay_relative_improvement": final_global_improvement,
        },
        "candidate_records": rows,
    }


def run_case(
    bridge: LocalToolBridge,
    case: ExperimentCase,
    run_dir: Path,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    started = time.time()
    baseline_dir = run_dir / "baseline"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = case.profile or infer_weight_profile(case.goal)
    weights = get_weight_profile(profile)
    objective_scope = str(args.objective_scope)
    search_rounds = max(1, int(getattr(args, "search_rounds", 1)))
    feedback_beam_width = max(1, int(getattr(args, "feedback_beam_width", 2)))
    feedback_enabled = (
        str(args.proposal_source) in LLM_FEEDBACK_PROPOSAL_SOURCES
        and search_rounds > 1
    )
    resolved_attempt_budget = _resolved_proposal_attempt_budget(args)
    if feedback_enabled:
        selection_method = (
            "feedback_guided_baseline_frozen_top10_constrained_beam_search"
            if objective_scope == "top10"
            else "feedback_guided_constrained_beam_search_with_baseline_guard"
        )
    else:
        selection_method = (
            "baseline_frozen_top10_constrained_search"
            if objective_scope == "top10"
            else SELECTION_METHOD
        )
    metadata = {
        "case": asdict(case),
        "profile": profile,
        "weights": weights,
        "llm": {
            **resolved_llm_config(),
            "enabled": str(args.proposal_source) in LLM_FEEDBACK_PROPOSAL_SOURCES,
        },
        "started_at": started,
        "python": sys.version,
        "platform": platform.platform(),
        "parameters": {
            "proposal_source": args.proposal_source,
            "proposal_attempt_budget_requested": args.proposal_attempt_budget,
            "proposal_attempt_budget": resolved_attempt_budget,
            "proposal_seed": args.proposal_seed,
            "sumo_budget": args.sumo_budget,
            "grid_edge_pool_size": args.grid_edge_pool_size,
            "optimizer": args.optimizer,
            "objective_scope": objective_scope,
            "bundle_top_k": args.bundle_top_k,
            "max_bundle_evals": args.max_bundle_evals,
            "routing_edge_counts": args.routing_edge_counts,
            "routing_reroute_fraction": args.routing_reroute_fraction,
            "search_rounds": search_rounds,
            "feedback_beam_width": feedback_beam_width,
            "feedback_enabled": feedback_enabled,
            "top_n": args.top_n,
            "subgraph_seed_n": args.subgraph_seed_n,
            "subgraph_hops": args.subgraph_hops,
            "subgraph_max_edges": args.subgraph_max_edges,
        },
    }
    _write_json(run_dir / "00_metadata.json", metadata)

    print(f"\n[1/6] Preparing and running baseline: {case.case_id}")
    baseline_artifacts = prepare_baseline_sandbox(case.cfg, baseline_dir)
    baseline_run = _tool(bridge, "runner", "run", {"cfg_path": baseline_artifacts["cfg"], "gui": False})
    diagnosis_top_n = max(args.top_n, TOP10_EDGE_COUNT) if objective_scope == "top10" else args.top_n
    baseline_kpi = collect_kpi(bridge, baseline_artifacts, diagnosis_top_n)
    diagnosis = add_bottleneck_subgraph(
        bridge,
        baseline_kpi.pop("diagnosis"),
        baseline_artifacts["net"],
        args.subgraph_seed_n,
        args.subgraph_hops,
        args.subgraph_max_edges,
    )
    focus_scope: Optional[Dict[str, Any]] = None
    if objective_scope == "top10":
        focus_scope = freeze_top10_scope(bridge, diagnosis, baseline_artifacts["net"])
        diagnosis["selection_objective_scope"] = focus_scope
        baseline_kpi["focus_scope"] = _collect_scope_metrics(
            bridge,
            baseline_artifacts,
            focus_scope,
        )
    _write_json(run_dir / "01_baseline.json", {"artifacts": baseline_artifacts, "run": baseline_run, "kpi": baseline_kpi})
    _write_json(run_dir / "02_diagnosis.json", diagnosis)

    print("[2/6] Generating and validating optimization proposals")
    state = AgentState.create(goal=case.goal, workspace=str(run_dir))
    state.artifacts.set(
        net=baseline_artifacts["net"],
        routes=baseline_artifacts["routes"],
        cfg=baseline_artifacts["cfg"],
        tripinfo=baseline_artifacts["tripinfo"],
    )
    state.context = {"last_user_input": case.goal, "workspace": str(run_dir)}
    debate = _generate_proposal_source(
        state,
        diagnosis,
        baseline_artifacts,
        case,
        profile,
        args,
    )
    for proposal in debate.get("proposals") or []:
        proposal.setdefault("search_round", 1)
    source_valid_proposals = [
        proposal
        for proposal in debate.get("proposals") or []
        if (proposal.get("validation") or {}).get("ok") and proposal.get("action")
    ]
    valid_proposals, routing_control_audit = _expand_routing_variants(
        source_valid_proposals,
        args.routing_edge_counts,
        args.routing_reroute_fraction,
        baseline_artifacts,
    )
    valid_proposals, deduplication_audit = _deduplicate_evaluation_proposals(valid_proposals)
    debate["routing_experiment_control"] = {
        "requested_edge_counts": args.routing_edge_counts,
        "fixed_reroute_fraction": args.routing_reroute_fraction,
        "variants": routing_control_audit,
    }
    debate["deduplication"] = deduplication_audit
    # Keep an independent audit list.  Sharing the same list object caused each
    # feedback proposal to be appended twice (once through valid_proposals and
    # once through debate["evaluation_proposals"]).
    debate["evaluation_proposals"] = list(valid_proposals)
    feedback_search: Dict[str, Any] = {
        "enabled": feedback_enabled,
        "requested_search_rounds": search_rounds,
        "beam_width": feedback_beam_width,
        "operator": "hybrid_feedback_search",
        "scheduled_operator": "single_argument_llm_mutation",
        "rounds": [],
    }
    if search_rounds > 1 and not feedback_enabled:
        feedback_search["skip_reason"] = (
            "simulation-feedback refinement requires an LLM feedback proposal source"
        )
    debate["feedback_search"] = feedback_search
    _write_json(run_dir / "03_debate.json", debate)
    for proposal in debate.get("proposals") or []:
        validation = proposal.get("validation") or {}
        print(f"      {proposal.get('agent')}: {proposal.get('action')} score={proposal.get('score')} ok={validation.get('ok')}")
    budget_text = (
        f" with a budget of {args.sumo_budget} SUMO run(s)"
        if args.sumo_budget is not None
        else ""
    )
    print(
        f"[3/6] Evaluating up to {len(valid_proposals)} validated proposal(s)"
        f" in independent SUMO runs{budget_text}"
    )
    candidates: List[Dict[str, Any]] = [
        _baseline_candidate(
            baseline_artifacts,
            baseline_run,
            baseline_kpi,
            diagnosis,
            weights,
            objective_scope,
        )
    ]
    sumo_evaluation_count = 0
    evaluated_single_count = 0
    for proposal in valid_proposals:
        if args.sumo_budget is not None and sumo_evaluation_count >= args.sumo_budget:
            break
        proposal_id = str(proposal.get("proposal_id") or proposal.get("agent") or "candidate")
        print(f"      running {proposal.get('agent')}: {proposal.get('action')}")
        candidate = _evaluate_proposal(
            bridge,
            proposal,
            run_dir / "candidates" / proposal_id,
            baseline_artifacts,
            baseline_kpi,
            weights,
            diagnosis_top_n,
            focus_scope,
        )
        candidates.append(candidate)
        evaluated_single_count += 1
        if candidate.get("sumo_evaluated"):
            sumo_evaluation_count += 1
        summary = _candidate_summary(candidate, profile, objective_scope)
        if candidate.get("status") == "ok":
            print(
                f"        kpi_score={summary['kpi_score']:.6f} "
                f"delta={summary['objective_delta']:.6f} improved={summary['improved']}"
            )
        else:
            print(f"        failed: {summary['error']}")

    evaluated_signatures = {
        _proposal_semantic_signature(candidate.get("proposal") or {})
        for candidate in candidates
        if not candidate.get("is_baseline") and candidate.get("proposal")
    }

    def evaluate_feedback_refinement(
        parent_candidate: Mapping[str, Any],
        *,
        round_index: int,
        round_record: Dict[str, Any],
        history: List[Mapping[str, Any]],
        phase: str = "scheduled",
        mutation_index: int = 1,
    ) -> bool:
        """Generate, deduplicate, and optionally simulate one local mutation."""
        nonlocal sumo_evaluation_count, evaluated_single_count
        parent = dict(parent_candidate.get("proposal") or {})
        parent_id = str(parent.get("proposal_id") or "parent")
        feedback = _candidate_feedback(
            parent_candidate,
            baseline_kpi,
            profile=profile,
            objective_scope=objective_scope,
        )
        refinement_record: Dict[str, Any] = {
            "parent_proposal_id": parent_id,
            "phase": phase,
            "mutation_index": int(mutation_index),
            "simulation_feedback": feedback,
        }
        try:
            refined = run_feedback_refinement(
                state,
                parent_proposal=parent,
                simulation_feedback=feedback,
                diagnosis=diagnosis,
                evaluated_history=history,
                user_request=case.goal,
                objective_profile=profile,
                search_round=round_index,
                mutation_index=mutation_index,
                call_llm_fn=call_llm,
            )
        except Exception as exc:
            refinement_record.update(
                {
                    "status": "generation_error",
                    "error": str(exc),
                }
            )
            round_record["refinements"].append(refinement_record)
            print(f"      feedback refinement for {parent_id} failed: {exc}")
            return False

        debate.setdefault("proposals", []).append(refined)
        refinement_record["proposal_id"] = refined.get("proposal_id")
        refinement_record["changed_fields"] = (
            refined.get("refinement") or {}
        ).get("changed_fields")
        validation = dict(refined.get("validation") or {})
        if not validation.get("ok"):
            refinement_record.update(
                {
                    "status": "validation_failed",
                    "validation": validation,
                }
            )
            round_record["refinements"].append(refinement_record)
            print(
                f"      feedback refinement for {parent_id} rejected: "
                f"{validation.get('message')}"
            )
            return False

        signature = _proposal_semantic_signature(refined)
        if signature in evaluated_signatures:
            validation["ok"] = False
            validation["error_type"] = "DUPLICATE_FEEDBACK_REFINEMENT"
            validation["message"] = "Feedback refinement duplicates an evaluated candidate."
            refined["validation"] = validation
            refinement_record.update(
                {
                    "status": "duplicate_skipped",
                    "validation": validation,
                }
            )
            deduplication_audit.append(
                {
                    "proposal_id": refined.get("proposal_id"),
                    "status": "duplicate_skipped",
                    "search_round": round_index,
                    "phase": phase,
                }
            )
            round_record["refinements"].append(refinement_record)
            print(f"      feedback refinement for {parent_id} duplicated prior work")
            return False

        evaluated_signatures.add(signature)
        valid_proposals.append(refined)
        debate.setdefault("evaluation_proposals", []).append(refined)
        deduplication_audit.append(
            {
                "proposal_id": refined.get("proposal_id"),
                "status": "unique",
                "search_round": round_index,
                "phase": phase,
            }
        )
        proposal_id = str(refined.get("proposal_id") or f"feedback_{round_index}")
        print(
            f"      running refinement {proposal_id}: "
            f"changed {', '.join((refined.get('refinement') or {}).get('changed_fields') or [])}"
        )
        candidate = _evaluate_proposal(
            bridge,
            refined,
            run_dir / "candidates" / proposal_id,
            baseline_artifacts,
            baseline_kpi,
            weights,
            diagnosis_top_n,
            focus_scope,
        )
        candidates.append(candidate)
        evaluated_single_count += 1
        was_evaluated = bool(candidate.get("sumo_evaluated"))
        if was_evaluated:
            sumo_evaluation_count += 1
        summary = _candidate_summary(candidate, profile, objective_scope)
        refinement_record.update(
            {
                "status": "evaluated" if candidate.get("status") == "ok" else "evaluation_failed",
                "sumo_evaluated": was_evaluated,
                "result": summary,
            }
        )
        round_record["refinements"].append(refinement_record)
        if candidate.get("status") == "ok":
            print(
                f"        kpi_score={summary['kpi_score']:.6f} "
                f"delta={summary['objective_delta']:.6f} improved={summary['improved']}"
            )
        else:
            print(f"        failed: {summary['error']}")
        return was_evaluated

    refinement_branch_counts: Dict[str, int] = {}
    for search_round in range(2, search_rounds + 1):
        if not feedback_enabled:
            break
        remaining_budget = None
        if args.sumo_budget is not None:
            remaining_budget = max(0, int(args.sumo_budget) - sumo_evaluation_count)
        if remaining_budget == 0:
            feedback_search["rounds"].append(
                {
                    "round": search_round,
                    "status": "skipped",
                    "reason": "sumo_budget_exhausted",
                    "parents": [],
                    "refinements": [],
                }
            )
            break

        round_width = feedback_beam_width
        if remaining_budget is not None:
            round_width = min(round_width, remaining_budget)
        parents = _feedback_parent_candidates(
            candidates,
            profile=profile,
            objective_scope=objective_scope,
            beam_width=round_width,
        )
        round_record: Dict[str, Any] = {
            "round": search_round,
            "status": "running" if parents else "skipped",
            "parents": [
                (candidate.get("proposal") or {}).get("proposal_id")
                for candidate in parents
            ],
            "refinements": [],
        }
        if not parents:
            round_record["reason"] = "no_successfully_simulated_parent"
            feedback_search["rounds"].append(round_record)
            break

        print(
            f"[3a/6] Feedback search round {search_round}/{search_rounds}: "
            f"refining {len(parents)} beam parent(s)"
        )
        history = _feedback_history(
            candidates,
            profile=profile,
            objective_scope=objective_scope,
        )
        evaluated_this_round = 0
        for parent_candidate in parents:
            if args.sumo_budget is not None and sumo_evaluation_count >= args.sumo_budget:
                break
            parent_id = str(
                (parent_candidate.get("proposal") or {}).get("proposal_id") or "parent"
            )
            mutation_index = refinement_branch_counts.get(parent_id, 0) + 1
            refinement_branch_counts[parent_id] = mutation_index
            if evaluate_feedback_refinement(
                parent_candidate,
                round_index=search_round,
                round_record=round_record,
                history=history,
                mutation_index=mutation_index,
            ):
                evaluated_this_round += 1
        round_record["status"] = "completed"
        round_record["sumo_evaluations"] = evaluated_this_round
        feedback_search["rounds"].append(round_record)
        if evaluated_this_round == 0:
            break

    feedback_search["generated_refinement_count"] = sum(
        len(record.get("refinements") or [])
        for record in feedback_search["rounds"]
    )
    feedback_search["evaluated_refinement_count"] = sum(
        int(record.get("sumo_evaluations") or 0)
        for record in feedback_search["rounds"]
    )
    debate["deduplication"] = deduplication_audit
    debate["feedback_search"] = feedback_search
    _write_json(run_dir / "03_debate.json", debate)

    successful_singles = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "ok" and not candidate.get("is_baseline")
    ]
    single_quality = {}
    for candidate in successful_singles:
        proposal_id = str((candidate.get("proposal") or {}).get("proposal_id") or "")
        if objective_scope == "top10":
            scope_score = (candidate.get("scope_objective") or {}).get("score")
            single_quality[proposal_id] = -float(
                scope_score if scope_score is not None else float("inf")
            )
        else:
            single_quality[proposal_id] = -float(candidate["kpi_score"])
    single_gain = {
        str((candidate.get("proposal") or {}).get("proposal_id") or ""): -float(
            (candidate.get("objective") or {}).get("delta") or 0.0
        )
        for candidate in successful_singles
    }
    bundle_budget = int(args.max_bundle_evals)
    if args.sumo_budget is not None:
        bundle_budget = min(
            bundle_budget,
            max(0, int(args.sumo_budget) - sumo_evaluation_count),
        )
    pair_bundles = generate_pair_bundles(
        [candidate["proposal"] for candidate in successful_singles],
        top_k=args.bundle_top_k,
        max_bundles=bundle_budget,
        quality_by_id=single_quality,
    )
    if pair_bundles:
        print(f"[3b/6] Evaluating {len(pair_bundles)} compatible two-action bundle(s)")
    for bundle in pair_bundles:
        if args.sumo_budget is not None and sumo_evaluation_count >= args.sumo_budget:
            break
        bundle_id = str(bundle["bundle_id"])
        action_names = [str(action.get("action")) for action in bundle.get("actions") or []]
        print(f"      running {bundle_id}: {' -> '.join(action_names)}")
        candidate = _evaluate_bundle(
            bridge,
            bundle,
            run_dir / "bundles" / bundle_id,
            baseline_artifacts,
            baseline_kpi,
            weights,
            diagnosis_top_n,
            single_gain,
            focus_scope,
        )
        candidates.append(candidate)
        if candidate.get("sumo_evaluated"):
            sumo_evaluation_count += 1
        summary = _candidate_summary(candidate, profile, objective_scope)
        if candidate.get("status") == "ok":
            print(
                f"        kpi_score={summary['kpi_score']:.6f} "
                f"delta={summary['objective_delta']:.6f} improved={summary['improved']}"
            )
        else:
            print(f"        failed: {summary['error']}")

    best_so_far_trace = _best_so_far_trace(
        candidates,
        profile=profile,
        objective_scope=objective_scope,
    )
    ranked = sorted(
        (_candidate_summary(candidate, profile, objective_scope) for candidate in candidates),
        key=lambda row: (
            row["status"] != "ok",
            not row["selection_eligible"],
            (
                float(row["scope_score"])
                if objective_scope == "top10" and row["scope_score"] is not None
                else float(row["kpi_score"])
                if row["kpi_score"] is not None
                else float("inf")
            ),
        ),
    )
    for rank, row in enumerate((row for row in ranked if row["status"] == "ok"), 1):
        row["rank"] = rank
    _write_json(
        run_dir / "04_candidates.json",
        {
            "selection_method": selection_method,
            "selection_policy": _selection_policy(profile, objective_scope),
            "objective_scope": focus_scope if focus_scope else {"name": "global"},
            "lower_is_better": True,
            "single_candidate_count": evaluated_single_count,
            "ready_single_candidate_count": len(valid_proposals),
            "bundle_candidate_count": len(pair_bundles),
            "baseline_candidate_count": 1,
            "sumo_budget": args.sumo_budget,
            "sumo_evaluation_count": sumo_evaluation_count,
            "proposal_source": args.proposal_source,
            "feedback_search": feedback_search,
            "best_so_far_trace": best_so_far_trace,
            "routing_experiment_control": debate["routing_experiment_control"],
            "judge_advisory_selected_proposal_id": (debate.get("judge") or {}).get("selected_proposal_id"),
            "candidates": ranked,
        },
    )

    best = _select_best_candidate(candidates, profile, objective_scope)
    if best is None:
        raise RuntimeError("Baseline guard could not create a selectable candidate")
    selected = dict(best["selected_action"])
    selected_proposal = dict(best["proposal"])
    modification = dict(best["modification"])
    optimized_record = dict(best["optimized"])
    optimized_kpi = dict(optimized_record["kpi"])
    comparison = dict(best["objective"])
    source_metrics = _proposal_source_metrics(
        list(debate.get("proposals") or []),
        [candidate for candidate in candidates if not candidate.get("is_baseline")],
        baseline_kpi,
        best,
        proposal_source=args.proposal_source,
        sumo_budget=args.sumo_budget,
        objective_scope=objective_scope,
    )
    _write_json(run_dir / "07_proposal_source_metrics.json", source_metrics)

    baseline_retained = bool(best.get("is_baseline"))
    print(f"[4/6] KPI winner: {selected_proposal.get('agent')} / {selected.get('action')}")
    _write_json(
        run_dir / "04_modification.json",
        {"selection_method": selection_method, "selected_proposal": selected_proposal, "selected_action": selected, "result": modification},
    )
    _write_json(run_dir / "05_optimized.json", optimized_record)

    print("[5/6] Writing baseline comparison and candidate ranking")
    result = {
        "status": "ok",
        "case_id": case.case_id,
        "seed": case.seed,
        "profile": profile,
        "proposal_source": args.proposal_source,
        "baseline_kpi": baseline_kpi,
        "optimized_kpi": optimized_kpi,
        "objective": comparison,
        "selection_method": selection_method,
        "selection_policy": _selection_policy(profile, objective_scope),
        "objective_scope": focus_scope if focus_scope else {"name": "global"},
        "scope_objective": best.get("scope_objective"),
        "baseline_retained": baseline_retained,
        "optimization_applied": not baseline_retained,
        "selected_proposal": selected_proposal,
        "selected_action": selected,
        "candidate_ranking": ranked,
        "judge_advisory": debate.get("judge"),
        "proposal_source_metrics": source_metrics,
        "feedback_search": feedback_search,
        "best_so_far_trace": best_so_far_trace,
        "sumo_evaluation_count": sumo_evaluation_count,
        "sumo_budget": args.sumo_budget,
        "elapsed_s": time.time() - started,
    }
    metadata["llm"]["token_usage"] = state.context.get("token_usage", {})
    metadata["llm"]["compatibility_fallbacks"] = state.context.get(
        "llm_compatibility_fallbacks",
        [],
    )
    _write_json(run_dir / "00_metadata.json", metadata)
    _write_json(run_dir / "06_comparison.json", result)
    print(
        f"[6/6] Done: improved={comparison['improved']} "
        f"objective_delta={comparison['delta']:.6f} baseline_retained={baseline_retained}"
    )
    return result


def load_cases(args: argparse.Namespace) -> List[ExperimentCase]:
    if args.manifest:
        cases: List[ExperimentCase] = []
        with open(args.manifest, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                cases.append(
                    ExperimentCase(
                        case_id=str(item.get("id") or f"case_{line_no:03d}"),
                        cfg=str(item["cfg"]),
                        goal=str(item.get("goal") or args.goal),
                        profile=item.get("profile"),
                        seed=int(item["seed"]) if item.get("seed") is not None else None,
                    )
                )
        return cases
    if not args.cfg:
        raise ValueError("Provide --cfg for one scenario or --manifest for a batch")
    return [ExperimentCase(args.case_id, args.cfg, args.goal, args.profile)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Optimize existing SUMO simulations without running the full agent workflow")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--cfg", help="Existing SUMO .sumocfg")
    source.add_argument("--manifest", help="JSONL file with id, cfg, goal, and optional profile")
    parser.add_argument("--case-id", default="scenario")
    parser.add_argument("--goal", default="reduce traffic delay")
    parser.add_argument("--profile", choices=["balanced", "delay", "throughput", "emissions"])
    parser.add_argument(
        "--objective-scope",
        choices=["global", "top10"],
        default="global",
        help=(
            "Select by the existing network-wide objective or minimize baseline-frozen "
            "Top-10 edge time loss with global-delay and one-hop spillover constraints."
        ),
    )
    parser.add_argument(
        "--optimizer",
        choices=["all", "signal", "network", "routing"],
        default="all",
        help="Run all proposal agents or force one specialist through modification and SUMO evaluation",
    )
    parser.add_argument(
        "--proposal-source",
        choices=[
            "llm",
            "random_initial_llm",
            "grounded_random",
            "deterministic_heuristic",
            "grounded_grid",
        ],
        default="llm",
        help=(
            "Generate initial candidates with specialist LLMs, grounded random "
            "initialization followed by LLM feedback, or a non-LLM baseline."
        ),
    )
    parser.add_argument(
        "--proposal-attempt-budget",
        type=int,
        help=(
            "Maximum raw proposals generated before validation. Defaults to the "
            "SUMO budget in one-shot mode; feedback mode reserves later evaluations "
            "and defaults to one initial proposal per active specialist."
        ),
    )
    parser.add_argument(
        "--sumo-budget",
        type=int,
        help="Maximum candidate SUMO evaluations, excluding the common baseline run.",
    )
    parser.add_argument(
        "--search-rounds",
        type=int,
        default=1,
        help=(
            "Number of simulation-grounded proposal rounds. Values above one enable "
            "LLM feedback refinement after each measured beam; use 2 for the initial version."
        ),
    )
    parser.add_argument(
        "--feedback-beam-width",
        type=int,
        default=2,
        help="Number of measured parent candidates refined in each feedback round.",
    )
    parser.add_argument(
        "--proposal-seed",
        type=int,
        help="Seed for grounded random proposal sampling; defaults to the case demand seed.",
    )
    parser.add_argument(
        "--grid-edge-pool-size",
        type=int,
        default=3,
        help="Number of highest-ranked diagnosed edges used by the optional grounded grid.",
    )
    parser.add_argument("--output-dir", default="experiment/optimization-only")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Independent proposal-generation repetitions per case",
    )
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--subgraph-seed-n", type=int, default=5)
    parser.add_argument("--subgraph-hops", type=int, default=1)
    parser.add_argument("--subgraph-max-edges", type=int, default=80)
    parser.add_argument(
        "--routing-edge-counts",
        type=_parse_routing_edge_counts,
        help=(
            "Controlled routing ablation from one LLM proposal, e.g. 1,3. "
            "Each variant uses the first N ordered avoid_edges from the same model output."
        ),
    )
    parser.add_argument(
        "--routing-reroute-fraction",
        type=float,
        choices=[0.25, 0.5, 0.75, 1.0],
        help="Override reroute_fraction for every controlled routing variant.",
    )
    parser.add_argument(
        "--bundle-top-k",
        type=int,
        default=3,
        help="Expand pairs only from the top-K successfully simulated single proposals",
    )
    parser.add_argument(
        "--max-bundle-evals",
        type=int,
        default=3,
        help="Maximum number of compatible two-action bundles to simulate; use 0 to disable",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.proposal_attempt_budget is not None and args.proposal_attempt_budget <= 0:
        raise ValueError("--proposal-attempt-budget must be positive")
    if args.sumo_budget is not None and args.sumo_budget <= 0:
        raise ValueError("--sumo-budget must be positive")
    if args.search_rounds <= 0:
        raise ValueError("--search-rounds must be positive")
    if args.feedback_beam_width <= 0:
        raise ValueError("--feedback-beam-width must be positive")
    if args.grid_edge_pool_size <= 0:
        raise ValueError("--grid-edge-pool-size must be positive")
    if (
        args.proposal_source in LLM_FEEDBACK_PROPOSAL_SOURCES
        and args.search_rounds > 1
        and not os.getenv("OPENAI_API_KEY")
    ) or (args.proposal_source == "llm" and not os.getenv("OPENAI_API_KEY")):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it before running, for example: "
            "export OPENAI_API_KEY='your-key'"
        )
    cases = load_cases(args)
    output_root = Path(args.output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    bridge = LocalToolBridge()
    manifest: List[Dict[str, Any]] = []
    for case in cases:
        for repetition in range(1, max(1, args.runs) + 1):
            args._repetition = repetition
            run_dir = output_root / case.case_id / f"run_{repetition:03d}"
            try:
                result = run_case(bridge, case, run_dir, args)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                result = {
                    "status": "error",
                    "case_id": case.case_id,
                    "repetition": repetition,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
                _write_json(run_dir / "error.json", result)
                print(f"[!] {case.case_id} run {repetition} failed: {exc}", file=sys.stderr)
            manifest.append({"case_id": case.case_id, "repetition": repetition, **result})
            _write_json(output_root / "run_manifest.json", manifest)
    failures = sum(1 for item in manifest if item.get("status") != "ok")
    print(f"\nManifest: {output_root / 'run_manifest.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
