"""Tests for the GraphHopper routing adapter (HTTP layer stubbed)."""

from __future__ import annotations

import json

from osm_geocoder.handlers.routing import graphhopper_router as G


def test_profile_map():
    assert G._profile("bicycle") == "bike"
    assert G._profile("walking") == "foot"
    assert G._profile("car") == "car"


def test_route_marshals_and_builds_params(monkeypatch):
    captured = {}

    def fake_get(endpoint, params, **k):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return {"paths": [{"distance": 18100, "time": 1188000,
                           "points": {"type": "LineString",
                                      "coordinates": [[-122.4194, 37.7749], [-122.2712, 37.8044]]}}]}

    monkeypatch.setattr(G, "_gh_get", fake_get)
    rv = G.handle({"_facet_name": "osm.Routing.GraphHopper.Route",
                   "from_lat": 37.7749, "from_lon": -122.4194,
                   "to_lat": 37.8044, "to_lon": -122.2712, "profile": "bike"})["result"]
    assert rv["route"]["backend"] == "graphhopper"
    assert rv["route"]["distance_km"] == 18.1        # m -> km
    assert rv["route"]["duration_min"] == 19.8       # ms -> min
    # GraphHopper wants point=lat,lon (lat first) and the mapped profile.
    assert ("point", "37.7749,-122.4194") in captured["params"]
    assert ("profile", "bike") in captured["params"]
    assert ("points_encoded", "false") in captured["params"]


def test_route_fallback(monkeypatch):
    monkeypatch.setattr(G, "_gh_get", lambda *a, **k: None)
    rv = G.handle({"_facet_name": "osm.Routing.GraphHopper.Route",
                   "from_lat": 37.77, "from_lon": -122.42,
                   "to_lat": 37.80, "to_lon": -122.27})["result"]
    assert rv["route"]["backend"] == "estimate"
    assert rv["route"]["distance_km"] > 0


def test_isochrone_marshals(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(G, "_gh_get", lambda *a, **k: {
        "polygons": [{"type": "Feature",
                      "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}]})
    rv = G.handle({"_facet_name": "osm.Routing.GraphHopper.Isochrone",
                   "center_lat": 37.77, "center_lon": -122.42, "time_minutes": 15})["result"]
    assert rv["backend"] == "graphhopper"
    feats = json.load(open(rv["output_path"]))["features"]
    assert len(feats) == 1 and feats[0]["geometry"]["type"] == "Polygon"


def test_isochrone_no_server(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(G, "_gh_get", lambda *a, **k: None)
    rv = G.handle({"_facet_name": "osm.Routing.GraphHopper.Isochrone",
                   "center_lat": 37.77, "center_lon": -122.42})["result"]
    assert rv["backend"] == "none"
    assert json.load(open(rv["output_path"]))["features"] == []
