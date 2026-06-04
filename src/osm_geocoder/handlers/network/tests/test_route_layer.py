"""Tests for osm.Network.RouteLayer — drawable all-pairs route geometries."""

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


def test_route_layer_one_linestring_per_pair(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _CHAIN)
    pts = json.dumps([{"lon": 0, "lat": 0, "name": "A"},
                      {"lon": 0, "lat": 1, "name": "B"},
                      {"lon": 0, "lat": 2, "name": "C"}])
    res = ops.route_layer(net, pts)
    assert res.point_count == 3
    assert res.route_count == 3          # unordered pairs: AB, AC, BC
    assert res.reachable_count == 3
    fc = json.loads(Path(res.output_path).read_text())
    assert {f["geometry"]["type"] for f in fc["features"]} == {"LineString"}
    labels = {(f["properties"]["from"], f["properties"]["to"]) for f in fc["features"]}
    assert labels == {("A", "B"), ("A", "C"), ("B", "C")}
    ac = next(f for f in fc["features"] if f["properties"]["to"] == "C" and f["properties"]["from"] == "A")
    assert 220 <= ac["properties"]["distance_km"] <= 223
    assert ac["geometry"]["coordinates"][0] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert ac["geometry"]["coordinates"][-1] == pytest.approx([0.0, 2.0], abs=1e-6)


def test_route_layer_accepts_geojson_path(tmp_path, monkeypatch):
    """Feeds straight from a cities GeoJSON layer (the workflow path)."""
    net = _build(tmp_path, monkeypatch, _CHAIN)
    cities = tmp_path / "cities.geojson"
    cities.write_text(json.dumps({"type": "FeatureCollection",
                                  "features": [_point(0, 0, "A"), _point(0, 2, "C")]}))
    res = ops.route_layer(net, str(cities))
    assert res.route_count == 1
    assert res.reachable_count == 1


def test_route_layer_unreachable_pair_still_drawn(tmp_path, monkeypatch):
    net = _build(tmp_path, monkeypatch, _DISJOINT)
    pts = json.dumps([{"lon": 0, "lat": 0, "name": "A"}, {"lon": 5, "lat": 5, "name": "B"}])
    res = ops.route_layer(net, pts)
    assert res.point_count == 2
    assert res.reachable_count == 0           # different components
    fc = json.loads(Path(res.output_path).read_text())
    # the one pair is still represented (route to the closest reachable node)
    assert all(f["properties"]["reached_b"] is False for f in fc["features"])


def test_route_layer_tolerates_fewer_than_two_points(tmp_path, monkeypatch):
    # A sparse band can legitimately have 0 or 1 points (e.g. a region with no
    # >=5M metro). All-pairs routing is undefined there, but a per-band compose
    # downstream still expects a drawable layer, so route_layer emits an empty
    # FeatureCollection rather than raising.
    net = _build(tmp_path, monkeypatch, _CHAIN)
    res = ops.route_layer(net, json.dumps([{"lon": 0, "lat": 0, "name": "A"}]))
    assert res.point_count == 1
    assert res.route_count == 0
    assert res.reachable_count == 0
    fc = json.loads(Path(res.output_path).read_text())
    assert fc == {"type": "FeatureCollection", "features": []}
