# providers/provider_traffic_generator.py
import os, sys, logging, subprocess
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

from typing import Dict, Any
import xml.etree.ElementTree as ET
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sumo-traffic-generator")

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


@mcp.tool()
def generate_flow_route(net_path: str, from_edge: str, to_edge: str, vph: int, route_output_path: str) -> Dict[str, Any]:
    """
    Generates a SUMO route file with a simple continuous traffic flow from one edge to another.
    
    Args:
        net_path: Path to the .net.xml file (for validation, though not used in simple flow generation).
        from_edge: The ID of the starting edge (street).
        to_edge: The ID of the ending edge (street).
        vph: Vehicles per hour (flow rate).
        route_output_path: The path where the new .rou.xml file should be saved.
    """
    
    try:
        os.makedirs(route_output_path, exist_ok=True)
        net_abs = net_path if os.path.isabs(net_path) else os.path.abspath(net_path)
        tree = ET.parse(net_abs)
        root = tree.getroot()
        from_edges = []
        to_edges = []
        for edge in root.findall("edge"):
            if edge.get("name") == from_edge or edge.get("id") == from_edge:
                from_edges.append(edge)
            if edge.get("name") == to_edge or edge.get("id") == to_edge:
                to_edges.append(edge)

        if not from_edges:
            return _err(
                "FROM_EDGE_NOT_FOUND",
                f"from_edge '{from_edge}' was not found in {net_abs}",
                net=net_abs,
            )
        if not to_edges:
            return _err(
                "TO_EDGE_NOT_FOUND",
                f"to_edge '{to_edge}' was not found in {net_abs}",
                net=net_abs,
            )

        from_edge_id = from_edges[0].get("id")
        to_edge_id = to_edges[0].get("id")
        if not from_edge_id or not to_edge_id:
            return _err(
                "EDGE_ID_MISSING",
                f"Matched edges do not have valid SUMO ids: from={from_edge_id}, to={to_edge_id}",
                net=net_abs,
            )

        trips = os.path.join(route_output_path, "trips.trips.xml")
        logging.info(f"Generating trips file at {trips}")
        
        routes = os.path.join(route_output_path, "routes.rou.xml")
        logging.info(f"Generating routes file at {routes}")

        period = max(1.0, 3600 / max(1, vph))

        trips_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
                        <routes>
                        <flow id="od_0"
                                begin="0"
                                end="3600"
                                from="{from_edge_id}"
                                to="{to_edge_id}"
                                period="{period:.2f}"/>
                        </routes>
                        """
        with open(trips, "w", encoding="utf-8") as f:
            f.write(trips_xml)

        cmd = [
            "duarouter",
            "-n", net_abs,
            "-t", trips,
            "-o", routes,
            "--ignore-errors",
            "--repair",
        ]
        logging.info(f"Running duarouter with command: {' '.join(cmd)}")
        with _open_log(route_output_path, "duarouter.log") as logf:
            subprocess.run(cmd, check=True, stdout=logf, stderr=logf)

        routes_abs = os.path.abspath(routes)
        trips_abs = os.path.abspath(trips)
        return _ok(
            message=f"Generated routes '{routes_abs}' with {vph} VPH.",
            artifacts={"net": net_abs, "routes": routes_abs, "trips": trips_abs},
            net=net_abs,
            routes=routes_abs,
            route_path=routes_abs,
            trips=trips_abs,
            flow_definition=f"Flow from {from_edge} to {to_edge} at {vph} VPH.",
            log=os.path.join(route_output_path, "duarouter.log"),
        )
    except Exception as e:
        logging.exception("generate_flow_route failed")
        return _err("GENERATE_FLOW_ROUTE_FAILED", str(e), net=net_path)

@mcp.tool()
def build_routes_duarouter(
    net: str,
    trips: str,
    out_dir: str = "./runs/tmp",
    routing_algorithm: str = "dijkstra",
) -> dict:
    """
    Convert trips/flows to executable routes using duarouter.
    """
    os.makedirs(out_dir, exist_ok=True)
    routes = os.path.join(out_dir, "routes.rou.xml")

    cmd = [
        "duarouter",
        "-n", net,
        "-t", trips,
        "-o", routes,
        "--routing-algorithm", routing_algorithm,
        "--ignore-errors",
        "--repair",
    ]

    with _open_log(out_dir, "duarouter.log") as logf:
        subprocess.run(cmd, check=True, stdout=logf, stderr=logf)

    routes_abs = os.path.abspath(routes)
    trips_abs = os.path.abspath(trips)
    net_abs = os.path.abspath(net)
    log_path = os.path.abspath(os.path.join(out_dir, "duarouter.log"))
    return _ok(
        message=f"Generated routes '{routes_abs}' from trips '{trips_abs}'.",
        artifacts={"net": net_abs, "routes": routes_abs, "trips": trips_abs},
        routes=routes_abs,
        route_path=routes_abs,
        trips=trips_abs,
        net=net_abs,
        algorithm=routing_algorithm,
        log=log_path,
    )


@mcp.tool()
def build_routes_random(net: str, flows: int, period: int, out_dir: str = "./runs/tmp") -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    rou = os.path.join(out_dir, "routes.rou.xml")
    trips = os.path.join("/usr/share/sumo/", "tools", "randomTrips.py")
    interval = 1.0 * 3600 / flows
    cmd = [sys.executable, trips, "-n", net, "-o", rou, "--period", str(interval), "--seed", "42"]
    logging.info("Generating routes with command: %s", " ".join(cmd))
    with _open_log(out_dir, "randomTrips.log") as logf:
        subprocess.run(cmd, check=True, stdout=logf, stderr=logf)
    routes_abs = os.path.abspath(rou)
    net_abs = os.path.abspath(net)
    trips_script = os.path.abspath(trips)
    log_path = os.path.abspath(os.path.join(out_dir, "randomTrips.log"))
    return _ok(
        message=f"Generated random routes '{routes_abs}'.",
        artifacts={"net": net_abs, "routes": routes_abs},
        net=net_abs,
        routes=routes_abs,
        route_path=routes_abs,
        trips_path=trips_script,
        log=log_path,
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
    <edgeData id="edge_performance" period="{edge_data_period}" file="{edge_data_abs}" excludeEmpty="true"/>
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

if __name__ == "__main__":
    mcp.run()
