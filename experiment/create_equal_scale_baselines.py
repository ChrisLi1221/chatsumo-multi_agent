#!/usr/bin/env python3
"""Create controlled, reproducible SUMO baselines for the five medium networks."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NETWORK_MANIFEST = PROJECT_ROOT / "runs" / "equal_scale_medium" / "network_manifest.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "experiment" / "equal-scale-baselines"
DEFAULT_RANDOM_TRIPS = Path("/usr/share/sumo/tools/randomTrips.py")

CITY_NAMES = {
    "albany": "Albany",
    "troy": "Troy",
    "ann_arbor": "Ann Arbor",
    "los_angeles": "Los Angeles",
    "manhattan": "Manhattan",
}


def _write_xml(path: Path, root: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _write_sumo_files(city_dir: Path, seed: int) -> Path:
    additional = ET.Element("additional")
    ET.SubElement(
        additional,
        "edgeData",
        {
            "id": "edge_performance",
            "period": "60",
            "file": "edgedata.xml",
            "excludeEmpty": "true",
        },
    )
    _write_xml(city_dir / "edge_data.add.xml", additional)

    configuration = ET.Element("configuration")
    inputs = ET.SubElement(configuration, "input")
    ET.SubElement(inputs, "net-file", {"value": "network.net.xml"})
    ET.SubElement(inputs, "route-files", {"value": "demand.rou.xml"})
    ET.SubElement(inputs, "additional-files", {"value": "edge_data.add.xml"})

    random_number = ET.SubElement(configuration, "random_number")
    ET.SubElement(random_number, "seed", {"value": str(seed)})

    report = ET.SubElement(configuration, "report")
    ET.SubElement(report, "tripinfo-output", {"value": "tripinfo.xml"})
    ET.SubElement(report, "device.emissions.probability", {"value": "1.0"})
    ET.SubElement(report, "duration-log.statistics", {"value": "true"})
    ET.SubElement(report, "no-step-log", {"value": "true"})

    cfg = city_dir / "sim.sumocfg"
    _write_xml(cfg, configuration)
    return cfg


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _tripinfo_stats(tripinfo_path: Path, expected_trips: int, log_path: Path) -> Dict[str, Any]:
    trips = ET.parse(tripinfo_path).getroot().findall("tripinfo")
    durations = [float(trip.get("duration") or 0.0) for trip in trips]
    route_lengths = [float(trip.get("routeLength") or 0.0) for trip in trips]
    completed = len(trips)
    co2_mg = 0.0
    for trip in trips:
        emissions = trip.find("emissions")
        if emissions is not None:
            co2_mg += float(emissions.get("CO2_abs") or emissions.get("CO2") or 0.0)

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    teleport_count = len(re.findall(r"Teleporting vehicle", log_text))
    return {
        "expected_trips": expected_trips,
        "completed_trips": completed,
        "completion_rate": completed / expected_trips if expected_trips else 0.0,
        "teleport_count": teleport_count,
        "mean_delay_s": _mean(float(trip.get("timeLoss") or 0.0) for trip in trips),
        "mean_duration_s": _mean(durations),
        "mean_waiting_s": _mean(float(trip.get("waitingTime") or 0.0) for trip in trips),
        "mean_depart_delay_s": _mean(float(trip.get("departDelay") or 0.0) for trip in trips),
        "mean_route_length_m": _mean(route_lengths),
        "distance_weighted_speed_mps": (
            sum(route_lengths) / sum(durations) if sum(durations) > 0 else 0.0
        ),
        "co2_total_mg": co2_mg,
        "last_arrival_s": max((float(trip.get("arrival") or 0.0) for trip in trips), default=0.0),
    }


def _generate_demand(
    net_path: Path,
    demand_path: Path,
    random_trips: Path,
    trip_count: int,
    begin: float,
    end: float,
    seed: int,
    key: str,
    log_path: Path,
) -> Dict[str, Any]:
    if trip_count <= 0:
        raise ValueError("trip_count must be positive")
    if end <= begin:
        raise ValueError("end must be greater than begin")
    period = (end - begin) / trip_count
    # randomTrips repeatedly adds a floating-point period and may otherwise emit
    # one extra trip at the end boundary (for example, 1001 trips at period 3.6).
    # A half-period-open cutoff preserves the intended departures 0..N-1 exactly.
    generation_end = end - period / 2.0
    command = [
        "python",
        str(random_trips),
        "--net-file",
        str(net_path),
        "--output-trip-file",
        str(demand_path),
        "--begin",
        str(begin),
        "--end",
        f"{generation_end:.12g}",
        "--period",
        f"{period:.12g}",
        "--seed",
        str(seed),
        "--prefix",
        f"{key}_",
        "--vehicle-class",
        "passenger",
        "--edge-permission",
        "passenger",
        "--length",
        "--lanes",
        "--validate",
        "--threads",
        "4",
    ]
    with log_path.open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, stdout=log, stderr=log, check=False)
    if completed.returncode != 0 or not demand_path.is_file():
        raise RuntimeError(
            f"randomTrips.py exited with code {completed.returncode}; see {log_path}"
        )

    root = ET.parse(demand_path).getroot()
    trips = root.findall("trip")
    if len(trips) != trip_count:
        raise RuntimeError(f"Expected {trip_count} validated trips, found {len(trips)}")
    invalid = [trip.get("id") for trip in trips if not trip.get("from") or not trip.get("to")]
    if invalid:
        raise RuntimeError(f"Trips without explicit OD endpoints: {invalid[:5]}")
    return {
        "representation": "adaptive_od_trips",
        "trip_count": len(trips),
        "begin_s": begin,
        "end_s": end,
        "period_s": period,
        "seed": seed,
        "vehicle_class": "passenger",
        "endpoint_weighting": "edge_length_x_lane_count",
        "connectivity_validated": True,
    }


def _stage_city(
    network_record: Dict[str, Any],
    output_root: Path,
    random_trips: Path,
    trip_count: int,
    begin: float,
    end: float,
    seed: int,
    sumo_binary: str,
    sumo_version: str,
    run_sumo: bool,
) -> Dict[str, Any]:
    spec = network_record.get("spec", {})
    key = str(spec.get("key") or Path(str(network_record["net"])).stem.split(".")[0])
    city = CITY_NAMES.get(key, key.replace("_", " ").title())
    source_net = Path(str(network_record["net"])).resolve()
    if network_record.get("status") != "ok" or not source_net.is_file():
        raise FileNotFoundError(f"Usable network not found for {city}: {source_net}")

    city_dir = (output_root / key).resolve()
    city_dir.mkdir(parents=True, exist_ok=True)
    net_out = city_dir / "network.net.xml"
    demand_out = city_dir / "demand.rou.xml"
    demand_log = city_dir / "random_trips.log"
    sumo_log = city_dir / "sumo.run.log"
    shutil.copy2(source_net, net_out)
    demand = _generate_demand(
        net_out,
        demand_out,
        random_trips,
        trip_count,
        begin,
        end,
        seed,
        key,
        demand_log,
    )
    cfg = _write_sumo_files(city_dir, seed)

    record: Dict[str, Any] = {
        "city": city,
        "key": key,
        "status": "staged",
        "source_net": str(source_net),
        "artifacts": {
            "cfg": str(cfg),
            "net": str(net_out),
            "routes": str(demand_out),
            "tripinfo": str(city_dir / "tripinfo.xml"),
            "edgedata": str(city_dir / "edgedata.xml"),
            "demand_log": str(demand_log),
            "sumo_log": str(sumo_log),
        },
        "network": network_record.get("network", {}),
        "demand": demand,
        "sumo": {"binary": sumo_binary, "version": sumo_version},
    }
    if not run_sumo:
        return record

    with sumo_log.open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [sumo_binary, "-c", cfg.name, "--start", "--quit-on-end"],
            cwd=city_dir,
            stdout=log,
            stderr=log,
            check=False,
        )
    tripinfo = city_dir / "tripinfo.xml"
    if completed.returncode != 0 or not tripinfo.is_file():
        record.update(
            {
                "status": "error",
                "error": f"SUMO exited with code {completed.returncode}; see {sumo_log}",
            }
        )
        return record

    kpi = _tripinfo_stats(tripinfo, trip_count, sumo_log)
    if kpi["completed_trips"] != trip_count:
        record.update(
            {
                "status": "error",
                "baseline_kpi": kpi,
                "error": f"Only {kpi['completed_trips']} of {trip_count} trips completed",
            }
        )
        return record
    if kpi["teleport_count"] != 0:
        record.update(
            {
                "status": "error",
                "baseline_kpi": kpi,
                "error": f"Detected {kpi['teleport_count']} vehicle teleports",
            }
        )
        return record
    record["status"] = "ok"
    record["baseline_kpi"] = kpi
    return record


def _parse_seed_list(value: str) -> List[int]:
    seeds: List[int] = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            seed = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid seed: {raw}") from exc
        if seed not in seeds:
            seeds.append(seed)
    if not seeds:
        raise argparse.ArgumentTypeError("provide at least one integer seed")
    return seeds


def _optimization_cases(
    records: Iterable[Dict[str, Any]],
    *,
    seed_suffix: int | None = None,
) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    for record in records:
        if record.get("status") not in {"ok", "staged"}:
            continue
        cfg = Path(record["artifacts"]["cfg"])
        try:
            cfg_reference = str(cfg.relative_to(PROJECT_ROOT))
        except ValueError:
            cfg_reference = str(cfg)
        case_id = f"{record['key']}_equal_scale_delay"
        if seed_suffix is not None:
            case_id += f"_seed_{seed_suffix}"
        cases.append(
            {
                "id": case_id,
                "cfg": cfg_reference,
                "goal": (
                    f"reduce traffic delay in {record['city']} using compatible interventions "
                    "while preserving all vehicles and OD pairs"
                ),
                "profile": "delay",
                "seed": seed_suffix if seed_suffix is not None else record.get("experiment_seed"),
            }
        )
    return cases


def _write_cases(path: Path, cases: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate equal-scale, equal-demand baselines for five SUMO city networks."
    )
    parser.add_argument("--network-manifest", default=str(DEFAULT_NETWORK_MANIFEST))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--trips", type=int, default=1200)
    parser.add_argument("--begin", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=3600.0)
    seed_group = parser.add_mutually_exclusive_group()
    seed_group.add_argument("--seed", type=int, default=42)
    seed_group.add_argument(
        "--seeds",
        type=_parse_seed_list,
        help=(
            "Comma-separated demand/SUMO seeds, e.g. 42,43,44. Each seed is "
            "written to output-dir/seed_<seed>, with a combined root manifest."
        ),
    )
    parser.add_argument("--random-trips", default=str(DEFAULT_RANDOM_TRIPS))
    parser.add_argument("--skip-sumo", action="store_true")
    parser.add_argument(
        "--sumo-binary",
        default="/usr/bin/sumo" if Path("/usr/bin/sumo").is_file() else "sumo",
    )
    args = parser.parse_args()

    manifest_path = Path(args.network_manifest).resolve()
    output_root = Path(args.output_dir).resolve()
    random_trips = Path(args.random_trips).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    if not random_trips.is_file():
        raise FileNotFoundError(f"randomTrips.py not found: {random_trips}")

    network_records = json.loads(manifest_path.read_text(encoding="utf-8"))
    sumo_binary = (
        str(Path(args.sumo_binary).resolve())
        if Path(args.sumo_binary).exists()
        else args.sumo_binary
    )
    version_run = subprocess.run(
        [sumo_binary, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    sumo_version = version_run.stdout.splitlines()[0].strip()
    print(f"SUMO: {sumo_version} ({sumo_binary})", flush=True)

    seeds = list(args.seeds or [args.seed])
    seeded_batch = args.seeds is not None
    all_records: List[Dict[str, Any]] = []
    all_cases: List[Dict[str, Any]] = []

    for seed_index, seed in enumerate(seeds, start=1):
        seed_root = output_root / f"seed_{seed}" if seeded_batch else output_root
        seed_root.mkdir(parents=True, exist_ok=True)
        if seeded_batch:
            print(f"\nSeed {seed} [{seed_index}/{len(seeds)}]", flush=True)

        records: List[Dict[str, Any]] = []
        for index, network_record in enumerate(network_records, start=1):
            key = str(network_record.get("spec", {}).get("key") or "unknown")
            print(f"[{index}/{len(network_records)}] {CITY_NAMES.get(key, key)}", flush=True)
            try:
                record = _stage_city(
                    network_record,
                    seed_root,
                    random_trips,
                    args.trips,
                    args.begin,
                    args.end,
                    seed,
                    sumo_binary,
                    sumo_version,
                    not args.skip_sumo,
                )
            except Exception as exc:
                record = {
                    "city": CITY_NAMES.get(key, key),
                    "key": key,
                    "status": "error",
                    "error": str(exc),
                }
            record["experiment_seed"] = seed
            records.append(record)
            if record.get("baseline_kpi"):
                kpi = record["baseline_kpi"]
                print(
                    "    "
                    f"status={record['status']} completed={kpi['completed_trips']}/{args.trips} "
                    f"delay={kpi['mean_delay_s']:.2f}s teleports={kpi['teleport_count']}",
                    flush=True,
                )
            else:
                print(f"    status={record['status']}", flush=True)

            (seed_root / "baseline_manifest.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        cases = _optimization_cases(
            records,
            seed_suffix=seed if seeded_batch else None,
        )
        _write_cases(seed_root / "optimization_cases.jsonl", cases)
        all_records.extend(records)
        all_cases.extend(cases)

    if seeded_batch:
        (output_root / "baseline_manifest.json").write_text(
            json.dumps(all_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_cases(output_root / "optimization_cases.jsonl", all_cases)

    cases_path = output_root / "optimization_cases.jsonl"
    failures = sum(1 for record in all_records if record.get("status") == "error")
    print(f"Manifest: {output_root / 'baseline_manifest.json'}", flush=True)
    print(f"Optimization cases: {cases_path}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
