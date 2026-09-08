import xml.etree.ElementTree as ET

from providers.Modification.reroute import (
    avoided_edge_usage,
    merge_partial_routes,
    select_eligible_vehicle_ids,
    validate_od_preservation,
    write_selected_trip_demand,
    write_soft_avoid_weights,
)


def _write(path, text):
    path.write_text(text.strip(), encoding="utf-8")
    return str(path)


def test_soft_avoid_weights_penalize_edges_without_editing_the_network(tmp_path):
    path = write_soft_avoid_weights(
        str(tmp_path / "weights.xml"),
        ["edge_a", "edge_b", "edge_a"],
        3600,
    )

    edges = ET.parse(path).getroot().findall("./interval/edge")
    assert [(edge.get("id"), edge.get("traveltime")) for edge in edges] == [
        ("edge_a", "3600"),
        ("edge_b", "3600"),
    ]


def test_od_validation_accepts_new_paths_with_the_same_endpoints(tmp_path):
    source = _write(
        tmp_path / "source.rou.xml",
        """
        <routes>
          <trip id="t0" depart="0" from="a" to="d"/>
          <vehicle id="v0" depart="1"><route edges="x y"/></vehicle>
        </routes>
        """,
    )
    output = _write(
        tmp_path / "output.rou.xml",
        """
        <routes>
          <vehicle id="t0" depart="0"><route edges="a c d"/></vehicle>
          <vehicle id="v0" depart="1"><route edges="x z y"/></vehicle>
        </routes>
        """,
    )

    validation = validate_od_preservation(source, output)

    assert validation["ok"] is True
    assert validation["missing_ids"] == []
    assert validation["endpoint_mismatches"] == []


def test_od_validation_rejects_dropped_and_changed_demand(tmp_path):
    source = _write(
        tmp_path / "source.rou.xml",
        '<routes><trip id="keep" from="a" to="d"/><trip id="lost" from="x" to="y"/></routes>',
    )
    output = _write(
        tmp_path / "output.rou.xml",
        '<routes><vehicle id="keep"><route edges="a c"/></vehicle></routes>',
    )

    validation = validate_od_preservation(source, output)

    assert validation["ok"] is False
    assert validation["missing_ids"] == ["lost"]
    assert validation["endpoint_mismatches"][0]["id"] == "keep"


def test_avoided_edge_usage_reports_unavoidable_routes(tmp_path):
    routes = _write(
        tmp_path / "routes.rou.xml",
        """
        <routes>
          <vehicle id="v0"><route edges="a target c"/></vehicle>
          <vehicle id="v1"><route edges="a bypass c"/></vehicle>
        </routes>
        """,
    )

    usage = avoided_edge_usage(routes, ["target"])

    assert usage["edge_vehicle_counts"] == {"target": 1}
    assert usage["vehicles_using_any_avoid_edge"] == 1
    assert usage["realized_route_count"] == 2


def test_fractional_selection_only_samples_vehicles_using_target_edge(tmp_path):
    routes = _write(
        tmp_path / "reference.rou.xml",
        """
        <routes>
          <vehicle id="v0"><route edges="a target d"/></vehicle>
          <vehicle id="v1"><route edges="b target e"/></vehicle>
          <vehicle id="v2"><route edges="c target f"/></vehicle>
          <vehicle id="v3"><route edges="g target h"/></vehicle>
          <vehicle id="unaffected"><route edges="a bypass d"/></vehicle>
        </routes>
        """,
    )

    first = select_eligible_vehicle_ids(routes, "target", 0.5)
    second = select_eligible_vehicle_ids(routes, "target", 0.5)

    assert first == second
    assert first["eligible_vehicle_count"] == 4
    assert first["selected_vehicle_count"] == 2
    assert first["realized_fraction"] == 0.5
    assert "unaffected" not in first["selected_vehicle_ids"]


def test_multi_edge_selection_samples_union_without_double_counting(tmp_path):
    routes = _write(
        tmp_path / "reference_multi.rou.xml",
        """
        <routes>
          <vehicle id="only_a"><route edges="start edge_a end"/></vehicle>
          <vehicle id="only_b"><route edges="start edge_b end"/></vehicle>
          <vehicle id="both"><route edges="start edge_a edge_b end"/></vehicle>
          <vehicle id="unaffected"><route edges="start bypass end"/></vehicle>
        </routes>
        """,
    )

    selection = select_eligible_vehicle_ids(routes, ["edge_a", "edge_b"], 1.0)

    assert selection["target_edges"] == ["edge_a", "edge_b"]
    assert selection["eligible_vehicle_count"] == 3
    assert selection["eligible_edge_vehicle_counts"] == {"edge_a": 2, "edge_b": 2}
    assert selection["vehicles_using_multiple_target_edges"] == 1
    assert selection["selected_vehicle_count"] == 3
    assert set(selection["selected_vehicle_ids"]) == {"only_a", "only_b", "both"}


def test_selected_demand_can_be_rerouted_and_merged_with_reference_routes(tmp_path):
    source = _write(
        tmp_path / "source.rou.xml",
        """
        <routes>
          <trip id="v0" depart="0" from="a" to="d"/>
          <trip id="v1" depart="1" from="b" to="e"/>
        </routes>
        """,
    )
    reference = _write(
        tmp_path / "reference.rou.xml",
        """
        <routes>
          <vehicle id="v0" depart="0"><route edges="a target d"/></vehicle>
          <vehicle id="v1" depart="1"><route edges="b target e"/></vehicle>
        </routes>
        """,
    )
    selected_demand = write_selected_trip_demand(
        source,
        ["v0"],
        str(tmp_path / "selected.rou.xml"),
    )
    selected_output = _write(
        tmp_path / "selected_output.rou.xml",
        '<routes><vehicle id="v0" depart="0"><route edges="a bypass d"/></vehicle></routes>',
    )

    merge = merge_partial_routes(
        reference,
        selected_output,
        ["v0"],
        str(tmp_path / "merged.rou.xml"),
    )
    selected_nodes = ET.parse(selected_demand).getroot().findall("trip")
    merged_routes = {
        node.get("id"): node.find("route").get("edges")
        for node in ET.parse(merge["path"]).getroot().findall("vehicle")
    }

    assert [node.get("id") for node in selected_nodes] == ["v0"]
    assert merged_routes == {"v0": "a bypass d", "v1": "b target e"}
    assert merge["replaced_vehicle_count"] == 1
    assert merge["changed_route_count"] == 1
