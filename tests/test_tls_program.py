import os
import xml.etree.ElementTree as ET

import pytest

from agents.capabilities import validate_modifier_capability
from providers.Modification.tls_program import (
    attach_additional_file,
    build_optimized_tllogic,
    clone_cfg_for_run,
    estimate_phase_demands_from_routes,
    load_tls_inventory,
    read_sumocfg_paths,
    write_tls_additional,
)


def _write(path, text):
    path.write_text(text.strip(), encoding="utf-8")
    return str(path)


def _network(tmp_path):
    return _write(
        tmp_path / "net.net.xml",
        """
        <net>
          <tlLogic id="J1" type="static" programID="0" offset="5">
            <phase duration="42" state="GGrr"/>
            <phase duration="3" state="yyrr"/>
            <phase duration="42" state="rrGG"/>
            <phase duration="3" state="rryy"/>
          </tlLogic>
          <connection from="north" to="south" tl="J1" linkIndex="0"/>
          <connection from="south" to="north" tl="J1" linkIndex="1"/>
          <connection from="east" to="west" tl="J1" linkIndex="2"/>
          <connection from="west" to="east" tl="J1" linkIndex="3"/>
        </net>
        """,
    )


def test_inventory_and_optimization_preserve_phase_states(tmp_path):
    inventory = load_tls_inventory(_network(tmp_path), "J1")

    assert inventory["green_phase_indices"] == [0, 2]
    assert inventory["phase_edge_groups"] == [["north", "south"], ["east", "west"]]
    assert inventory["optimizable"] is True

    tl_logic, result = build_optimized_tllogic(
        inventory,
        demands=[1, 3],
        cycle=90,
        min_split=0.1,
    )
    phases = tl_logic.findall("phase")

    assert [phase.get("state") for phase in phases] == ["GGrr", "yyrr", "rrGG", "rryy"]
    assert [float(phase.get("duration")) for phase in phases] == [25.2, 3.0, 58.8, 3.0]
    assert sum(float(phase.get("duration")) for phase in phases) == pytest.approx(90.0)
    assert result["fixed_intergreen_time"] == 6.0


def test_zero_green_share_is_rejected_before_writing_sumo_program(tmp_path):
    inventory = load_tls_inventory(_network(tmp_path), "J1")

    with pytest.raises(ValueError, match=r"phase 2 duration must be > 0"):
        build_optimized_tllogic(
            inventory,
            demands=[1, 0],
            cycle=90,
            min_split=0.0,
        )


def test_existing_non_positive_phase_is_rejected_before_simulation(tmp_path):
    inventory = load_tls_inventory(_network(tmp_path), "J1")
    inventory["phases"][1]["duration"] = 0.0
    inventory["phases"][1]["attributes"]["duration"] = "0"

    with pytest.raises(ValueError, match=r"phase 1 duration must be > 0"):
        build_optimized_tllogic(inventory, demands=[1, 1])


def test_attach_additional_uses_input_section(tmp_path):
    cfg = tmp_path / "sim.sumocfg"
    existing = tmp_path / "edge_data.add.xml"
    tls_add = tmp_path / "tls.add.xml"
    _write(existing, "<additional/>")
    _write(tls_add, "<additional/>")
    _write(
        cfg,
        f"""
        <configuration>
          <input><additional-files value="{existing}"/></input>
        </configuration>
        """,
    )

    attach_additional_file(str(cfg), str(tls_add))
    root = ET.parse(cfg).getroot()
    node = root.find("./input/additional-files")

    assert root.find("./additional-files") is None
    assert node is not None
    assert node.get("value").split(",") == [str(existing), "tls.add.xml"]


def test_attach_additional_replaces_previous_candidate_with_same_name(tmp_path):
    cfg = tmp_path / "sim.sumocfg"
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_tls = old_dir / "tls_J1_opt.add.xml"
    new_tls = new_dir / "tls_J1_opt.add.xml"
    _write(old_tls, "<additional/>")
    _write(new_tls, "<additional/>")
    _write(
        cfg,
        f'<configuration><input><additional-files value="{old_tls}"/></input></configuration>',
    )

    attach_additional_file(str(cfg), str(new_tls))
    value = ET.parse(cfg).getroot().find("./input/additional-files").get("value")

    assert value == os.path.relpath(new_tls, tmp_path)


def test_clone_cfg_redirects_tripinfo_and_edgedata(tmp_path):
    net = _network(tmp_path)
    routes = _write(tmp_path / "routes.rou.xml", "<routes/>")
    edge_add = tmp_path / "edge_data.add.xml"
    _write(edge_add, '<additional><edgeData id="e" file="old-edgedata.xml"/></additional>')
    cfg = tmp_path / "sim.sumocfg"
    _write(
        cfg,
        f"""
        <configuration>
          <input>
            <net-file value="{net}"/>
            <route-files value="{routes}"/>
            <additional-files value="{edge_add}"/>
          </input>
          <report><tripinfo-output value="old-tripinfo.xml"/></report>
        </configuration>
        """,
    )
    out_dir = tmp_path / "candidate"

    cloned = clone_cfg_for_run(str(cfg), str(out_dir))
    info = read_sumocfg_paths(cloned)

    assert info["tripinfo"] == str(out_dir / "tripinfo.xml")
    assert info["additional_files"] == [str(out_dir / "edge_data.add.xml")]
    edge_node = ET.parse(info["additional_files"][0]).getroot().find("edgeData")
    assert edge_node.get("file") == str(out_dir / "edgedata.xml")


def test_route_demand_estimation_supports_route_refs_and_flows(tmp_path):
    route_path = _write(
        tmp_path / "routes.rou.xml",
        """
        <routes>
          <route id="r1" edges="a north b"/>
          <vehicle id="v1" route="r1"/>
          <flow id="f1" begin="0" end="3600" vehsPerHour="300">
            <route edges="a east b"/>
          </flow>
          <trip id="t1" from="south" to="z"/>
        </routes>
        """,
    )

    demands = estimate_phase_demands_from_routes(
        route_path,
        [["north", "south"], ["east", "west"]],
    )

    assert demands == [2.0, 300.0]


def test_nonexistent_and_single_green_tls_are_rejected(tmp_path):
    net = _network(tmp_path)
    with pytest.raises(ValueError, match="was not found"):
        load_tls_inventory(net, "missing")

    inventory = load_tls_inventory(net, "J1")
    inventory["green_phase_indices"] = [0]
    with pytest.raises(ValueError, match="fewer than two"):
        build_optimized_tllogic(inventory, demands=[1])


def test_written_tls_additional_is_valid_xml(tmp_path):
    inventory = load_tls_inventory(_network(tmp_path), "J1")
    tl_logic, _ = build_optimized_tllogic(inventory, demands=[1, 1])
    path = write_tls_additional(str(tmp_path / "tls.add.xml"), tl_logic)

    root = ET.parse(path).getroot()
    assert root.tag == "additional"
    assert root.find("tlLogic").get("id") == "J1"


def test_capability_validation_checks_real_tls_and_phase_count(tmp_path):
    net = _network(tmp_path)
    cfg = tmp_path / "sim.sumocfg"
    _write(
        cfg,
        f"""
        <configuration><input><net-file value="{net}"/></input></configuration>
        """,
    )
    artifacts = {"cfg": str(cfg)}

    valid = validate_modifier_capability(
        "tls_optimize_and_apply",
        {"cfg_path": str(cfg), "tl_id": "J1", "demands": [1, 2]},
        artifacts,
    )
    missing = validate_modifier_capability(
        "tls_optimize_and_apply",
        {"cfg_path": str(cfg), "tl_id": "missing"},
        artifacts,
    )
    wrong_count = validate_modifier_capability(
        "tls_optimize_and_apply",
        {"cfg_path": str(cfg), "tl_id": "J1", "demands": [1]},
        artifacts,
    )
    zero_min_split = validate_modifier_capability(
        "tls_optimize_and_apply",
        {"cfg_path": str(cfg), "tl_id": "J1", "min_split": 0.0},
        artifacts,
    )
    zero_explicit_split = validate_modifier_capability(
        "tls_apply_program",
        {"cfg_path": str(cfg), "tl_id": "J1", "cycle": 90, "splits": [1.0, 0.0]},
        artifacts,
    )

    assert valid["ok"] is True
    assert missing["ok"] is False
    assert "was not found" in missing["tls_inventory_errors"][0]
    assert wrong_count["ok"] is False
    assert "requires 2 phase values" in wrong_count["tls_inventory_errors"][0]
    assert zero_min_split["ok"] is False
    assert "min_split must be a fraction in (0.0, 0.5)" in zero_min_split["range_errors"]
    assert zero_explicit_split["ok"] is False
    assert "splits must be strictly positive" in zero_explicit_split["range_errors"]
