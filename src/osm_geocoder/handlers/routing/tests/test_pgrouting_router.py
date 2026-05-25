"""Tests for the pgRouting routing adapter (DB layer stubbed)."""

from __future__ import annotations

import json

from osm_geocoder.handlers.routing import pgrouting_router as P


def test_speed_map():
    assert P._speed("bike") == 20
    assert P._speed("foot") == 5
    assert P._speed("anything") == 80


def test_route_from_db(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "_pg_route", lambda *a, **k: {
        "distance_km": 16.0, "coordinates": [[-122.42, 37.77], [-122.27, 37.80]]})
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Route",
                   "from_lat": 37.77, "from_lon": -122.42,
                   "to_lat": 37.80, "to_lon": -122.27, "profile": "car"})["result"]
    assert rv["route"]["backend"] == "pgrouting"
    assert rv["route"]["distance_km"] == 16.0
    assert rv["route"]["duration_min"] == 12.0       # 16 km / 80 km/h * 60
    assert rv["waypoint_count"] == 2


def test_route_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "_pg_route", lambda *a, **k: None)
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Route",
                   "from_lat": 37.77, "from_lon": -122.42,
                   "to_lat": 37.80, "to_lon": -122.27})["result"]
    assert rv["route"]["backend"] == "estimate"
    assert rv["route"]["distance_km"] > 0 and rv["route"]["duration_min"] > 0


def test_matrix_from_db(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    # _pg_matrix returns (vids, distance_matrix_meters)
    monkeypatch.setattr(P, "_pg_matrix", lambda pts, prefix: (
        [1, 2], [[0.0, 12000.0], [12000.0, 0.0]]))
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Matrix",
                   "points": "-122.4,37.7;-122.2,37.8", "profile": "car"})["result"]
    assert rv["backend"] == "pgrouting" and rv["point_count"] == 2
    assert json.loads(rv["distances"])[0][1] == 12000.0
    # 12 km / 80 km/h = 0.15 h = 540 s
    assert json.loads(rv["durations"])[0][1] == 540.0


def test_matrix_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "_pg_matrix", lambda *a, **k: None)
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Matrix",
                   "points": "-122.4,37.7;-122.2,37.8;-121.9,37.3"})["result"]
    assert rv["backend"] == "estimate"
    d = json.loads(rv["distances"])
    assert len(d) == 3 and d[0][0] == 0.0


def test_isochrone_from_db(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "_pg_isochrone", lambda *a, **k: {
        "type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]})
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Isochrone",
                   "center_lat": 37.77, "center_lon": -122.42, "time_minutes": 15})["result"]
    assert rv["backend"] == "pgrouting"
    feats = json.load(open(rv["output_path"]))["features"]
    assert len(feats) == 1 and feats[0]["geometry"]["type"] == "Polygon"


def test_isochrone_no_db(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(P, "_pg_isochrone", lambda *a, **k: None)
    rv = P.handle({"_facet_name": "osm.Routing.PgRouting.Isochrone",
                   "center_lat": 37.77, "center_lon": -122.42})["result"]
    assert rv["backend"] == "none"
    assert json.load(open(rv["output_path"]))["features"] == []


def test_edges_sql_uses_prefix():
    assert "osm_ways" in P._edges_sql("osm_")
    assert P._vertices("osm_") == "osm_ways_vertices_pgr"
