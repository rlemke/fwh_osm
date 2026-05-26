"""Phase 3 tests for osm.Network.RouteMatrix — all-pairs + input adapters.

Deterministic synthetic networks; exercises the JSON / GeoJSON / "lon,lat;..."
point inputs (the GeoJSON->waypoints gap closure) and unreachable pairs.
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


def _point(lon, lat, name):
    return {"type": "Feature", "properties": {"name": name},
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _build(tmp_path, monkeypatch, features, ref_filter=""):
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AFL_OUTPUT_BASE", str(tmp_path / "out"))
    src = tmp_path / "edges.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return ops.build_network(str(src), ref_filter=ref_filter, snap_tolerance_m=25.0).network_path


_CHAIN = [_line([[0, 0], [0, 1]], ref="I 5"), _line([[0, 1], [0, 2]], ref="I 5")]
_DISJOINT = [_line([[0, 0], [0, 1]], ref="I 5"), _line([[5, 5], [5, 6]], ref="I 80")]


def _pairs(result_path):
    return {(p["from"], p["to"]): p for p in json.loads(Path(result_path).read_text())["pairs"]}


def test_all_pairs_over_a_chain(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    pts = json.dumps([{"lon": 0, "lat": 0, "name": "A"},
                      {"lon": 0, "lat": 1, "name": "B"},
                      {"lon": 0, "lat": 2, "name": "C"}])
    res = ops.route_matrix(net, pts)
    assert res.pair_count == 6          # 3 * (3-1) ordered pairs
    assert res.reachable_count == 6
    p = _pairs(res.result_path)
    assert 110 <= p[("A", "B")]["distance_km"] <= 112
    assert 220 <= p[("A", "C")]["distance_km"] <= 223
    assert 110 <= p[("B", "C")]["distance_km"] <= 112
    # symmetric on an undirected graph
    assert p[("A", "C")]["distance_km"] == pytest.approx(p[("C", "A")]["distance_km"])
    assert all(v["reached_b"] for v in p.values())


def test_accepts_geojson_featurecollection_inline(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    fc = json.dumps({"type": "FeatureCollection",
                     "features": [_point(0, 0, "A"), _point(0, 2, "C")]})
    res = ops.route_matrix(net, fc)
    assert res.pair_count == 2
    p = _pairs(res.result_path)
    assert 220 <= p[("A", "C")]["distance_km"] <= 223


def test_accepts_geojson_file_path(tmp_path, monkeypatch):
    """The GeoJSON->waypoints gap closure: a merged-cities layer feeds straight in."""
    net = _build(tmp_path, monkeypatch, _CHAIN)
    cities = tmp_path / "cities.geojson"
    cities.write_text(json.dumps({"type": "FeatureCollection",
                                  "features": [_point(0, 0, "A"), _point(0, 1, "B"), _point(0, 2, "C")]}))
    res = ops.route_matrix(net, str(cities))
    assert res.pair_count == 6
    assert res.reachable_count == 6


def test_accepts_lonlat_string(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    res = ops.route_matrix(net, "0,0;0,2")
    assert res.pair_count == 2
    p = _pairs(res.result_path)
    # unnamed points get positional names p0/p1
    assert 220 <= p[("p0", "p1")]["distance_km"] <= 223


def test_unreachable_pairs_marked(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _DISJOINT)
    pts = json.dumps([{"lon": 0, "lat": 0, "name": "A"}, {"lon": 5, "lat": 5, "name": "B"}])
    res = ops.route_matrix(net, pts)
    assert res.pair_count == 2
    assert res.reachable_count == 0          # different components
    p = _pairs(res.result_path)
    assert p[("A", "B")]["reached_b"] is False
    assert p[("B", "A")]["reached_b"] is False


def test_result_json_shape(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    res = ops.route_matrix(net, json.dumps([{"lon": 0, "lat": 0, "name": "A"},
                                            {"lon": 0, "lat": 2, "name": "C"}]))
    doc = json.loads(Path(res.result_path).read_text())
    assert doc["operation"] == "route_matrix"
    assert doc["point_count"] == 2
    assert doc["pair_count"] == res.pair_count == 2
    assert doc["reachable_count"] == res.reachable_count
    for pair in doc["pairs"]:
        assert set(pair) == {"from", "to", "distance_km", "reached_b", "gap_km"}


def test_needs_at_least_two_points(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    with pytest.raises(ValueError, match=">= 2 points"):
        ops.route_matrix(net, json.dumps([{"lon": 0, "lat": 0, "name": "A"}]))
