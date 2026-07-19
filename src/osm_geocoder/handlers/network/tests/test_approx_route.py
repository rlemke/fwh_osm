"""Phase 2 tests for osm.Network.ApproxRoute — snap + Dijkstra + reachability.

Deterministic synthetic networks built in a tmp FW_DATA_ROOT, then routed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")
pytest.importorskip("networkx")

from osm_geocoder.handlers.network import network_ops as ops


def _line(coords, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


def _build(tmp_path, monkeypatch, features, ref_filter=""):
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("FW_OUTPUT_BASE", str(tmp_path / "out"))
    src = tmp_path / "edges.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return ops.build_network(str(src), ref_filter=ref_filter, snap_tolerance_m=25.0).network_path


# Two segments sharing a node at (0,1): a routable 3-node chain (0,0)-(0,1)-(0,2).
_CHAIN = [_line([[0, 0], [0, 1]], ref="I 5"), _line([[0, 1], [0, 2]], ref="I 5")]
# Two disjoint chains in different components.
_DISJOINT = [_line([[0, 0], [0, 1]], ref="I 5"), _line([[5, 5], [5, 6]], ref="I 80")]


def test_route_reaches_b_along_the_chain(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    r = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.0)
    assert r.reached_b is True
    assert r.node_hops == 3                       # n(0,0) -> n(0,1) -> n(0,2)
    assert 220.0 <= r.distance_km <= 223.0        # 2 degrees of latitude
    assert r.reached_lat == pytest.approx(2.0, abs=1e-6)
    assert r.reached_lon == pytest.approx(0.0, abs=1e-6)
    assert r.gap_to_b_km == pytest.approx(0.0, abs=0.05)


def test_gap_is_offnet_access_distance_when_reached(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    # B sits ~1 km east of the (0,2) node but still snaps to it (reached).
    r = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.01)
    assert r.reached_b is True
    assert r.reached_lon == pytest.approx(0.0, abs=1e-6)   # snapped onto the network
    assert 0.8 <= r.gap_to_b_km <= 1.3                     # ~1 km off-freeway last mile


def test_unreachable_b_returns_closest_reachable_point(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _DISJOINT)
    # A in component 1, B in component 2 -> cannot reach; stop at closest reachable node.
    r = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=6.0, to_lon=5.0)
    assert r.reached_b is False
    assert r.reached_lat == pytest.approx(1.0, abs=1e-6)   # the (0,1) node, closest to B
    assert r.reached_lon == pytest.approx(0.0, abs=1e-6)
    assert r.gap_to_b_km > 500.0                            # large unreachable residual
    assert 110.0 <= r.distance_km <= 112.0                  # on-net distance to the reached node


def test_route_path_is_a_linestring_tracing_the_path(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    r = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.0)
    fc = json.loads(Path(r.route_path).read_text())
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "LineString"
    coords = feat["geometry"]["coordinates"]
    assert coords[0] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert coords[-1] == pytest.approx([0.0, 2.0], abs=1e-6)
    assert feat["properties"]["reached_b"] is True


def test_route_is_deterministic(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    a = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.0)
    b = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.0)
    assert (a.distance_km, a.node_hops, a.reached_b) == (b.distance_km, b.node_hops, b.reached_b)


def test_network_is_loaded_once_and_memoized(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    ops._GRAPH_CACHE.clear()
    ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=2.0, to_lon=0.0)
    keys_after_first = set(ops._GRAPH_CACHE)
    assert len(keys_after_first) == 1
    ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=1.0, to_lon=0.0)
    assert set(ops._GRAPH_CACHE) == keys_after_first   # reused, not reloaded


def test_empty_network_is_valid_unreachable_not_error(tmp_path, monkeypatch):
    """A validly-built EMPTY network (region with no major-tier roads, e.g.
    Haiti) yields a valid unreachable result — distance_km = -1.0 sentinel,
    gap = straight-line A->B — instead of raising. Corrupt/missing artifacts
    still fail loudly inside _load_network (sidecar validation)."""
    net = _build(tmp_path, monkeypatch, [])   # zero features -> 0-node graph
    r = ops.approx_route(net, from_lat=0.0, from_lon=0.0, to_lat=1.0, to_lon=0.0)
    assert r.reached_b is False
    assert r.distance_km == -1.0
    assert r.node_hops == 0
    assert 110.0 <= r.gap_to_b_km <= 112.0    # ~1 degree of latitude, crow-flies
    assert r.route_path == ""
