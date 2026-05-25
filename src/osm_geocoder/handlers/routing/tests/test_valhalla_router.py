"""Tests for the Valhalla routing adapter (HTTP layer stubbed)."""

from __future__ import annotations

import json

from osm_geocoder.handlers.routing import valhalla_router as V


def _enc6(coords):
    """Minimal precision-6 polyline encoder (lat delta first) for round-trip tests."""
    def enc(v):
        v = ~(v << 1) if v < 0 else (v << 1)
        s = ""
        while v >= 0x20:
            s += chr((0x20 | (v & 0x1F)) + 63)
            v >>= 5
        return s + chr(v + 63)
    out, plat, plon = "", 0, 0
    for lon, lat in coords:
        la, lo = round(lat * 1e6), round(lon * 1e6)
        out += enc(la - plat) + enc(lo - plon)
        plat, plon = la, lo
    return out


def test_decode_polyline6_roundtrip():
    coords = [[-122.4194, 37.7749], [-122.2712, 37.8044], [-121.8863, 37.3382]]
    decoded = V._decode_polyline6(_enc6(coords))
    assert len(decoded) == 3
    for (lon, lat), (dlon, dlat) in zip(coords, decoded, strict=True):
        assert dlon == round(lon, 6) and dlat == round(lat, 6)


def test_costing_map():
    assert V._costing("bike") == "bicycle"
    assert V._costing("foot") == "pedestrian"
    assert V._costing("anything") == "auto"


def test_route_marshals(monkeypatch):
    shape = _enc6([[-122.4194, 37.7749], [-122.2712, 37.8044]])
    monkeypatch.setattr(V, "_valhalla_post", lambda ep, body, **k: {
        "trip": {"summary": {"length": 18.1, "time": 1188}, "legs": [{"shape": shape}]}})
    rv = V.handle({"_facet_name": "osm.Routing.Valhalla.Route",
                   "from_lat": 37.7749, "from_lon": -122.4194,
                   "to_lat": 37.8044, "to_lon": -122.2712, "profile": "car"})["result"]
    assert rv["route"]["backend"] == "valhalla"
    assert rv["route"]["distance_km"] == 18.1
    assert rv["route"]["duration_min"] == 19.8
    assert rv["waypoint_count"] == 2


def test_route_fallback(monkeypatch):
    monkeypatch.setattr(V, "_valhalla_post", lambda *a, **k: None)
    rv = V.handle({"_facet_name": "osm.Routing.Valhalla.Route",
                   "from_lat": 37.77, "from_lon": -122.42,
                   "to_lat": 37.80, "to_lon": -122.27})["result"]
    assert rv["route"]["backend"] == "estimate"
    assert rv["route"]["distance_km"] > 0


def test_matrix_marshals(monkeypatch):
    monkeypatch.setattr(V, "_valhalla_post", lambda *a, **k: {"sources_to_targets": [
        [{"time": 0, "distance": 0.0}, {"time": 600, "distance": 12.0}],
        [{"time": 600, "distance": 12.0}, {"time": 0, "distance": 0.0}]]})
    rv = V.handle({"_facet_name": "osm.Routing.Valhalla.Matrix",
                   "points": "-122.4,37.7;-122.2,37.8"})["result"]
    assert rv["backend"] == "valhalla" and rv["point_count"] == 2
    assert json.loads(rv["distances"])[0][1] == 12000.0   # km -> m


def test_matrix_fallback(monkeypatch):
    monkeypatch.setattr(V, "_valhalla_post", lambda *a, **k: None)
    rv = V.handle({"_facet_name": "osm.Routing.Valhalla.Matrix",
                   "points": "-122.4,37.7;-122.2,37.8;-121.9,37.3"})["result"]
    assert rv["backend"] == "estimate"
    d = json.loads(rv["durations"])
    assert len(d) == 3 and d[0][0] == 0.0


def test_isochrone_marshals(monkeypatch, tmp_path):
    monkeypatch.setattr(V, "_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(V, "_valhalla_post", lambda *a, **k: {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[]]},
                      "properties": {}}]})
    rv = V.handle({"_facet_name": "osm.Routing.Valhalla.Isochrone",
                   "center_lat": 37.77, "center_lon": -122.42, "time_minutes": 15})["result"]
    assert rv["backend"] == "valhalla"
    assert json.load(open(rv["output_path"]))["features"]
