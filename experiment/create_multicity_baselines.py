#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from providers.Modification.remove_edge import route_demand_units  # noqa: E402


def _write_xml(path: Path, root: ET.Element) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _write_sumo_files(city_dir: Path) -> Path:
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
    report = ET.SubElement(configuration, "report")
    ET.SubElement(report, "tripinfo-output", {"value": "tripinfo.xml"})
    ET.SubElement(report, "device.emissions.probability", {"value": "1.0"})
    cfg = city_dir / "sim.sumocfg"
    _write_xml(cfg, configuration)
    return cfg


def _network_stats(net_path: Path) -> Dict[str, int]:
    root = ET.parse(net_path).getroot()
    return {
        "edge_count": sum(
            1 for edge in root.findall("edge") if not str(edge.get("id") or "").startswith(":")
        ),
        "junction_count": len(root.findall("junction")),
        "tls_count": len(root.findall("tlLogic")),
    }


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _tripinfo_stats(tripinfo_path: Path, source_demand: float) -> Dict[str, Any]:
    trips = ET.parse(tripinfo_path).getroot().findall("tripinfo")
    durations = [float(trip.get("duration") or 0.0) for trip in trips]
    route_lengths = [float(trip.get("routeLength") or 0.0) for trip in trips]
    completed = len(trips)
    co2 = 0.0
    for trip in trips:
        emissions = trip.find("emissions")
        if emissions is not None:
            co2 += float(emissions.get("CO2_abs") or emissions.get("CO2") or 0.0)
    return {
        "source_demand_units": source_demand,
        "completed_trips": completed,
        "completion_rate": completed / source_demand if source_demand else 0.0,
        "mean_delay_s": _mean(float(trip.get("timeLoss") or 0.0) for trip in trips),
        "mean_duration_s": _mean(durations),
        "mean_waiting_s": _mean(float(trip.get("waitingTime") or 0.0) for trip in trips),
        "mean_route_length_m": _mean(route_lengths),
        "network_mean_speed_mps": (
            sum(route_lengths) / sum(durations) if sum(durations) > 0 else 0.0
        ),
        "co2_total_mg": co2,
    }


def _stage_city(
    spec: Dict[str, Any],
    output_root: Path,
    run_sumo: bool,
    sumo_binary: str,
    sumo_version: str,
) -> Dict[str, Any]:
    city = str(spec["city"])
    key = str(spec["key"])
    source_net = (PROJECT_ROOT / str(spec["net"])).resolve()
    source_routes = (PROJECT_ROOT / str(spec["routes"])).resolve()
    if not source_net.is_file():
        raise FileNotFoundError(f"Missing source network for {city}: {source_net}")
    if not source_routes.is_file():
        raise FileNotFoundError(f"Missing source demand for {city}: {source_routes}")

    city_dir = (output_root / key).resolve()
    city_dir.mkdir(parents=True, exist_ok=True)
    net_out = city_dir / "network.net.xml"
    routes_out = city_dir / "demand.rou.xml"
    shutil.copy2(source_net, net_out)
    shutil.copy2(source_routes, routes_out)
    cfg = _write_sumo_files(city_dir)
    source_demand = route_demand_units(str(routes_out))

    record: Dict[str, Any] = {
        "city": city,
        "key": key,
        "status": "staged",
        "source": {
            "net": str(source_net),
            "routes": str(source_routes),
        },
        "artifacts": {
            "cfg": str(cfg),
            "net": str(net_out),
            "routes": str(routes_out),
            "tripinfo": str(city_dir / "tripinfo.xml"),
            "edgedata": str(city_dir / "edgedata.xml"),
            "sumo_log": str(city_dir / "sumo.run.log"),
        },
        "network": _network_stats(net_out),
        "source_demand_units": source_demand,
        "sumo": {"binary": sumo_binary, "version": sumo_version},
    }
    if not run_sumo:
        return record

    with (city_dir / "sumo.run.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [sumo_binary, "-c", cfg.name, "--start", "--quit-on-end"],
            cwd=city_dir,
            stdout=log,
            stderr=log,
            check=False,
        )
    if completed.returncode != 0 or not (city_dir / "tripinfo.xml").is_file():
        record.update(
            {
                "status": "error",
                "error": f"SUMO exited with code {completed.returncode}; see sumo.run.log",
            }
        )
        return record
    record["status"] = "ok"
    record["baseline_kpi"] = _tripinfo_stats(city_dir / "tripinfo.xml", source_demand)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage and run reproducible baseline SUMO scenarios for multiple cities."
    )
    parser.add_argument(
        "--sources",
        default=str(PROJECT_ROOT / "experiment" / "multicity_sources.json"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "experiment" / "multicity-baselines"),
    )
    parser.add_argument("--skip-sumo", action="store_true")
    parser.add_argument(
        "--sumo-binary",
        default="/usr/bin/sumo" if Path("/usr/bin/sumo").is_file() else "sumo",
        help="SUMO executable; defaults to /usr/bin/sumo to match the optimizer environment.",
    )
    args = parser.parse_args()

    sources_path = Path(args.sources).resolve()
    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    specs = json.loads(sources_path.read_text(encoding="utf-8"))
    sumo_binary = str(Path(args.sumo_binary).resolve()) if Path(args.sumo_binary).exists() else args.sumo_binary
    version_run = subprocess.run(
        [sumo_binary, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    sumo_version = version_run.stdout.splitlines()[0].strip()
    print(f"SUMO: {sumo_version} ({sumo_binary})", flush=True)
    records: List[Dict[str, Any]] = []
    for spec in specs:
        print(f"[baseline] {spec['city']}", flush=True)
        try:
            record = _stage_city(
                spec,
                output_root,
                not args.skip_sumo,
                sumo_binary,
                sumo_version,
            )
        except Exception as exc:
            record = {
                "city": spec.get("city"),
                "key": spec.get("key"),
                "status": "error",
                "error": str(exc),
            }
        records.append(record)
        print(f"           status={record['status']}", flush=True)

    (output_root / "baseline_manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cases = []
    for record in records:
        if record.get("status") not in {"ok", "staged"}:
            continue
        cfg = Path(record["artifacts"]["cfg"])
        cases.append(
            {
                "id": f"{record['key']}_delay",
                "cfg": str(cfg.relative_to(PROJECT_ROOT)),
                "goal": (
                    f"reduce traffic delay in {record['city']} using compatible interventions "
                    "while preserving all vehicles and OD pairs"
                ),
                "profile": "delay",
            }
        )
    with (output_root / "optimization_cases.jsonl").open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    failures = sum(1 for record in records if record.get("status") == "error")
    print(f"Manifest: {output_root / 'baseline_manifest.json'}")
    print(f"Optimization cases: {output_root / 'optimization_cases.jsonl'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
