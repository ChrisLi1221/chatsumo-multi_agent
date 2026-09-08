import sys
import types

import pytest

from agents.objectives import traffic_objective

# Metrics are pure functions, but the provider module also exposes them through
# FastMCP. Keep unit tests runnable in minimal environments without MCP extras.
if "fastmcp" not in sys.modules:
    fastmcp = types.ModuleType("fastmcp")

    class _FastMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self, *args, **kwargs):
            if args and callable(args[0]):
                return args[0]
            return lambda fn: fn

        def run(self, *args, **kwargs):
            pass

    fastmcp.FastMCP = _FastMCP
    sys.modules["fastmcp"] = fastmcp

from providers.provider_metrics import (
    _edge_group_performance_data,
    _network_diagnosis_data,
    _trip_delay_summary,
)


def test_trip_delay_penalizes_unfinished_vehicles(tmp_path):
    tripinfo = tmp_path / "tripinfo.xml"
    tripinfo.write_text(
        """<tripinfos>
        <tripinfo id="v0" duration="20" routeLength="100" waitingTime="2" timeLoss="10"/>
        <tripinfo id="v1" duration="30" routeLength="100" waitingTime="4" timeLoss="20"/>
        <statistics><vehicles loaded="4" inserted="4" running="2" waiting="0"/></statistics>
        </tripinfos>""",
        encoding="utf-8",
    )

    result = _trip_delay_summary(str(tripinfo), unfinished_penalty_s=100.0)

    assert result["completed_trips"] == 2
    assert result["departed_trips"] == 4
    assert result["unfinished_trips"] == 2
    assert result["completion_rate"] == pytest.approx(0.5)
    assert result["penalized_avg_time_loss_s"] == pytest.approx(57.5)


def test_edge_averages_include_stopped_active_interval(tmp_path):
    net = tmp_path / "net.xml"
    net.write_text(
        """<net><edge id="e0" from="n0" to="n1">
        <lane id="e0_0" speed="10" length="100"/>
        </edge></net>""",
        encoding="utf-8",
    )
    edge_data = tmp_path / "edgedata.xml"
    edge_data.write_text(
        """<meandata>
        <interval begin="0" end="10"><edge id="e0" sampledSeconds="10" speed="10" density="10" occupancy="10"/></interval>
        <interval begin="10" end="30"><edge id="e0" sampledSeconds="20" speed="0" density="30" occupancy="30"/></interval>
        </meandata>""",
        encoding="utf-8",
    )

    result = _network_diagnosis_data(str(edge_data), str(net))
    edge = result["top_congested_edges"][0]

    assert edge["avg_speed_mps"] == pytest.approx(10.0 / 3.0)
    assert edge["speed_ratio"] == pytest.approx(1.0 / 3.0)
    assert edge["avg_density_veh_per_km"] == pytest.approx(70.0 / 3.0)


def test_top_congested_edges_exclude_fragments_shorter_than_ten_meters(tmp_path):
    net = tmp_path / "net.xml"
    net.write_text(
        """<net>
        <edge id="short" from="n0" to="n1">
          <lane id="short_0" speed="10" length="9.99"/>
        </edge>
        <edge id="boundary" from="n1" to="n2">
          <lane id="boundary_0" speed="10" length="10.0"/>
        </edge>
        <edge id="long" from="n2" to="n3">
          <lane id="long_0" speed="10" length="50"/>
        </edge>
        </net>""",
        encoding="utf-8",
    )
    edge_data = tmp_path / "edgedata.xml"
    edge_data.write_text(
        """<meandata><interval begin="0" end="60">
          <edge id="short" sampledSeconds="100" speed="0.1" density="100"
                occupancy="100" waitingTime="1000" timeLoss="1000"/>
          <edge id="boundary" sampledSeconds="50" speed="4" density="40"
                occupancy="35" waitingTime="100" timeLoss="100"/>
          <edge id="long" sampledSeconds="40" speed="5" density="35"
                occupancy="30" waitingTime="80" timeLoss="80"/>
        </interval></meandata>""",
        encoding="utf-8",
    )

    result = _network_diagnosis_data(str(edge_data), str(net), top_n=10)

    assert [row["edge_id"] for row in result["top_congested_edges"]] == [
        "boundary",
        "long",
    ]
    assert result["thresholds"]["minimum_top_edge_length_m"] == 10.0
    assert result["summary"]["top_congested_candidate_count"] == 2
    assert result["summary"]["short_edges_excluded_from_top_congested"] == 1


def test_edge_group_performance_uses_a_frozen_requested_set(tmp_path):
    edge_data = tmp_path / "edgedata.xml"
    edge_data.write_text(
        """<meandata>
        <interval begin="0" end="10">
          <edge id="top1" sampledSeconds="20" speed="5" waitingTime="8" timeLoss="12" entered="2"/>
          <edge id="top2" sampledSeconds="10" speed="10" waitingTime="3" timeLoss="4" left="1"/>
          <edge id="other" sampledSeconds="50" speed="2" waitingTime="20" timeLoss="30" entered="5"/>
        </interval>
        </meandata>""",
        encoding="utf-8",
    )

    result = _edge_group_performance_data(str(edge_data), ["top1", "top2", "missing"])

    assert result["total_time_loss_s"] == 16
    assert result["total_waiting_time_s"] == 11
    assert result["sampled_speed_mps"] == pytest.approx(20 / 3)
    assert result["volume_proxy"] == 3
    assert result["observed_edge_ids"] == ["top1", "top2"]
    assert result["missing_edge_ids"] == ["missing"]


def test_objective_uses_co2_and_completion_rate():
    baseline = {"mean_delay": 10, "co2": 20, "throughput": 0.8, "mean_speed": 10}
    current = {"mean_delay": 8, "co2": 18, "throughput": 0.9, "mean_speed": 11}

    result = traffic_objective(current, baseline)

    assert set(result["available_metrics"]) == {"co2", "delay", "speed", "throughput"}
    assert result["terms"]["co2"]["normalized"] == pytest.approx(0.9)
    assert result["terms"]["throughput"]["normalized"] == pytest.approx(1.125)
