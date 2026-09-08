import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Union

from geopy.geocoders import Nominatim

logging.basicConfig(stream=sys.stderr, level=logging.INFO)


class NetGenerate:
    def __init__(self, net_name, volume):
        self.net_name = net_name
        self.volume = volume

    def _open_log(self, out_dir: str, name: str):
        os.makedirs(out_dir, exist_ok=True)
        return open(os.path.join(out_dir, name), "w", encoding="utf-8")

    def get_lat_lon(self, city_name):
        geolocator = Nominatim(user_agent="sumotest")
        # geolocator = Photon(user_agent="sumotest")
        location = geolocator.geocode(city_name)
        if location:
            return location.latitude, location.longitude
        else:
            raise ValueError(f"Could not find coordinates for {city_name}.")
        
    def download_osm_data(self, bbox, out_dir):
        try:
            bbox_str = f" {bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
            cmd = [
                "python",
                "/usr/share/sumo/tools/osmGet.py",
                "--bbox", bbox_str,
                "-p", self.net_name,
                "-d", out_dir,
            ]
            
            with self._open_log(out_dir, "netgenerate.log") as logf:
                logging.info(f"Downloading OSM data with command: {' '.join(cmd)}")
                subprocess.run(cmd, check=True, stdout=logf, stderr=logf, text=True)
        except Exception as e:
            logging.error(f"Error downloading OSM data: {e}")
            raise

    def convert_osm_to_sumo(self, out_dir, osm_path=None):
        osm_path = osm_path or os.path.join(out_dir, self.net_name + "_bbox.osm.xml")
        net_path = os.path.join(out_dir, self.net_name + ".net.xml")
        convert = [
            "netconvert",
            "--osm-files",
            osm_path,
            "-o",
            net_path,
            "--output.street-names",
            "true",
            "--no-warnings",
        ]
        with self._open_log(out_dir, self.net_name + "_netconvert.log") as logf:
            subprocess.run(convert, check=True, stdout=logf, stderr=logf)
        return net_path

    def generate_vehicle_types(self, car_probability, ev_probability):
        vtypes = f"""<additional xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/additional_file.xsd">
                        <vTypeDistribution id="typeDist">
                            <vType id="car" vClass="passenger" accel="2.4" decel="4.5" sigma="0.5" length="5" minGap="2.5" maxSpeed="70" probability="{str(car_probability)}"/>
                            <vType id="evehicle" vClass="evehicle" emissionClass="Energy" accel="2.6" decel="4.8" sigma="0.5" length="5" minGap="2.5" maxSpeed="70" probability="{str(ev_probability)}"/>
                        </vTypeDistribution>
                    </additional>"""

        with open("vehicleTypes.add.xml", "w") as vfile:
            vfile.write(vtypes)

    def generate_trips(self, out_dir):
        trips = (
            "python /usr/share/sumo/tools/randomTrips.py -v -n "
            + self.net_name
            + ".net.xml "
            + "--fringe-factor 9 --insertion-rate "
            + str(self.volume)
            + " -r "
            + self.net_name
            + ".rou.xml"
        )
        with self._open_log(out_dir, self.net_name + "_randomTrips.log") as logf:
            subprocess.run(trips, shell=True, check=True, stdout=logf, stderr=logf)

    def create_config(self):
        today = datetime.now()
        cfg = f"""<?xml version="1.0" encoding="UTF-8"?>

    <!-- generated on {today} by sumotest.py
    -->

    <configuration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/sumoConfiguration.xsd">

        <input>
            <net-file value="{self.net_name}.net.xml"/>
            <route-files value="{self.net_name}.rou.xml"/>
        </input>
        
        <additional-files value="edgeAdd.add.xml"/>
        <fcd-output value="fcd-output.xml"/>
        <tripinfo-output value="tripinfo-output.xml"/>
        <device.emissions.probability value="1.0"/>

    </configuration>"""

        with open(f"{self.net_name}.sumocfg", "w") as cfgfile:
            cfgfile.write(cfg)

        add_file = """<additional xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/additional_file.xsd">
        <edgeData id="dump_60" period="60" file="edgeData.xml" excludeEmpty="true"/>
    </additional>"""

        with open("edgeAdd.add.xml", "w") as addfile:
            addfile.write(add_file)


def extract_road_osm(
    osm_in: str,
    osm_out: str,
    road_name: Optional[str] = None,
    way_id: Optional[Union[int, str]] = None,
) -> int:
    """
    Create a minimal OSM file containing only one (or a few) road ways and the
    nodes they reference. Returns number of kept ways.
    - road_name: substring match on <tag k="name" v="..."> (case-insensitive)
    - way_id: exact <way id="..."> match (int or str). If provided, takes precedence.
    """
    assert os.path.exists(osm_in), f"input OSM not found: {osm_in}"
    assert road_name or way_id, "Provide road_name or way_id"

    way_id = int(way_id) if way_id is not None else None
    tree = ET.parse(osm_in)
    root = tree.getroot()  # <osm> root

    # 1) Find matching ways
    keep_way_ids = set()
    node_refs_needed = set()

    for w in root.findall("way"):
        wid = int(w.attrib.get("id"))
        if way_id is not None:
            match = wid == way_id
        else:
            # match by name tag
            name = None
            for t in w.findall("tag"):
                if t.attrib.get("k") == "name":
                    name = t.attrib.get("v", "")
                    break
            match = bool(name and road_name.lower() in name.lower())

        if match:
            keep_way_ids.add(wid)
            for nd in w.findall("nd"):
                node_refs_needed.add(nd.attrib["ref"])

    if not keep_way_ids:
        raise ValueError("No matching way found in OSM with given filter.")

    # 2) Build a new <osm> doc containing only needed nodes + ways
    out = ET.Element("osm", root.attrib)
    # copy minimal bounds if present (optional)
    bounds = root.find("bounds")
    if bounds is not None:
        out.append(bounds)

    # index nodes for quick lookup
    node_by_id = {}
    for n in root.findall("node"):
        nid = n.attrib["id"]
        if nid in node_refs_needed:
            node_by_id[nid] = n

    # append nodes
    for n in node_by_id.values():
        out.append(n)

    # append ways (only the kept ones)
    for w in root.findall("way"):
        wid = int(w.attrib.get("id"))
        if wid in keep_way_ids:
            out.append(w)

    ET.ElementTree(out).write(osm_out, encoding="utf-8", xml_declaration=True)
    return len(keep_way_ids)


def build_single_road_from_local_osm(
    osm_in: str,
    out_dir: str,
    road_name: Optional[str] = None,
    way_id: Optional[Union[int, str]] = None,
    net_name: str = "single_road",
) -> str:
    """
    Filter a local OSM to a single road and convert to SUMO .net.xml.
    Returns the path to <net_name>.net.xml
    """
    os.makedirs(out_dir, exist_ok=True)
    road_osm = os.path.join(out_dir, f"{net_name}.osm")
    kept = extract_road_osm(osm_in, road_osm, road_name=road_name, way_id=way_id)
    logging.info("Kept %d way(s) into %s", kept, road_osm)

    net_out = os.path.join(out_dir, f"{net_name}.net.xml")
    cmd = [
        "netconvert",
        "--osm-files",
        road_osm,
        "-o",
        net_out,
        "--output.street-names",
        "true",
        "--no-warnings",
    ]
    # capture a small log for debugging
    logf = open(
        os.path.join(out_dir, f"{net_name}_netconvert.log"), "w", encoding="utf-8"
    )
    try:
        subprocess.run(cmd, stdout=logf, stderr=logf, check=False)
    finally:
        logf.close()

    if not os.path.exists(net_out):
        raise RuntimeError("netconvert did not produce the .net.xml (check the log).")
    return net_out
