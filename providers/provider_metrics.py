# providers/provider_metrics.py
# FastMCP metrics provider (no stdout pollution; JSON-only returns)
import os
import sys
import logging
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Tuple, Optional, Set

try:
    from fastmcp import FastMCP
except ImportError:
    # 兼容老分发名
    from mcp.server.fastmcp import FastMCP  # type: ignore

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
mcp = FastMCP("traffic-metrics")


# ---------------------------
# Helpers (pure functions)
# ---------------------------

def _require_file(path: str, name: str) -> None:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"{name} not found: {path}")

def _parse_tripinfo(tripinfo_xml: str) -> List[ET.Element]:
    _require_file(tripinfo_xml, "tripinfo xml")
    root = ET.parse(tripinfo_xml).getroot()
    return list(root.iter("tripinfo"))

def _parse_edgedata(edgedata_xml: str) -> List[Tuple[str, float]]:
    _require_file(edgedata_xml, "edgedata xml")
    root = ET.parse(edgedata_xml).getroot()
    pairs: List[Tuple[str, float]] = []
    for interval in root.findall("interval"):
        for edge in interval.findall("edge"):
            eid = edge.get("id")
            density = float(edge.get("density", "0"))
            if eid:
                pairs.append((eid, density))
    return pairs

def _f(x: Optional[str], default: float = 0.0) -> float:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default

def _parse_net_edges(net_file: str) -> Dict[str, Dict[str, Any]]:
    """Read basic edge metadata from SUMO net.xml."""
    _require_file(net_file, "net file")
    root = ET.parse(net_file).getroot()
    edges: Dict[str, Dict[str, Any]] = {}
    for edge in root.findall("edge"):
        eid = edge.get("id")
        if not eid or eid.startswith(":"):
            continue
        lanes = edge.findall("lane")
        lane_lengths = [_f(l.get("length")) for l in lanes]
        lane_speeds = [_f(l.get("speed")) for l in lanes]
        edges[eid] = {
            "edge_id": eid,
            "name": edge.get("name") or eid,
            "from_junction": edge.get("from"),
            "to_junction": edge.get("to"),
            "priority": _f(edge.get("priority")),
            "lane_count": len(lanes),
            "length_m": max(lane_lengths) if lane_lengths else 0.0,
            "max_speed_mps": max(lane_speeds) if lane_speeds else 0.0,
        }
    return edges

def _parse_net_junctions(net_file: str) -> Dict[str, Dict[str, Any]]:
    _require_file(net_file, "net file")
    root = ET.parse(net_file).getroot()
    junctions: Dict[str, Dict[str, Any]] = {}
    for junction in root.findall("junction"):
        jid = junction.get("id")
        if not jid or jid.startswith(":"):
            continue
        junctions[jid] = {
            "junction_id": jid,
            "type": junction.get("type"),
            "x": _f(junction.get("x")),
            "y": _f(junction.get("y")),
            "incoming_lanes": [x for x in (junction.get("incLanes") or "").split() if x],
            "internal_lanes": [x for x in (junction.get("intLanes") or "").split() if x],
        }
    return junctions

def _parse_net_connections(net_file: str) -> Tuple[List[Dict[str, Any]], Dict[str, Set[str]], Dict[str, Set[str]]]:
    _require_file(net_file, "net file")
    root = ET.parse(net_file).getroot()
    connections: List[Dict[str, Any]] = []
    outgoing: Dict[str, Set[str]] = {}
    incoming: Dict[str, Set[str]] = {}
    for conn in root.findall("connection"):
        src = conn.get("from")
        dst = conn.get("to")
        if not src or not dst or src.startswith(":") or dst.startswith(":"):
            continue
        record = {
            "from_edge": src,
            "to_edge": dst,
            "from_lane": conn.get("fromLane"),
            "to_lane": conn.get("toLane"),
            "via": conn.get("via"),
            "tl_id": conn.get("tl"),
            "link_index": conn.get("linkIndex"),
            "direction": conn.get("dir"),
            "state": conn.get("state"),
        }
        connections.append(record)
        outgoing.setdefault(src, set()).add(dst)
        incoming.setdefault(dst, set()).add(src)
    return connections, outgoing, incoming

def _bottleneck_subgraph_data(
    net_file: str,
    edge_ids: List[str],
    hops: int = 1,
    max_edges: int = 80,
) -> Dict[str, Any]:
    """
    Build a compact local road graph around bottleneck edges.

    The result is intentionally small enough for LLM evidence: seed edges,
    upstream/downstream neighbors, local connections, junction metadata, and
    TLS ids observed on local SUMO <connection> elements.
    """
    net_edges = _parse_net_edges(net_file)
    junctions = _parse_net_junctions(net_file)
    connections, outgoing, incoming = _parse_net_connections(net_file)

    seeds = []
    seen_seed = set()
    for eid in edge_ids or []:
        if eid in net_edges and eid not in seen_seed:
            seeds.append(eid)
            seen_seed.add(eid)

    included: Set[str] = set(seeds)
    frontier: Set[str] = set(seeds)
    for _ in range(max(0, int(hops))):
        nxt: Set[str] = set()
        for eid in frontier:
            nxt.update(outgoing.get(eid, set()))
            nxt.update(incoming.get(eid, set()))
        nxt = {eid for eid in nxt if eid in net_edges and eid not in included}
        room = max(0, int(max_edges) - len(included))
        if room <= 0:
            break
        limited = set(sorted(nxt)[:room])
        included.update(limited)
        frontier = limited
        if not frontier:
            break

    local_connections = [
        c for c in connections
        if c["from_edge"] in included and c["to_edge"] in included
    ]
    local_junction_ids = {
        str(v)
        for eid in included
        for v in (
            net_edges.get(eid, {}).get("from_junction"),
            net_edges.get(eid, {}).get("to_junction"),
        )
        if v
    }
    tls_ids = sorted({
        str(c.get("tl_id"))
        for c in local_connections
        if c.get("tl_id")
    })

    def edge_record(eid: str) -> Dict[str, Any]:
        meta = dict(net_edges[eid])
        meta["is_seed_bottleneck"] = eid in seen_seed
        meta["incoming_edges"] = sorted(e for e in incoming.get(eid, set()) if e in included)
        meta["outgoing_edges"] = sorted(e for e in outgoing.get(eid, set()) if e in included)
        return meta

    return {
        "seed_edge_ids": seeds,
        "hops": int(hops),
        "edge_count": len(included),
        "connection_count": len(local_connections),
        "tls_ids": tls_ids,
        "edges": [edge_record(eid) for eid in sorted(included)],
        "connections": local_connections,
        "junctions": [
            junctions[jid] for jid in sorted(local_junction_ids)
            if jid in junctions
        ],
        "sources": {"net_file": os.path.abspath(net_file)},
    }

def _parse_edgedata_records(edgedata_xml: str) -> List[Dict[str, float | str]]:
    _require_file(edgedata_xml, "edgedata xml")
    root = ET.parse(edgedata_xml).getroot()
    records: List[Dict[str, float | str]] = []
    for interval in root.findall("interval"):
        begin = _f(interval.get("begin"))
        end = _f(interval.get("end"))
        period = max(0.0, end - begin)
        for edge in interval.findall("edge"):
            eid = edge.get("id")
            if not eid:
                continue
            records.append({
                "edge_id": eid,
                "period_s": period,
                "sampled_seconds": _f(edge.get("sampledSeconds")),
                "density": _f(edge.get("density")),
                "occupancy": _f(edge.get("occupancy")),
                "speed": _f(edge.get("speed")),
                "travel_time": _f(edge.get("traveltime")),
                "waiting_time": _f(edge.get("waitingTime")),
                "time_loss": _f(edge.get("timeLoss")),
                "departed": _f(edge.get("departed")),
                "arrived": _f(edge.get("arrived")),
                "entered": _f(edge.get("entered")),
                "left": _f(edge.get("left")),
            })
    return records


def _edge_group_performance_data(
    edgedata_xml: str,
    edge_ids: List[str],
) -> Dict[str, Any]:
    """Aggregate comparable edgeData metrics for one baseline-frozen edge set."""
    requested = list(dict.fromkeys(str(edge_id) for edge_id in edge_ids or [] if edge_id))
    requested_set = set(requested)
    records = [
        record
        for record in _parse_edgedata_records(edgedata_xml)
        if str(record["edge_id"]) in requested_set
    ]
    observed = sorted({str(record["edge_id"]) for record in records})
    sampled_seconds = sum(float(record["sampled_seconds"]) for record in records)
    speed_numerator = sum(
        float(record["speed"]) * float(record["sampled_seconds"])
        for record in records
    )
    volume_proxy = sum(
        max(
            float(record["entered"]),
            float(record["left"]),
            float(record["departed"]),
            float(record["arrived"]),
        )
        for record in records
    )
    return {
        "available": bool(requested),
        "requested_edge_count": len(requested),
        "observed_edge_count": len(observed),
        "edge_ids": requested,
        "observed_edge_ids": observed,
        "missing_edge_ids": [edge_id for edge_id in requested if edge_id not in set(observed)],
        "total_time_loss_s": sum(float(record["time_loss"]) for record in records),
        "total_waiting_time_s": sum(float(record["waiting_time"]) for record in records),
        "sampled_seconds": sampled_seconds,
        "volume_proxy": volume_proxy,
        "sampled_speed_mps": speed_numerator / sampled_seconds if sampled_seconds > 0 else 0.0,
        "source": os.path.abspath(edgedata_xml),
    }

def _avg(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0

def _pctl(vals: List[float], p: float) -> float:
    if not vals:
        return 0.0
    xs = sorted(vals)
    idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return xs[idx]

def _norm(v: float, hi: float) -> float:
    if hi <= 0:
        return 0.0
    return max(0.0, min(1.0, v / hi))

def _trip_completion_summary(root: ET.Element, completed: int) -> Dict[str, Any]:
    """Read SUMO's end-of-run vehicle counters when present."""
    vehicles = root.find("./statistics/vehicles")
    if vehicles is None:
        departed = completed
        running = waiting = 0
    else:
        departed = int(_f(vehicles.get("inserted") or vehicles.get("departed")))
        running = int(_f(vehicles.get("running")))
        waiting = int(_f(vehicles.get("waiting")))
        # Older SUMO outputs may omit inserted/departed.
        if departed <= 0:
            departed = completed + running

    departed = max(departed, completed)
    unfinished = max(0, departed - completed)
    return {
        "completed_trips": completed,
        "departed_trips": departed,
        "unfinished_trips": unfinished,
        "running_trips": running,
        "waiting_to_depart": waiting,
        "completion_rate": (completed / departed) if departed > 0 else 0.0,
    }


def _trip_delay_summary(
    tripinfo_xml: Optional[str],
    unfinished_penalty_s: float = 300.0,
) -> Dict[str, Any]:
    if not tripinfo_xml or not os.path.isfile(tripinfo_xml):
        return {"available": False}
    root = ET.parse(tripinfo_xml).getroot()
    trips = list(root.findall("tripinfo"))
    durations = [_f(t.get("duration")) for t in trips]
    route_lengths = [_f(t.get("routeLength")) for t in trips]
    waiting_times = [_f(t.get("waitingTime")) for t in trips]
    time_losses = [_f(t.get("timeLoss")) for t in trips]
    completion = _trip_completion_summary(root, len(trips))
    penalized_total = sum(time_losses) + completion["unfinished_trips"] * max(0.0, unfinished_penalty_s)
    penalized_denominator = completion["departed_trips"]
    return {
        "available": True,
        "trip_count": len(trips),
        "avg_duration_s": _avg(durations),
        "p95_duration_s": _pctl(durations, 0.95),
        "avg_route_length_m": _avg(route_lengths),
        "avg_waiting_time_s": _avg(waiting_times),
        "p95_waiting_time_s": _pctl(waiting_times, 0.95),
        "avg_time_loss_s": _avg(time_losses),
        "p95_time_loss_s": _pctl(time_losses, 0.95),
        "unfinished_penalty_s": max(0.0, unfinished_penalty_s),
        "penalized_avg_time_loss_s": (
            penalized_total / penalized_denominator if penalized_denominator > 0 else 0.0
        ),
        **completion,
        "source": os.path.abspath(tripinfo_xml),
    }

def _get_edge_name_from_net(net_file: str, edge_id: str) -> str:
    # optional nicety; if missing, just return None/edge_id
    try:
        _require_file(net_file, "net file")
        root = ET.parse(net_file).getroot()
        for e in root.findall("edge"):
            if e.get("id") == edge_id:
                return e.get("name") or edge_id
    except Exception:
        pass
    return edge_id

# ---------------------------
# Tools
# ---------------------------

@mcp.tool()
def health() -> Dict[str, Any]:
    """Liveness probe."""
    return {"ok": True}


def _tripinfo_mean_speed_data(path: str, unit: str = "m/s") -> Dict[str, Any]:
    trips = _parse_tripinfo(path)

    speeds: list[float] = []

    for t in trips:
        L = float(t.get("routeLength", "0"))   # meters
        T = float(t.get("duration", "0"))      # seconds
        if T > 0:
            speeds.append(L / T)               # m/s

    mean_of_ratios = (sum(speeds) / len(speeds)) if speeds else 0.0

    # 单位换算
    factor = 3.6 if unit == "km/h" else 1.0
    return {
        "value": 
            mean_of_ratios * factor,
        "count": len(speeds),
        "source": os.path.abspath(path),
    }


def _tripinfo_travel_time_summary_data(path: str, top_n: int = 10) -> Dict[str, Any]:
    trips = _parse_tripinfo(path)
    durations: Dict[str, float] = {}
    for t in trips:
        tid = t.get("id") or ""
        if not tid:
            continue
        durations[tid] = float(t.get("duration", "0"))
    if not durations:
        return {
            "avg_travel_time_s": 0.0,
            "top_longest": [],
            "count": 0,
            "source": os.path.abspath(path),
        }
    avg_tt = sum(durations.values()) / len(durations)
    top = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[: max(0, top_n)]
    return {
        "avg_travel_time_s": avg_tt,
        "top_longest": [{"trip_id": k, "duration_s": v} for k, v in top],
        "count": len(durations),
        "source": os.path.abspath(path),
    }


def _emissions_total_data(path: str, unit: str = "g") -> Dict[str, Any]:
    trips = _parse_tripinfo(path)
    totals = {
        "CO2_abs": 0.0,
        "CO_abs": 0.0,
        "NOx_abs": 0.0,
        "PMx_abs": 0.0,
        "fuel_abs": 0.0,
        "electricity_abs": 0.0,  # Wh（不做质量单位换算）
    }
    for t in trips:
        em = t.find("emissions")
        if em is None:
            continue
        for key in totals.keys():
            val = float(em.get(key, "0"))
            totals[key] += val

    # 单位换算（假设 SUMO 输出质量以 g 计；若你的仿真为 mg，请在此处统一先 /1000 转 g）
    factor = 1.0
    if unit == "kg":
        factor = 1.0 / 1_000.0
    elif unit == "t":
        factor = 1.0 / 1_000_000.0

    for key in ["CO2_abs", "CO_abs", "NOx_abs", "PMx_abs", "fuel_abs"]:
        totals[key] *= factor

    return {
        "totals": totals,
        "unit": {"mass": unit, "electricity": "Wh"},
        "trips_count": len(trips),
        "source": os.path.abspath(path),
    }


@mcp.tool()
def tripinfo_mean_speed(path: str, unit: str = "m/s") -> Dict[str, Any]:
    """
    以 routeLength/duration 计算平均速度。
    返回两种口径：
      - mean_of_ratios: 对每辆车的 (L/T) 取平均
      - distance_weighted: 总里程 / 总时长（推荐）
    unit: "m/s" 或 "km/h"
    """
    return _tripinfo_mean_speed_data(path, unit=unit)


@mcp.tool()
def tripinfo_travel_time_summary(path: str, top_n: int = 10) -> Dict[str, Any]:
    """
    Summarize travel times:
      - average duration (s)
      - top-N longest trips: [(trip_id, duration_s)]
    """
    return _tripinfo_travel_time_summary_data(path, top_n=top_n)


@mcp.tool()
def emissions_total(
    path: str,
    # analysis.py 中把除 electricity_abs 外的量都 /1e9（从 g→t?）。这里提供单位策略可选：
    unit: str = "g"  # 支持: "g"（SUMO 原值单位，常为 mg? 依 SUMO 配置）, "kg", "t"
) -> Dict[str, Any]:
    """
    Sum <emissions> under each <tripinfo>.
    Keys: CO2_abs, CO_abs, NOx_abs, PMx_abs, fuel_abs, electricity_abs
    unit: convert totals to given unit (g/kg/t) except electricity_abs（单位：Wh）
    """
    return _emissions_total_data(path, unit=unit)

@mcp.tool()
def edgedata_density_summary(
    edgedata_xml: str,
    net_file: Optional[str] = None,
    top_n: int = 10
) -> Dict[str, Any]:
    """
    Aggregate densities from edgeData output:
      - average density per edge
      - top-N edges by average density
    Optionally map edge names using net.xml if provided.
    """
    pairs = _parse_edgedata(edgedata_xml)
    if not pairs:
        return {
            "avg_density_by_edge": {},
            "top_edges": [],
            "source": os.path.abspath(edgedata_xml),
            "net_file": (os.path.abspath(net_file) if net_file else None),
        }

    # 汇总为 edge -> [densities...]
    by_edge: Dict[str, List[float]] = {}
    for eid, d in pairs:
        by_edge.setdefault(eid, []).append(d)

    avg_by_edge = {eid: (sum(vals) / len(vals)) for eid, vals in by_edge.items()}
    top = sorted(avg_by_edge.items(), key=lambda kv: kv[1], reverse=True)[: max(0, top_n)]

    # 如果给了 net_file，尝试映射名称
    top_records = []
    for eid, val in top:
        record = {"edge_id": eid, "avg_density_veh_per_km": val}
        if net_file:
            record["name"] = _get_edge_name_from_net(net_file, eid)
        top_records.append(record)

    return {
        "avg_density_by_edge": avg_by_edge,  # 注意：可能很大；生产可只返回 top 或分页
        "top_edges": top_records,
        "count_edges": len(avg_by_edge),
        "source": os.path.abspath(edgedata_xml),
        "net_file": (os.path.abspath(net_file) if net_file else None),
    }

def _network_diagnosis_data(
    edgedata_xml: str,
    net_file: str,
    tripinfo_xml: Optional[str] = None,
    top_n: int = 10,
    minimum_top_edge_length_m: float = 10.0,
    congestion_speed_ratio: float = 0.45,
    congestion_density: float = 35.0,
    congestion_occupancy: float = 30.0,
) -> Dict[str, Any]:
    """
    Diagnose congestion, delay, bottlenecks, and critical roads.

    Congestion is detected from edgeData using a mixed score:
    - low speed relative to lane speed limit
    - high density
    - high occupancy
    - edge-level waiting time and time loss

    Critical roads are ranked by combined traffic exposure and performance impact.
    """
    net_edges = _parse_net_edges(net_file)
    records = _parse_edgedata_records(edgedata_xml)

    grouped: Dict[str, List[Dict[str, float | str]]] = {}
    for rec in records:
        grouped.setdefault(str(rec["edge_id"]), []).append(rec)

    edge_rows: List[Dict[str, Any]] = []
    for eid, recs in grouped.items():
        meta = net_edges.get(eid, {
            "edge_id": eid,
            "name": eid,
            "length_m": 0.0,
            "max_speed_mps": 0.0,
            "lane_count": 0,
        })
        # Include true stopped traffic (speed=0) but exclude intervals with no
        # sampled vehicles. Weight by interval duration when it is available.
        active = [r for r in recs if float(r["sampled_seconds"]) > 0]
        def weighted_avg(field: str) -> float:
            if not active:
                return 0.0
            weights = [max(float(r["period_s"]), 0.0) for r in active]
            denominator = sum(weights)
            if denominator <= 0:
                return _avg([float(r[field]) for r in active])
            return sum(float(r[field]) * w for r, w in zip(active, weights)) / denominator

        avg_speed = weighted_avg("speed")
        max_speed = float(meta.get("max_speed_mps") or 0.0)
        speed_ratio = (avg_speed / max_speed) if max_speed > 0 and active else 0.0
        avg_density = weighted_avg("density")
        avg_occupancy = weighted_avg("occupancy")
        total_waiting = sum(float(r["waiting_time"]) for r in recs)
        total_time_loss = sum(float(r["time_loss"]) for r in recs)
        total_sampled_seconds = sum(float(r["sampled_seconds"]) for r in recs)
        volume_proxy = sum(
            max(float(r["entered"]), float(r["left"]), float(r["departed"]), float(r["arrived"]))
            for r in recs
        )

        low_speed_score = 1.0 - max(0.0, min(1.0, speed_ratio))
        congestion_score = (
            0.35 * low_speed_score
            + 0.25 * _norm(avg_density, congestion_density)
            + 0.20 * _norm(avg_occupancy, congestion_occupancy)
            + 0.10 * _norm(total_time_loss, 300.0)
            + 0.10 * _norm(total_waiting, 300.0)
        )
        critical_score = (
            0.45 * _norm(total_sampled_seconds, 600.0)
            + 0.25 * _norm(volume_proxy, 50.0)
            + 0.20 * congestion_score
            + 0.10 * _norm(float(meta.get("length_m") or 0.0), 500.0)
        )

        flags = []
        if speed_ratio and speed_ratio < congestion_speed_ratio:
            flags.append("low_speed")
        if avg_density >= congestion_density:
            flags.append("high_density")
        if avg_occupancy >= congestion_occupancy:
            flags.append("high_occupancy")
        if total_time_loss > 0:
            flags.append("time_loss")
        if total_waiting > 0:
            flags.append("waiting")

        edge_rows.append({
            "edge_id": eid,
            "name": meta.get("name") or eid,
            "length_m": meta.get("length_m"),
            "lane_count": meta.get("lane_count"),
            "max_speed_mps": max_speed,
            "avg_speed_mps": avg_speed,
            "speed_ratio": speed_ratio,
            "avg_density_veh_per_km": avg_density,
            "avg_occupancy_pct": avg_occupancy,
            "total_waiting_time_s": total_waiting,
            "total_time_loss_s": total_time_loss,
            "sampled_seconds": total_sampled_seconds,
            "volume_proxy": volume_proxy,
            "congestion_score": congestion_score,
            "critical_score": critical_score,
            "flags": flags,
        })

    # SUMO/netconvert can split roads into very short non-internal fragments.
    # Exclude those fragments before selecting the Top-K congestion scope so
    # network segmentation does not dominate cross-city comparisons.  Do not
    # backfill with short edges when fewer than top_n eligible edges exist.
    minimum_top_edge_length_m = max(0.0, float(minimum_top_edge_length_m))
    top_congested_candidates = [
        edge
        for edge in edge_rows
        if float(edge.get("length_m") or 0.0) >= minimum_top_edge_length_m
    ]
    top_congested = sorted(
        top_congested_candidates,
        key=lambda x: x["congestion_score"],
        reverse=True,
    )[:max(0, top_n)]
    bottlenecks = [
        e for e in sorted(edge_rows, key=lambda x: (x["congestion_score"], x["total_time_loss_s"]), reverse=True)
        if (
            (e["speed_ratio"] and e["speed_ratio"] < congestion_speed_ratio)
            or e["avg_density_veh_per_km"] >= congestion_density
            or e["avg_occupancy_pct"] >= congestion_occupancy
        )
    ][:max(0, top_n)]
    critical_roads = sorted(edge_rows, key=lambda x: x["critical_score"], reverse=True)[:max(0, top_n)]

    return {
        "summary": {
            "edge_count_in_net": len(net_edges),
            "edge_count_with_data": len(edge_rows),
            "top_congested_candidate_count": len(top_congested_candidates),
            "short_edges_excluded_from_top_congested": (
                len(edge_rows) - len(top_congested_candidates)
            ),
            "congested_edge_count": len([
                e for e in edge_rows
                if e["congestion_score"] >= 0.5
                or e["avg_density_veh_per_km"] >= congestion_density
                or (e["speed_ratio"] and e["speed_ratio"] < congestion_speed_ratio)
            ]),
            "avg_network_speed_mps": _avg([e["avg_speed_mps"] for e in edge_rows if e["avg_speed_mps"] > 0]),
            "avg_network_density_veh_per_km": _avg([e["avg_density_veh_per_km"] for e in edge_rows]),
            "total_waiting_time_s": sum(e["total_waiting_time_s"] for e in edge_rows),
            "total_time_loss_s": sum(e["total_time_loss_s"] for e in edge_rows),
        },
            "delay": _trip_delay_summary(tripinfo_xml),
        "top_congested_edges": top_congested,
        "bottlenecks": bottlenecks,
        "critical_roads": critical_roads,
        "thresholds": {
            "minimum_top_edge_length_m": minimum_top_edge_length_m,
            "congestion_speed_ratio": congestion_speed_ratio,
            "congestion_density": congestion_density,
            "congestion_occupancy": congestion_occupancy,
        },
        "sources": {
            "edgedata_xml": os.path.abspath(edgedata_xml),
            "net_file": os.path.abspath(net_file),
            "tripinfo_xml": os.path.abspath(tripinfo_xml) if tripinfo_xml else None,
        },
    }


@mcp.tool()
def network_diagnosis(
    edgedata_xml: str,
    net_file: str,
    tripinfo_xml: Optional[str] = None,
    top_n: int = 10,
    minimum_top_edge_length_m: float = 10.0,
    congestion_speed_ratio: float = 0.45,
    congestion_density: float = 35.0,
    congestion_occupancy: float = 30.0,
) -> Dict[str, Any]:
    return _network_diagnosis_data(
        edgedata_xml=edgedata_xml,
        net_file=net_file,
        tripinfo_xml=tripinfo_xml,
        top_n=top_n,
        minimum_top_edge_length_m=minimum_top_edge_length_m,
        congestion_speed_ratio=congestion_speed_ratio,
        congestion_density=congestion_density,
        congestion_occupancy=congestion_occupancy,
    )


@mcp.tool()
def edge_group_performance(
    edgedata_xml: str,
    edge_ids: List[str],
) -> Dict[str, Any]:
    return _edge_group_performance_data(edgedata_xml, edge_ids)

@mcp.tool()
def bottleneck_subgraph(
    net_file: str,
    edge_ids: List[str],
    hops: int = 1,
    max_edges: int = 80,
) -> Dict[str, Any]:
    return _bottleneck_subgraph_data(
        net_file=net_file,
        edge_ids=edge_ids,
        hops=hops,
        max_edges=max_edges,
    )

@mcp.tool()
def traffic_report(
    tripinfo_xml: str,
    net_file: Optional[str] = None,
    edgedata_xml: Optional[str] = None,
    top_n: int = 5,
    speed_unit: str = "km/h",
    emission_unit: str = "kg",
) -> Dict[str, Any]:
    """
    Generate a concise natural-language traffic state report from simulation outputs.

    This is intentionally deterministic and evidence-based: it summarizes the
    numeric tools instead of asking an LLM to invent an interpretation.
    """
    speed = _tripinfo_mean_speed_data(tripinfo_xml, unit=speed_unit)
    travel = _tripinfo_travel_time_summary_data(tripinfo_xml, top_n=top_n)
    emissions = _emissions_total_data(tripinfo_xml, unit=emission_unit)

    diagnosis: Optional[Dict[str, Any]] = None
    if net_file and edgedata_xml and os.path.isfile(net_file) and os.path.isfile(edgedata_xml):
        diagnosis = _network_diagnosis_data(
            edgedata_xml=edgedata_xml,
            net_file=net_file,
            tripinfo_xml=tripinfo_xml,
            top_n=top_n,
        )

    delay = diagnosis.get("delay", {}) if diagnosis else _trip_delay_summary(tripinfo_xml)
    diag_summary = diagnosis.get("summary", {}) if diagnosis else {}
    bottlenecks = diagnosis.get("bottlenecks", []) if diagnosis else []
    critical_roads = diagnosis.get("critical_roads", []) if diagnosis else []

    mean_speed = float(speed.get("value") or 0.0)
    avg_tt = float(travel.get("avg_travel_time_s") or 0.0)
    avg_wait = float(delay.get("avg_waiting_time_s") or 0.0)
    avg_loss = float(delay.get("avg_time_loss_s") or 0.0)
    trip_count = int(travel.get("count") or speed.get("count") or 0)
    congested_count = int(diag_summary.get("congested_edge_count") or 0)

    if mean_speed >= 40 and avg_loss < 30:
        state_label = "generally stable"
    elif mean_speed >= 25 and avg_loss < 90:
        state_label = "moderately congested"
    else:
        state_label = "heavily congested"

    top_bottleneck_names = []
    for edge in bottlenecks[:top_n]:
        name = edge.get("name") or edge.get("edge_id")
        if name and name not in top_bottleneck_names:
            top_bottleneck_names.append(str(name))

    top_critical_names = []
    for edge in critical_roads[:top_n]:
        name = edge.get("name") or edge.get("edge_id")
        if name and name not in top_critical_names:
            top_critical_names.append(str(name))

    co2 = (emissions.get("totals") or {}).get("CO2_abs", 0.0)

    lines = [
        "Traffic Simulation Report",
        "",
        f"The simulation completed with {trip_count} recorded trips. Overall, the traffic state is {state_label}.",
        (
            f"Average speed was {mean_speed:.2f} {speed_unit}, average travel time was "
            f"{avg_tt:.2f} s, average waiting time was {avg_wait:.2f} s, and average time loss "
            f"was {avg_loss:.2f} s."
        ),
        f"Estimated total CO2 emissions were {float(co2):.2f} {emission_unit}.",
    ]

    if diagnosis:
        lines.append(
            f"At the road-segment level, {congested_count} edges were flagged as congested."
        )
        if top_bottleneck_names:
            lines.append("Main bottleneck candidates: " + ", ".join(top_bottleneck_names[:top_n]) + ".")
        if top_critical_names:
            lines.append("Most critical roads by network impact: " + ", ".join(top_critical_names[:top_n]) + ".")
    else:
        lines.append(
            "Road-level bottleneck diagnosis was not available because net_file or edgedata_xml was missing."
        )

    lines.append(
        "Recommended next step: compare this run against an intervention scenario, such as signal timing adjustment, "
        "route guidance, or demand reduction on the identified bottleneck corridors."
    )

    return {
        "report": "\n".join(lines),
        "state_label": state_label,
        "evidence": {
            "speed": speed,
            "travel_time": travel,
            "delay": delay,
            "emissions": emissions,
            "diagnosis_summary": diag_summary,
            "top_bottlenecks": bottlenecks[:top_n],
            "critical_roads": critical_roads[:top_n],
        },
        "sources": {
            "tripinfo_xml": os.path.abspath(tripinfo_xml),
            "net_file": os.path.abspath(net_file) if net_file else None,
            "edgedata_xml": os.path.abspath(edgedata_xml) if edgedata_xml else None,
        },
    }


# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    try:
        mcp.run()       # FastMCP 2.x：默认 STDIO
    except AttributeError:
        mcp.run_stdio() # 兼容老版
