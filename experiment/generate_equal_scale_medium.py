from __future__ import annotations

import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "runs" / "equal_scale"
OUTPUT_ROOT = PROJECT_ROOT / "runs" / "equal_scale_medium"

# Geographic centers are fixed so rerunning this script produces the same
# subnetwork boundaries. distance_miles is the half-width/half-height of the
# retained square geographic boundary.
SPECS = [
    {"key": "albany", "center_lat": 42.6526, "center_lon": -73.7562, "distance_miles": 0.64},
    {"key": "troy", "center_lat": 42.7284, "center_lon": -73.6918, "distance_miles": 0.67},
    {"key": "ann_arbor", "center_lat": 42.2808, "center_lon": -83.7430, "distance_miles": 0.59},
    {"key": "los_angeles", "center_lat": 34.0522, "center_lon": -118.2437, "distance_miles": 0.51},
    {"key": "manhattan", "center_lat": 40.7831, "center_lon": -73.9712, "distance_miles": 0.82},
]


def source_network(city_key: str) -> Path:
    candidates = list(SOURCE_ROOT.glob(f"scn_round_*/{city_key}.net.xml"))
    if not candidates:
        raise FileNotFoundError(f"No source network found for {city_key} under {SOURCE_ROOT}")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def geographic_boundary(lat: float, lon: float, distance_miles: float) -> list[float]:
    lat_delta = distance_miles / 69.0
    lon_delta = distance_miles / (69.172 * math.cos(math.radians(lat)))
    return [lon - lon_delta, lat - lat_delta, lon + lon_delta, lat + lat_delta]


def network_stats(net_path: Path) -> Dict[str, Any]:
    root = ET.parse(net_path).getroot()
    edge_count = 0
    lane_count = 0
    road_length_m = 0.0
    lane_length_m = 0.0
    for edge in root.findall("edge"):
        if str(edge.get("id") or "").startswith(":"):
            continue
        lanes = edge.findall("lane")
        lengths = [float(lane.get("length") or 0.0) for lane in lanes]
        edge_count += 1
        lane_count += len(lanes)
        road_length_m += max(lengths, default=0.0)
        lane_length_m += sum(lengths)
    return {
        "edge_count": edge_count,
        "lane_count": lane_count,
        "junction_count": len(root.findall("junction")),
        "tls_count": len(root.findall("tlLogic")),
        "road_km": road_length_m / 1000.0,
        "lane_km": lane_length_m / 1000.0,
    }


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = []
    failures = 0

    for spec in SPECS:
        city_dir = OUTPUT_ROOT / spec["key"]
        city_dir.mkdir(parents=True, exist_ok=True)
        target = city_dir / f"{spec['key']}.net.xml"
        log_path = city_dir / "netconvert.log"
        source = source_network(spec["key"])
        boundary = geographic_boundary(
            float(spec["center_lat"]),
            float(spec["center_lon"]),
            float(spec["distance_miles"]),
        )
        command = [
            "netconvert",
            "--sumo-net-file",
            str(source),
            "--keep-edges.in-geo-boundary=" + ",".join(f"{value:.7f}" for value in boundary),
            "--keep-edges.components",
            "1",
            # Merge OSM junction fragments that are too short to hold a SUMO
            # passenger vehicle. Applying the same cleanup to all cities avoids
            # topology-induced teleport artifacts in the controlled baselines.
            "--junctions.join",
            "true",
            "--junctions.join-dist",
            "10",
            "--output-file",
            str(target),
            "--no-warnings",
            "true",
        ]
        print(
            f"Cropping {spec['key']} at distance_miles={spec['distance_miles']:.2f}",
            flush=True,
        )
        record: Dict[str, Any] = {
            "spec": spec,
            "source_net": str(source),
            "boundary": boundary,
            "preprocessing": {
                "largest_connected_component": True,
                "junction_join": True,
                "junction_join_distance_m": 10.0,
            },
            "net": str(target.resolve()),
            "netconvert_log": str(log_path.resolve()),
        }
        try:
            with log_path.open("w", encoding="utf-8") as log:
                subprocess.run(command, check=True, stdout=log, stderr=log)
            record["status"] = "ok"
            record["network"] = network_stats(target)
            print(json.dumps(record["network"], indent=2), flush=True)
        except Exception as exc:
            failures += 1
            record["status"] = "error"
            record["error"] = str(exc)
            print(f"FAILED: {exc}", flush=True)
        manifest.append(record)
        (OUTPUT_ROOT / "network_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"Manifest: {OUTPUT_ROOT / 'network_manifest.json'}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
