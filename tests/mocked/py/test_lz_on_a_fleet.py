"""Low-zoom map pieces that only break on a multi-host fleet (2026-10-02).

- Zoom layers written to one host's local scratch could not be read by the tile
  steps other hosts claimed; a remote output_dir is now staged and uploaded.
- An empty zoom band (no motorway -> empty z2) dead-lettered the tile step and
  failed the map; it now yields no tiles, and the viewer leaves the band out.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from osm_geocoder.handlers.roads import zoom_handlers as Z
from osm_geocoder.handlers.tiles import tile_handlers as T
from osm_geocoder.handlers.visualization import visualization_handlers as V


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    for mod in (Z, T, V):
        monkeypatch.setattr(mod, "cached_result", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(mod, "save_result_meta", lambda *a, **k: None, raising=False)


def test_an_empty_band_yields_no_tiles_instead_of_failing(tmp_path):
    src = tmp_path / "roads_z2.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    rv = T.handle({"_facet_name": "osm.Tiles.BuildVectorTiles", "geojson_path": str(src),
                   "layer_name": "x_roads_z2", "min_zoom": 2})["result"]
    assert rv["output_path"] == "" and rv["format"] == "empty"


def test_the_viewer_leaves_empty_bands_out(monkeypatch):
    seen = {}

    def fake_render(tiles, layer_names=None, colors=None, **kw):
        seen.update(tiles=tiles, names=layer_names, colors=colors)
        return type("R", (), {"output_path": "/x/index.html"})()

    import osm_geocoder.handlers.visualization.map_renderer as mr
    monkeypatch.setattr(mr, "render_tiled_map", fake_render)
    monkeypatch.setattr(V, "_result_to_dict", lambda r: {"output_path": r.output_path})
    h = V._make_render_tiled_map_handler("RenderTiledMap")
    h({"tiles": ["", "b.pmtiles"], "layer_names": ["z2", "z3"], "colors": ["#1", "#2"]})
    assert seen == {"tiles": ["b.pmtiles"], "names": ["z3"], "colors": ["#2"]}
    with pytest.raises(RuntimeError, match="no roads"):
        h({"tiles": ["", ""], "layer_names": ["z2", "z3"]})


def test_remote_output_dir_is_built_locally_then_uploaded(monkeypatch, tmp_path):
    written = {}

    class Backend:
        def open(self, path, mode="r"):
            buf = io.BytesIO()
            buf.close = lambda: written.__setitem__(path, buf.getvalue())  # type: ignore[method-assign]
            return buf

    import facetwork.runtime.storage as st
    monkeypatch.setattr(st, "get_storage_backend", lambda p=None: Backend())

    def fake_build(*, output_dir, **kw):
        assert not output_dir.startswith("s3://"), "the pipeline must get a LOCAL dir"
        with open(os.path.join(output_dir, "roads_z2.geojson"), "w") as fh:
            fh.write("{}")
        return {"output_dir": output_dir, "csv_path": output_dir + "/segment_scores.csv",
                "selected_edges": 3}, {"city_count": 1}

    monkeypatch.setattr(Z, "build_zoom_layers", fake_build)
    monkeypatch.setenv("FW_LOCAL_SCRATCH", str(tmp_path))
    h = Z._make_build_zoom_layers_handler("BuildZoomLayers")
    rv = h({"cache": {}, "graph": {}, "output_dir": "s3://bucket/lz/haiti"})["result"]
    assert rv["output_dir"] == "s3://bucket/lz/haiti"
    assert rv["csv_path"] == "s3://bucket/lz/haiti/segment_scores.csv"
    assert "s3://bucket/lz/haiti/roads_z2.geojson" in written
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("zoom-layers-")], "scratch cleaned"
