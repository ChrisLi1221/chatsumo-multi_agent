import xml.etree.ElementTree as ET

import pytest

from agents.capabilities import validate_modifier_capability
from providers.Modification.edge_speed import (
    corridor_speed_inventory,
    write_corridor_speed_network,
    write_edge_speed_network,
)
from providers.Modification.remove_edge import (
    EdgeTargetError,
    check_edge_removal_feasibility,
    resolve_opposite_edge,
    resolve_edge_target,
    rewrite_sumocfg_inputs,
    route_demand_units,
)


def _write(path, text):
    path.write_text(text.strip(), encoding="utf-8")
    return str(path)


def _network(tmp_path, *, with_bypass=True):
    bypass = '<connection from="alt" to="s"/>' if with_bypass else ""
    return _write(
        tmp_path / "net.net.xml",
        f"""
        <net>
          <edge id="p" name="Main Street" from="j0" to="j1"/>
          <edge id="target" name="Unique Road" from="j1" to="j2"><lane id="target_0" speed="10"/></edge>
          <edge id="s" name="Main Street" from="j2" to="j3"/>
          <edge id="alt" name="Bypass" from="j1" to="j2"/>
          <connection from="p" to="target"/>
          <connection from="target" to="s"/>
          <connection from="p" to="alt"/>
          {bypass}
        </net>
        """,
    )


def test_resolve_edge_prefers_id_and_requires_unique_name(tmp_path):
    net = _network(tmp_path)

    by_id = resolve_edge_target(net, "target")
    by_name = resolve_edge_target(net, "Unique Road")

    assert by_id["matched_by"] == "edge_id"
    assert by_name["edge_id"] == "target"
    assert by_name["matched_by"] == "street_name"
    with pytest.raises(EdgeTargetError, match="matches 2 SUMO edges") as exc_info:
        resolve_edge_target(net, "Main Street")
    assert exc_info.value.error_type == "AMBIGUOUS_EDGE_NAME"
    assert {row["edge_id"] for row in exc_info.value.candidates} == {"p", "s"}


def test_resolve_opposite_edge_uses_reversed_junctions_and_rejects_ambiguity(tmp_path):
    net = _write(
        tmp_path / "directions.net.xml",
        """
        <net>
          <edge id="forward" name="Main Road" from="a" to="b"/>
          <edge id="reverse" name="Main Road" from="b" to="a"/>
        </net>
        """,
    )
    resolved = resolve_opposite_edge(net, "forward")
    assert resolved["keep_edge"]["edge_id"] == "forward"
    assert resolved["opposite_edge"]["edge_id"] == "reverse"
    assert resolved["matched_by"] == "reversed_junctions"

    ambiguous_net = _write(
        tmp_path / "parallel.net.xml",
        """
        <net>
          <edge id="forward" from="a" to="b"/>
          <edge id="reverse_1" from="b" to="a"/>
          <edge id="reverse_2" from="b" to="a"/>
        </net>
        """,
    )
    with pytest.raises(EdgeTargetError) as exc_info:
        resolve_opposite_edge(ambiguous_net, "forward")
    assert exc_info.value.error_type == "AMBIGUOUS_OPPOSITE_EDGE"
    explicit = resolve_opposite_edge(ambiguous_net, "forward", "reverse_2")
    assert explicit["opposite_edge"]["edge_id"] == "reverse_2"
    assert explicit["matched_by"] == "explicit_edge_id"


def test_one_way_capability_validates_exact_kept_direction(tmp_path):
    net = _write(
        tmp_path / "one_way.net.xml",
        """
        <net>
          <edge id="forward" name="Main Road" from="a" to="b"/>
          <edge id="reverse" name="Main Road" from="b" to="a"/>
        </net>
        """,
    )
    valid = validate_modifier_capability(
        "make_road_one_way",
        {"net_path": net, "keep_edge_id": "forward"},
        {"net": net},
    )
    invalid = validate_modifier_capability(
        "make_road_one_way",
        {"net_path": net, "keep_edge_id": "Main Road"},
        {"net": net},
    )
    assert valid["ok"] is True
    assert valid["extra_params"] == []
    assert invalid["ok"] is False
    assert "No exact edge ID" in invalid["edge_target_errors"][0]


def test_feasibility_detects_missing_bypass(tmp_path):
    feasible = check_edge_removal_feasibility(_network(tmp_path), "target")
    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    blocked = check_edge_removal_feasibility(_network(blocked_dir, with_bypass=False), "target")

    assert feasible["ok"] is True
    assert blocked["ok"] is False
    assert blocked["blocked_pairs"] == [{"from_edge": "p", "to_edge": "s"}]


def test_route_demand_units_handles_vehicles_trips_and_flows(tmp_path):
    routes = _write(
        tmp_path / "routes.rou.xml",
        """
        <routes>
          <vehicle id="v1"/>
          <trip id="t1"/>
          <flow id="f1" number="10"/>
          <flow id="f2" begin="0" end="3600" vehsPerHour="300"/>
          <flow id="f3" begin="0" end="100" period="10"/>
        </routes>
        """,
    )

    assert route_demand_units(routes) == 322.0


def test_rewrite_sumocfg_inputs_preserves_other_settings(tmp_path):
    cfg = tmp_path / "sim.sumocfg"
    _write(
        cfg,
        """
        <configuration>
          <input><net-file value="old.net.xml"/><route-files value="old.rou.xml"/></input>
          <time><end value="100"/></time>
        </configuration>
        """,
    )

    rewrite_sumocfg_inputs(str(cfg), str(tmp_path / "new.net.xml"), str(tmp_path / "new.rou.xml"))
    root = ET.parse(cfg).getroot()

    assert root.find("./input/net-file").get("value") == str(tmp_path / "new.net.xml")
    assert root.find("./input/route-files").get("value") == str(tmp_path / "new.rou.xml")
    assert root.find("./time/end").get("value") == "100"


def test_capability_validates_edge_target_and_no_longer_requires_volume(tmp_path):
    net = _network(tmp_path)
    artifacts = {"net": net}

    valid = validate_modifier_capability(
        "remove_edge",
        {"net_path": net, "edge_id": "target"},
        artifacts,
    )
    ambiguous = validate_modifier_capability(
        "remove_edge",
        {"net_path": net, "edge_id": "Main Street"},
        artifacts,
    )

    assert valid["ok"] is True
    assert ambiguous["ok"] is False
    assert "matches 2 SUMO edges" in ambiguous["edge_target_errors"][0]

    blocked_dir = tmp_path / "blocked_capability"
    blocked_dir.mkdir()
    blocked_net = _network(blocked_dir, with_bypass=False)
    blocked = validate_modifier_capability(
        "remove_edge",
        {"net_path": blocked_net, "edge_id": "target"},
        {"net": blocked_net},
    )
    assert blocked["ok"] is False
    assert "blocks 1 local movement" in blocked["edge_target_errors"][0]


def test_reroute_capability_requires_full_retention_and_positive_soft_penalty(tmp_path):
    net = _network(tmp_path)
    routes = _write(tmp_path / "routes.rou.xml", '<routes><vehicle id="v0"/></routes>')
    artifacts = {"net": net, "routes": routes}

    valid = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target"],
            "reroute_fraction": 0.5,
            "min_route_retention": 1.0,
            "avoid_penalty_s": 3600,
        },
        artifacts,
    )
    invalid = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target"],
            "reroute_fraction": 0.5,
            "min_route_retention": 0.95,
        },
        artifacts,
    )

    assert valid["ok"] is True
    assert valid["extra_params"] == []
    assert invalid["ok"] is False
    assert "reroute_flows requires min_route_retention=1.0" in invalid["range_errors"]

    invalid_penalty = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target"],
            "reroute_fraction": 0.5,
            "avoid_penalty_s": 0,
        },
        artifacts,
    )
    assert invalid_penalty["ok"] is False
    assert "avoid_penalty_s must be > 0" in invalid_penalty["range_errors"]

    invalid_fraction = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target"],
            "reroute_fraction": 0.3,
        },
        artifacts,
    )
    assert invalid_fraction["ok"] is False
    assert "reroute_fraction must be one of [0.25, 0.5, 0.75, 1.0]" in invalid_fraction["range_errors"]


def test_reroute_capability_accepts_up_to_three_distinct_avoid_edges(tmp_path):
    net = _network(tmp_path)
    routes = _write(tmp_path / "routes.rou.xml", '<routes><vehicle id="v0"/></routes>')

    accepted = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target", "alt", "third"],
            "reroute_fraction": 0.25,
        },
        {"net": net, "routes": routes},
    )
    too_many = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["e1", "e2", "e3", "e4"],
            "reroute_fraction": 0.25,
        },
        {"net": net, "routes": routes},
    )
    duplicates = validate_modifier_capability(
        "reroute_flows",
        {
            "net_path": net,
            "route_path": routes,
            "avoid_edges": ["target", "target"],
            "reroute_fraction": 0.25,
        },
        {"net": net, "routes": routes},
    )

    assert accepted["ok"] is True
    assert too_many["ok"] is False
    assert "avoid_edges must contain between one and three edge IDs" in too_many["argument_shape_errors"]
    assert duplicates["ok"] is False
    assert "avoid_edges must contain distinct edge IDs" in duplicates["argument_shape_errors"]


def test_set_edge_speed_validates_and_writes_isolated_network(tmp_path):
    net = _network(tmp_path)
    artifacts = {"net": net}

    valid = validate_modifier_capability(
        "set_edge_speed",
        {"net_path": net, "edge_id": "target", "speed_mps": 8.0},
        artifacts,
    )
    excessive = validate_modifier_capability(
        "set_edge_speed",
        {"net_path": net, "edge_id": "target", "speed_mps": 6.0},
        artifacts,
    )
    unchanged = validate_modifier_capability(
        "set_edge_speed",
        {"net_path": net, "edge_id": "target", "speed_mps": 10.0},
        artifacts,
    )

    assert valid["ok"] is True
    assert excessive["ok"] is False
    assert "more than 30%" in excessive["edge_target_errors"][0]
    assert unchanged["ok"] is False
    result = write_edge_speed_network(net, "target", 8.0, str(tmp_path / "candidate"))
    assert result["net"] != net
    lane = ET.parse(result["net"]).getroot().find("./edge[@id='target']/lane")
    assert float(lane.get("speed")) == 8.0
    original_lane = ET.parse(net).getroot().find("./edge[@id='target']/lane")
    assert float(original_lane.get("speed")) == 10.0


def test_set_corridor_speed_resolves_connected_road_and_scales_each_lane(tmp_path):
    net = _write(
        tmp_path / "corridor.net.xml",
        """
        <net>
          <edge id="e0" name="Main Road" from="j0" to="j1"><lane id="e0_0" speed="10" length="20" shape="0,0 20,0"/></edge>
          <edge id="e1" name="Main Road" from="j1" to="j2"><lane id="e1_0" speed="12" length="20" shape="20,0 40,0"/></edge>
          <edge id="e2" name="Main Road" from="j2" to="j3"><lane id="e2_0" speed="10" length="20" shape="40,0 60,0"/></edge>
          <edge id="e3" name="Main Road" from="j3" to="j4"><lane id="e3_0" speed="11" length="20" shape="60,0 80,0"/></edge>
          <edge id="e4" name="Main Road" from="j4" to="j5"><lane id="e4_0" speed="10" length="20" shape="80,0 100,0"/></edge>
          <edge id="reverse" name="Main Road" from="j3" to="j2"><lane id="reverse_0" speed="10" length="20" shape="60,0 40,0"/></edge>
          <edge id="branch" name="Main Road" from="j3" to="jb"><lane id="branch_0" speed="10" length="20" shape="60,0 60,20"/></edge>
          <edge id="other" name="Side Road" from="j5" to="j6"><lane id="other_0" speed="10" length="20"/></edge>
        </net>
        """,
    )

    inventory = corridor_speed_inventory(net, "e2", 5)
    assert inventory["edge_ids"] == ["e0", "e1", "e2", "e3", "e4"]
    assert inventory["resolved_edge_count"] == 5
    assert inventory["total_length_m"] == 100.0

    valid = validate_modifier_capability(
        "set_corridor_speed",
        {"net_path": net, "edge_id": "e2", "speed_factor": 0.8, "edge_count": 5},
        {"net": net},
    )
    assert valid["ok"] is True

    result = write_corridor_speed_network(net, "e2", 0.8, str(tmp_path / "candidate"), 5)
    modified = ET.parse(result["net"]).getroot()
    assert float(modified.find("./edge[@id='e1']/lane").get("speed")) == pytest.approx(9.6)
    assert float(modified.find("./edge[@id='e4']/lane").get("speed")) == pytest.approx(8.0)
    assert float(modified.find("./edge[@id='branch']/lane").get("speed")) == 10.0
    assert float(ET.parse(net).getroot().find("./edge[@id='e2']/lane").get("speed")) == 10.0


def test_set_corridor_speed_rejects_invalid_factor_and_isolated_anchor(tmp_path):
    net = _network(tmp_path)
    invalid_factor = validate_modifier_capability(
        "set_corridor_speed",
        {"net_path": net, "edge_id": "target", "speed_factor": 0.5, "edge_count": 5},
        {"net": net},
    )
    isolated = validate_modifier_capability(
        "set_corridor_speed",
        {"net_path": net, "edge_id": "target", "speed_factor": 0.8, "edge_count": 5},
        {"net": net},
    )

    assert invalid_factor["ok"] is False
    assert "speed_factor must be in [0.7, 0.95]" in invalid_factor["range_errors"]
    assert isolated["ok"] is False
    assert "no contiguous same-road corridor" in isolated["edge_target_errors"][0]
