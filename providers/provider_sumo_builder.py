# providers/builder_fastmcp.py
import sys, logging
import time
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

import os, subprocess, re
import numpy as np
from geopy.distance import geodesic
import requests

from typing import Dict, Any, Optional, Sequence
from NetGenerate.net_generate import NetGenerate

from fastmcp import FastMCP
from exceptiongroup import ExceptionGroup

mcp = FastMCP("sumo-builder")

OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://api.openstreetmap.fr/oapi/interpreter",
]

def _open_log(out_dir: str, name: str):
    os.makedirs(out_dir, exist_ok=True)
    return open(os.path.join(out_dir, name), "w", encoding="utf-8")


def _ok(message: str = "", artifacts: Dict[str, Any] | None = None, **extra) -> Dict[str, Any]:
    artifacts = artifacts or {}
    return {
        "status": "ok",
        "success": True,
        "message": message,
        "artifacts": artifacts,
        **extra,
    }


def _err(error_type: str, message: str, **extra) -> Dict[str, Any]:
    return {
        "status": "error",
        "success": False,
        "error_type": error_type,
        "message": message,
        "error": message,
        **extra,
    }


def _safe_city_dir(city_name: str) -> str:
    s = city_name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "city"


def compute_bbox_geodesic(lat, lon, distance_miles):
    radius_m = float(distance_miles) * 1609.344
    center = (lat, lon)

    north = geodesic(meters=radius_m).destination(center, 0)
    south = geodesic(meters=radius_m).destination(center, 180)
    east  = geodesic(meters=radius_m).destination(center, 90)
    west  = geodesic(meters=radius_m).destination(center, 270)

    bbox = np.array([
        west.longitude,
        south.latitude,
        east.longitude,
        north.latitude,
    ])

    return np.round(bbox, 6)

def download_osm_by_bbox(
    bbox: Sequence[float],
    out_dir: str,
    prefix: str = "osm",
    timeout_s: int = 180,
    max_retries: int = 2,
) -> str:
    """
    Download OSM XML directly from Overpass instead of SUMO's osmGet.py.

    bbox order is [west, south, east, north].
    """
    west, south, east, north = map(float, bbox)
    os.makedirs(out_dir, exist_ok=True)
    osm_path = os.path.join(out_dir, f"{prefix}.osm.xml")

    query = f"""
[out:xml][timeout:{timeout_s}];
(
  way["highway"]({south},{west},{north},{east});
  >;
);
out body;
"""

    last_err: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(max_retries):
            try:
                logging.info(
                    "Overpass request started: endpoint=%s attempt=%s/%s bbox=%s timeout=%ss",
                    endpoint,
                    attempt + 1,
                    max_retries,
                    [west, south, east, north],
                    timeout_s + 30,
                )
                resp = requests.post(
                    endpoint,
                    data=query.encode("utf-8"),
                    headers={
                        "User-Agent": "ChatSUMO/1.0",
                        "Content-Type": "text/plain; charset=utf-8",
                        "Accept-Encoding": "gzip",
                    },
                    timeout=timeout_s + 30,
                )
                resp.raise_for_status()

                content = resp.content
                if not content.strip():
                    raise RuntimeError("Empty OSM response")

                with open(osm_path, "wb") as f:
                    f.write(content)

                logging.info(
                    "Overpass request succeeded: endpoint=%s bytes=%s saved_to=%s",
                    endpoint,
                    len(content),
                    osm_path,
                )
                return osm_path
            except Exception as e:
                last_err = e
                logging.warning(
                    "Overpass request failed: endpoint=%s attempt=%s/%s error=%s",
                    endpoint,
                    attempt + 1,
                    max_retries,
                    e,
                )
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"OSM download failed for bbox={bbox}: {last_err}")

@mcp.tool()
def build_from_realworld(
    city_name: str,
    volume: int,
    distance_miles: float,
    out_dir: Optional[str] = None
) -> Dict[str, Any]:
    """
    Build a real-world SUMO network from OSM around a city.
    - city_name: e.g. "Ann Arbor, MI"
    - volume: desired demand scale, kept for downstream compatibility
    - distance_miles: half-size of square bbox (miles) around city center
    - out_dir: optional custom folder (defaults to ./runs/<city>)
    Returns: dict with bbox and generated artifacts.
    """
    city_key = _safe_city_dir(city_name)
    out_dir = out_dir or os.path.abspath(os.path.join("runs", city_key))
    os.makedirs(out_dir, exist_ok=True)

    try:
        generator = NetGenerate(city_key, volume)
        logging.info(
            "Starting real-world build: city=%s city_key=%s volume=%s distance_miles=%s out_dir=%s",
            city_name,
            city_key,
            volume,
            distance_miles,
            out_dir,
        )

        lat, lon = generator.get_lat_lon(city_name)
        bbox = compute_bbox_geodesic(lat, lon, distance_miles)
        logging.info("Computed bbox for %s: %s", city_name, bbox.tolist())

        osm_path = download_osm_by_bbox(bbox, out_dir=out_dir, prefix=city_key)
        net_path = generator.convert_osm_to_sumo(osm_path=osm_path, out_dir=out_dir)
        logging.info("SUMO network conversion succeeded: net=%s", net_path)

        return _ok(
            message=f"Built real-world SUMO network '{net_path}'.",
            artifacts={
                "osm": osm_path,
                "net": net_path,
                "output_dir": out_dir,
            },
            city=city_name,
            city_key=city_key,
            center={"lat": lat, "lon": lon},
            bbox={
                "west": float(bbox[0]),
                "south": float(bbox[1]),
                "east": float(bbox[2]),
                "north": float(bbox[3]),
            },
            net=net_path,
            output_dir=out_dir,
            logs={
                "netconvert": os.path.join(out_dir, f"{city_key}_netconvert.log"),
            },
        )
    except Exception as e:
        logging.exception("Real-world build failed")
        return _err(
            "BUILD_FROM_REALWORLD_FAILED",
            str(e),
            city=city_name,
            city_key=city_key,
            output_dir=out_dir,
            hint=(
                "OSM download depends on external Overpass endpoints and DNS/network access. "
                "If this is a DNS/proxy issue, verify network access or provide a local OSM file/cache."
            ),
        )

@mcp.tool()
def build_net_from_roundabout(radius: int = 35, lanes: int = 1, out_dir: str = "./runs/tmp") -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    net = os.path.join(out_dir, "net.net.xml")
    cmd = ["netgenerate", "--roundabout", "--radius", str(radius), "--lanes", str(lanes),
           "--default-junction-type", "priority", "-o", net]
    with _open_log(out_dir, "netgenerate.log") as logf:
        subprocess.run(cmd, check=True, stdout=logf, stderr=logf)
    net_abs = os.path.abspath(net)
    return _ok(
        message=f"Built roundabout network '{net_abs}'.",
        artifacts={"net": net_abs},
        net=net_abs,
        log=os.path.abspath(os.path.join(out_dir, "netgenerate.log")),
    )

@mcp.tool()
def write_sumocfg(net: str, routes: str, options: Dict[str, Any] | None = None) -> Dict[str, Any]:
    options = options or {}

    net_abs = net if os.path.isabs(net) else os.path.abspath(net)
    rou_abs = routes if os.path.isabs(routes) else os.path.abspath(routes)

    cfg_dir = os.path.dirname(net_abs)
    os.makedirs(cfg_dir, exist_ok=True)

    tripinfo = options.get("tripinfo_output", os.path.join(cfg_dir, "tripinfo.xml"))
    tripinfo_abs = tripinfo if os.path.isabs(tripinfo) else os.path.abspath(tripinfo)
    
    edge_data_file = options.get("edge_data_file", os.path.join(cfg_dir, "edgedata.xml"))
    edge_data_abs = edge_data_file if os.path.isabs(edge_data_file) else os.path.abspath(edge_data_file)
    edge_additional = options.get("edge_additional_file", os.path.join(cfg_dir, "edge_data.add.xml"))
    edge_additional_abs = (
        edge_additional if os.path.isabs(edge_additional) else os.path.abspath(edge_additional)
    )
    edge_data_period = int(options.get("edge_data_period", 60))

    cfg = os.path.join(cfg_dir, "sim.sumocfg")

    with open(edge_additional_abs, "w", encoding="utf-8") as f:
        f.write(
            f"""<additional>
    <edgeData id="dump_edge_speed" period="{edge_data_period}" file="{edge_data_abs}" excludeEmpty="true"/>
</additional>"""
        )
    
    with open(cfg, "w") as f:
        f.write(f'''<configuration>
                        <input>
                            <net-file value="{net_abs}"/>
                            <route-files value="{rou_abs}"/>
                            <additional-files value="{edge_additional_abs}"/>
                        </input>
                        <report>
                            <tripinfo-output value="{tripinfo_abs}"/>
                            <device.emissions.probability value="1.0"/>
                        </report>

                    </configuration>''')

    cfg_abs = os.path.abspath(cfg)
    return _ok(
        message=f"Wrote SUMO config '{cfg_abs}'.",
        artifacts={
            "net": net_abs,
            "routes": rou_abs,
            "cfg": cfg_abs,
            "tripinfo": tripinfo_abs,
            "edge_data": edge_data_abs,
            "additional": edge_additional_abs,
        },
        net=net_abs,
        routes=rou_abs,
        route_path=rou_abs,
        cfg=cfg_abs,
        tripinfo=tripinfo_abs,
        edge_data=edge_data_abs,
        additional=edge_additional_abs,
    )

def _flatten_exceptiongroup(e: BaseException) -> list[BaseException]:
    leaves: list[BaseException] = []

    def collect(ex: BaseException):
        if isinstance(ex, ExceptionGroup):
            for sub in ex.exceptions:
                collect(sub)
        else:
            leaves.append(ex)

    collect(e)
    return leaves


if __name__ == "__main__":
    try:
        mcp.run()
    except ExceptionGroup as eg:
        leaves = _flatten_exceptiongroup(eg)
        if leaves and all(isinstance(x, BrokenPipeError) for x in leaves):
            logging.warning("Client disconnected (BrokenPipe). Exiting cleanly.")
            sys.exit(0)
        raise
    except BrokenPipeError:
        logging.warning("Client disconnected (BrokenPipe). Exiting cleanly.")
        sys.exit(0)
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting cleanly.")
        sys.exit(0)
