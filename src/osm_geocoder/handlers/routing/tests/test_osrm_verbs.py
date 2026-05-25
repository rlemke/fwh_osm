"""Tests for the OSRM Matrix / Nearest / MapMatch verbs.

The OSRM HTTP call (`_osrm_request`) is stubbed, so these check our parsing,
marshalling, and the graceful great-circle fallback when OSRM is unavailable —
the live OSRM run covers the real engine.
"""

from __future__ import annotations

import json

from osm_geocoder.handlers.routing import osrm_router as R


def _no_osrm(monkeypatch):
    monkeypatch.setattr(R, "_osrm_request", lambda *a, **k: None)


# --- parsing -------------------------------------------------------------------


def test_parse_points_json_and_string():
    assert R._parse_points('[{"lon":1,"lat":2,"name":"a"}]') == [{"lon": 1, "lat": 2, "name": "a"}]
    pts = R._parse_points("1,2;3,4")
    assert [(p["lon"], p["lat"]) for p in pts] == [(1.0, 2.0), (3.0, 4.0)]


# --- Matrix --------------------------------------------------------------------


def test_matrix_from_osrm(monkeypatch):
    monkeypatch.setattr(R, "_osrm_request", lambda *a, **k: {
        "code": "Ok", "durations": [[0, 600], [600, 0]], "distances": [[0, 12000], [12000, 0]]})
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Matrix",
                   "points": "-122.4,37.7;-122.2,37.8"})["result"]
    assert rv["point_count"] == 2
    assert rv["backend"] == "osrm-local"
    assert json.loads(rv["durations"]) == [[0, 600], [600, 0]]


def test_matrix_fallback_is_full_nxn(monkeypatch):
    _no_osrm(monkeypatch)
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Matrix",
                   "points": "-122.42,37.77;-122.27,37.80;-121.89,37.34"})["result"]
    assert rv["backend"] == "estimate"
    d = json.loads(rv["durations"])
    assert len(d) == 3 and all(len(row) == 3 for row in d)
    assert d[0][0] == 0.0   # distance to self is zero


def test_matrix_too_few_points():
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Matrix", "points": "-122.4,37.7"})["result"]
    assert rv["point_count"] == 0 and rv["backend"] == "none"


# --- Nearest -------------------------------------------------------------------


def test_nearest_from_osrm(monkeypatch):
    monkeypatch.setattr(R, "_osrm_request", lambda *a, **k: {
        "code": "Ok", "waypoints": [{"location": [-122.41945, 37.77494],
                                       "distance": 6.0, "name": "Market Street"}]})
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Nearest", "lat": 37.7749, "lon": -122.4194})["result"]
    assert rv["name"] == "Market Street"
    assert rv["distance_m"] == 6.0
    assert rv["snapped_lat"] == 37.77494 and rv["backend"] == "osrm-local"


def test_nearest_fallback_echoes_input(monkeypatch):
    _no_osrm(monkeypatch)
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Nearest", "lat": 37.7, "lon": -122.4})["result"]
    assert rv["snapped_lat"] == 37.7 and rv["snapped_lon"] == -122.4
    assert rv["distance_m"] == 0.0 and rv["backend"] == "estimate"


# --- MapMatch ------------------------------------------------------------------


def test_map_match_from_osrm(monkeypatch):
    monkeypatch.setattr(R, "_osrm_request", lambda *a, **k: {
        "code": "Ok", "matchings": [{"confidence": 0.97, "distance": 1500,
                                       "geometry": {"coordinates": [[-122.4, 37.7], [-122.39, 37.71]]}}]})
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.MapMatch",
                   "trace": "-122.4,37.7;-122.39,37.71"})["result"]
    assert rv["confidence"] == 0.97
    assert rv["distance_km"] == 1.5
    assert rv["matched_points"] == 2 and rv["backend"] == "osrm-local"


def test_map_match_fallback(monkeypatch):
    _no_osrm(monkeypatch)
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.MapMatch",
                   "trace": "-122.4,37.7;-122.39,37.71"})["result"]
    assert rv["backend"] == "estimate"
    assert rv["confidence"] == 0.0 and rv["matched_points"] == 2


def test_map_match_too_few_points():
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.MapMatch", "trace": "-122.4,37.7"})["result"]
    assert rv["matched_points"] == 0 and rv["backend"] == "none"


# --- Trip (TSP) ----------------------------------------------------------------


def test_trip_optimizes_order(monkeypatch):
    # 3 inputs; OSRM reorders them: input 0->tour pos 0, input 1->pos 2, input 2->pos 1.
    monkeypatch.setattr(R, "_osrm_request", lambda *a, **k: {
        "code": "Ok",
        "trips": [{"distance": 30000, "duration": 1800,
                   "geometry": {"coordinates": [[-122.4, 37.7], [-122.0, 37.4]]}}],
        "waypoints": [{"waypoint_index": 0}, {"waypoint_index": 2}, {"waypoint_index": 1}],
    })
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Trip",
                   "waypoints": '[{"lon":-122.4,"lat":37.7,"name":"A"},'
                                '{"lon":-122.0,"lat":37.4,"name":"B"},'
                                '{"lon":-122.2,"lat":37.8,"name":"C"}]'})["result"]
    assert rv["waypoint_count"] == 3
    assert rv["backend"] == "osrm-local"
    assert json.loads(rv["order"]) == [0, 2, 1]          # by waypoint_index
    assert json.loads(rv["ordered_names"]) == ["A", "C", "B"]
    assert rv["total_distance_km"] == 30.0


def test_trip_fallback_keeps_input_order(monkeypatch):
    _no_osrm(monkeypatch)
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Trip",
                   "waypoints": "-122.4,37.7;-122.0,37.4;-122.2,37.8"})["result"]
    assert rv["backend"] == "estimate"
    assert json.loads(rv["order"]) == [0, 1, 2]
    assert rv["total_distance_km"] > 0


def test_trip_too_few_points():
    rv = R.handle({"_facet_name": "osm.Routing.OSRM.Trip", "waypoints": "-122.4,37.7"})["result"]
    assert rv["waypoint_count"] == 0 and rv["backend"] == "none"
