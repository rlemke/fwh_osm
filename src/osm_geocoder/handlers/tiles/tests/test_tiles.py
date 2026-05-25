"""Tests for osm.Tiles.BuildVectorTiles.

The build itself shells out to tippecanoe; tests are skipped when it isn't on
PATH. The synthetic input keeps the build tiny and fast.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from osm_geocoder.handlers.tiles import tile_handlers as H

pytestmark = pytest.mark.skipif(shutil.which("tippecanoe") is None, reason="tippecanoe not installed")


def _write_points(path, n=20):
    feats = [
        {"type": "Feature", "properties": {"id": i, "amenity": "cafe"},
         "geometry": {"type": "Point", "coordinates": [-122.4 + i * 0.001, 37.7 + i * 0.001]}}
        for i in range(n)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(path)


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    monkeypatch.setattr(H, "cached_result", lambda *a, **k: None)
    monkeypatch.setattr(H, "save_result_meta", lambda *a, **k: None)


def test_build_vector_tiles_mbtiles(tmp_path, monkeypatch):
    # Force MBTiles output so the test is independent of the pmtiles CLI, and
    # write the artifact under tmp_path (not the shared cache).
    monkeypatch.setattr(H.shutil, "which", lambda name: None)  # pretend pmtiles absent
    out = tmp_path / "tiles.mbtiles"
    monkeypatch.setattr(H, "derive_output_path", lambda *a, **k: str(out))

    src = _write_points(tmp_path / "pts.geojson")
    rv = H.handle({
        "_facet_name": "osm.Tiles.BuildVectorTiles",
        "geojson_path": src, "layer_name": "cafes", "min_zoom": 0, "max_zoom": 12,
    })["result"]

    assert rv["format"] == "mbtiles"
    assert rv["layer"] == "cafes"
    assert rv["max_zoom"] == 12
    assert rv["size_bytes"] > 0
    assert out.exists()
    # An MBTiles file is a SQLite DB with a `tiles` table holding >0 tiles.
    con = sqlite3.connect(str(out))
    try:
        n_tiles = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    finally:
        con.close()
    assert n_tiles > 0


def test_build_vector_tiles_missing_input_returns_empty(monkeypatch):
    rv = H.handle({"_facet_name": "osm.Tiles.BuildVectorTiles", "geojson_path": ""})["result"]
    assert rv["output_path"] == "" and rv["size_bytes"] == 0
