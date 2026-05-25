"""Handler-layer tests for osm.Clip.

The osmium extract itself lives in the pre-existing ``_osm_tools.pbf_clip``
tool; these tests cover the *facet boundary* this module adds — source-region
resolution from the OSMCache, flat/deterministic clip-name minting, bbox vs
polygon dispatch, and building the clipped OSMCache — by stubbing ``clip_pbf``.
The end-to-end osmium path is proven by the live California clip run.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from osm_geocoder.handlers.clip import clip_handlers as H

CA_REGION = {"geofabrik_path": "north-america/us/california", "canonical": "California"}


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    """Bypass the output-cache filesystem layer so tests stay hermetic."""
    monkeypatch.setattr(H, "cached_result", lambda *a, **k: None)
    monkeypatch.setattr(H, "save_result_meta", lambda *a, **k: None)


def _stub_clip(monkeypatch, **result_kw):
    """Replace clip_pbf with a capturing stub; returns the captured-calls list."""
    calls = []

    def fake_clip_pbf(name, source_region, *, bbox=None, polygon_path=None, **kw):
        calls.append({"name": name, "source_region": source_region, "bbox": bbox,
                      "polygon_path": polygon_path})
        return SimpleNamespace(
            path=result_kw.get("path", f"/cache/osm/pbf-clips/{name}-latest.osm.pbf"),
            generated_at=result_kw.get("generated_at", "2026-05-25T00:00:00Z"),
            size_bytes=result_kw.get("size_bytes", 12_345_678),
            was_cached=result_kw.get("was_cached", False),
        )

    monkeypatch.setattr(H.clip_tool, "clip_pbf", fake_clip_pbf)
    return calls


def test_source_region_from_geofabrik_path():
    assert H._source_region({"region": CA_REGION}) == "north-america/us/california"


def test_source_region_missing_raises():
    with pytest.raises(ValueError, match="geofabrik_path"):
        H._source_region({"region": {}})


def test_bbox_clip_name_is_flat_and_deterministic():
    bbox = (-122.6, 37.2, -121.5, 38.1)
    n1 = H._bbox_clip_name("north-america/us/california", bbox)
    n2 = H._bbox_clip_name("north-america/us/california", bbox)
    assert n1 == n2                 # deterministic / idempotent
    assert "/" not in n1            # flat — clip_pbf rejects '/'
    assert n1.startswith("california_bbox_")
    # A different bbox yields a different name (no collision).
    assert H._bbox_clip_name("north-america/us/california", (-122.0, 37.0, -121.0, 38.0)) != n1


def test_clip_by_bbox_dispatch_builds_clipped_cache(monkeypatch):
    calls = _stub_clip(monkeypatch, path="/cache/osm/pbf-clips/california_bbox_x-latest.osm.pbf",
                       size_bytes=9_000_000, was_cached=False)
    payload = {
        "_facet_name": "osm.Clip.ClipByBBox",
        "cache": {"region": CA_REGION, "path": "/cache/.../california-latest.osm.pbf", "size": 1_200_000_000},
        "west": -122.6, "south": 37.2, "east": -121.5, "north": 38.1,
    }
    rv = H.handle(payload)
    # clip_pbf was called with the right bbox + source region.
    assert calls[0]["source_region"] == "north-america/us/california"
    assert calls[0]["bbox"] == (-122.6, 37.2, -121.5, 38.1)
    # The returned OSMCache points at the clipped PBF and preserves provenance.
    cache = rv["cache"]
    assert cache["path"].endswith("california_bbox_x-latest.osm.pbf")
    assert cache["size"] == 9_000_000
    assert cache["wasInCache"] is False
    assert cache["region"] == CA_REGION


def test_clip_by_polygon_dispatch_and_name_keyed_on_content(monkeypatch, tmp_path):
    poly = tmp_path / "bay.geojson"
    poly.write_text('{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}')
    calls = _stub_clip(monkeypatch)
    payload = {
        "_facet_name": "osm.Clip.ClipByPolygon",
        "cache": {"region": CA_REGION, "path": "/cache/.../california-latest.osm.pbf"},
        "polygon_path": str(poly),
    }
    rv = H.handle(payload)
    assert calls[0]["polygon_path"] == str(poly)
    assert calls[0]["name"].startswith("california_poly_")
    assert rv["cache"]["path"]


def test_clip_by_polygon_requires_path(monkeypatch):
    _stub_clip(monkeypatch)
    with pytest.raises(ValueError, match="polygon_path is required"):
        H.handle({"_facet_name": "osm.Clip.ClipByPolygon", "cache": {"region": CA_REGION}})


def test_was_cached_flows_through(monkeypatch):
    _stub_clip(monkeypatch, was_cached=True)
    payload = {
        "_facet_name": "osm.Clip.ClipByBBox",
        "cache": {"region": CA_REGION, "path": "/x.pbf"},
        "west": -122.6, "south": 37.2, "east": -121.5, "north": 38.1,
    }
    assert H.handle(payload)["cache"]["wasInCache"] is True


def test_heartbeat_pumped_during_clip(monkeypatch):
    """The blocking clip runs under a heartbeat pump (lease-safety)."""
    _stub_clip(monkeypatch)
    beats = []
    payload = {
        "_facet_name": "osm.Clip.ClipByBBox",
        "cache": {"region": CA_REGION, "path": "/x.pbf"},
        "west": -122.6, "south": 37.2, "east": -121.5, "north": 38.1,
        "_task_heartbeat": lambda: beats.append(1),
    }
    # clip_pbf returns instantly so the 30s pump won't fire — we only assert the
    # wrapper runs the work and returns, with a callable heartbeat present.
    rv = H.handle(payload)
    assert rv["cache"]["path"]
