from agents.bundle_optimizer import (
    check_bundle_compatibility,
    generate_pair_bundles,
    order_bundle_actions,
)


def _proposal(proposal_id, action, args=None, score=1.0):
    return {
        "proposal_id": proposal_id,
        "agent": proposal_id,
        "tool": "provider_sumo_modifier",
        "action": action,
        "args": args or {},
        "score": score,
        "validation": {"ok": True},
        "risk": f"risk for {proposal_id}",
    }


def test_bundle_actions_use_domain_safe_order():
    tls = _proposal("signal", "tls_optimize_and_apply", {"tl_id": "tls_1"})
    routing = _proposal("routing", "reroute_flows", {"avoid_edges": ["e2"]})
    topology = _proposal("network", "remove_edge", {"edge_id": "e1"})

    ordered = order_bundle_actions([tls, routing, topology])

    assert [proposal["action"] for proposal in ordered] == [
        "remove_edge",
        "reroute_flows",
        "tls_optimize_and_apply",
    ]


def test_bundle_rejects_same_family_and_removed_edge_conflicts():
    remove = _proposal("remove", "remove_edge", {"edge_id": "e1"})
    speed = _proposal("speed", "set_edge_speed", {"edge_id": "e1", "speed_mps": 8})
    routing = _proposal("routing", "reroute_flows", {"avoid_edges": ["e1"]})
    second_speed = _proposal("speed_2", "set_edge_speed", {"edge_id": "e2", "speed_mps": 9})

    removal_speed = check_bundle_compatibility([remove, speed])
    removal_routing = check_bundle_compatibility([remove, routing])
    two_speeds = check_bundle_compatibility([speed, second_speed])

    assert removal_speed["ok"] is False
    assert "removed and speed-modified" in removal_speed["reasons"][0]
    assert removal_routing["ok"] is False
    assert "already removed" in removal_routing["reasons"][0]
    assert two_speeds["ok"] is False
    assert "at most one speed action" in two_speeds["reasons"][0]


def test_bundle_rejects_speed_change_that_rerouting_softly_avoids():
    speed = _proposal("speed", "set_edge_speed", {"edge_id": "e1", "speed_mps": 8})
    routing = _proposal("routing", "reroute_flows", {"avoid_edges": ["e1"]})

    compatibility = check_bundle_compatibility([speed, routing])

    assert compatibility["ok"] is False
    assert "speed-modified and softly avoided" in compatibility["reasons"][0]


def test_bundle_detects_routing_conflict_on_an_adjacent_corridor_edge(tmp_path):
    net = tmp_path / "corridor.net.xml"
    net.write_text(
        """
        <net>
          <edge id="e1" name="Main Road" from="a" to="b"><lane id="e1_0" speed="10" length="10"/></edge>
          <edge id="e2" name="Main Road" from="b" to="c"><lane id="e2_0" speed="10" length="10"/></edge>
          <edge id="e3" name="Main Road" from="c" to="d"><lane id="e3_0" speed="10" length="10"/></edge>
        </net>
        """.strip(),
        encoding="utf-8",
    )
    corridor = _proposal(
        "corridor",
        "set_corridor_speed",
        {"net_path": str(net), "edge_id": "e2", "speed_factor": 0.8, "edge_count": 3},
    )
    routing = _proposal("routing", "reroute_flows", {"avoid_edges": ["e3"]})

    compatibility = check_bundle_compatibility([corridor, routing])

    assert compatibility["ok"] is False
    assert "e3" in compatibility["reasons"][0]


def test_pair_generation_uses_measured_quality_and_filters_conflicts():
    signal = _proposal("signal", "tls_optimize_and_apply", {"tl_id": "tls_1"}, score=1)
    routing = _proposal("routing", "reroute_flows", {"avoid_edges": ["e2"]}, score=3)
    speed = _proposal("speed", "set_edge_speed", {"edge_id": "e1", "speed_mps": 8}, score=2)
    remove = _proposal("remove", "remove_edge", {"edge_id": "e1"}, score=10)

    bundles = generate_pair_bundles(
        [signal, routing, speed, remove],
        top_k=4,
        max_bundles=3,
        quality_by_id={"signal": 10, "routing": 9, "speed": 8, "remove": 7},
    )

    assert bundles[0]["bundle_id"] == "bundle__routing__signal"
    assert [action["action"] for action in bundles[0]["actions"]] == [
        "reroute_flows",
        "tls_optimize_and_apply",
    ]
    assert all(
        {action["source_proposal_id"] for action in bundle["actions"]} != {"speed", "remove"}
        for bundle in bundles
    )
    assert all(bundle["bundle_size"] == 2 for bundle in bundles)
