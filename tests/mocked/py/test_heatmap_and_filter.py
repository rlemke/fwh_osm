"""Tests for the shared geojson_filter + heatmap libraries (and the facets that
delegate to them)."""
import json
import os
import tempfile

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src" / "osm_geocoder" / "tools"))

from _osm_tools import geojson_filter as gf
from _osm_tools import heatmap as hm


def _pt(lon, lat, **props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props}


@pytest.fixture
def chargers(tmp_path):
    feats = [
        _pt(-122.4, 37.7, amenity="charging_station", operator="Tesla, Inc."),
        _pt(-118.2, 34.0, amenity="charging_station", brand="Tesla"),
        _pt(-71.0, 42.3, amenity="charging_station", **{"socket:tesla_supercharger": "yes"}),
        _pt(-87.6, 41.8, amenity="charging_station", operator="ChargePoint"),
        _pt(-95.3, 29.7, amenity="restaurant"),
    ]
    p = tmp_path / "chargers.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(p)


def test_filter_by_expression(chargers, tmp_path):
    expr = ('props.get("amenity")=="charging_station" and ("tesla" in str(props.get("operator","")).lower()'
            ' or "tesla" in str(props.get("brand","")).lower() or any(k.startswith("socket:tesla") for k in props))')
    r = gf.filter_geojson(chargers, expr, str(tmp_path / "out.geojson"))
    assert (r.total, r.kept, r.errors) == (5, 3, 0)


def test_filter_by_def_keep(chargers, tmp_path):
    script = ("def keep(feature):\n"
              "    p = feature['properties']\n"
              "    if p.get('amenity') != 'charging_station': return False\n"
              "    return 'tesla' in ' '.join(str(k)+' '+str(v) for k,v in p.items()).lower()\n")
    r = gf.filter_geojson(chargers, script, str(tmp_path / "out2.geojson"))
    assert r.kept == 3


def test_filter_bad_script_raises():
    with pytest.raises(gf.FilterError):
        gf.compile_filter("not valid python !!!")


def test_filter_sandbox_blocks_import():
    # __import__ is not in the safe builtins -> predicate errors -> feature dropped
    pred = gf.compile_filter("__import__('os').system('true') or True")
    with pytest.raises(Exception):
        pred({"properties": {}})


def test_heatmap_kernel(chargers, tmp_path):
    out = str(tmp_path / "heat.html")
    r = hm.render_heatmap(chargers, out, title="T", style="kernel")
    html = Path(out).read_text()
    assert r.point_count == 5 and r.style == "kernel"
    assert "maplibre-gl" in html and "heatmap" in html


def test_heatmap_grid(chargers, tmp_path):
    out = str(tmp_path / "grid.html")
    r = hm.render_heatmap(chargers, out, style="grid", cell_km=500)
    assert r.style == "grid" and r.point_count >= 1
    assert "'count'" in Path(out).read_text()


def test_heatmap_empty_raises(tmp_path):
    p = tmp_path / "empty.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(hm.HeatmapError):
        hm.render_heatmap(str(p), str(tmp_path / "x.html"))


# --- #2: streaming, truncation-tolerant reader -----------------------------

def _fc(n):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-122 + i*0.01, 37 + i*0.01]},
         "properties": {"amenity": "charging_station", "operator": "Tesla" if i % 2 else "Other", "id": i}}
        for i in range(n)]}


def test_iter_features_complete_and_pretty(tmp_path):
    import json
    p = tmp_path / "full.geojson"; p.write_text(json.dumps(_fc(50)))
    assert sum(1 for _ in gf.iter_features(str(p))) == 50
    pp = tmp_path / "pretty.geojson"; pp.write_text(json.dumps(_fc(50), indent=2))
    assert sum(1 for _ in gf.iter_features(str(pp))) == 50


def test_iter_features_tolerates_truncation(tmp_path):
    import json
    full = json.dumps(_fc(50))
    p = tmp_path / "trunc.geojson"; p.write_text(full[:-40])  # cut mid last feature
    n = sum(1 for _ in gf.iter_features(str(p)))
    assert 48 <= n < 50  # all complete features salvaged, only the partial tail dropped


def test_filter_and_heatmap_on_truncated(tmp_path):
    import json
    full = json.dumps(_fc(60))
    src = tmp_path / "t.geojson"; src.write_text(full[:-40])
    r = gf.filter_geojson(str(src), "'tesla' in str(props.get('operator','')).lower()", str(tmp_path / "o.geojson"))
    assert r.kept > 0
    h = hm.render_heatmap(str(tmp_path / "o.geojson"), str(tmp_path / "h.html"))
    assert h.point_count > 0


# --- #1: facets fail loudly on empty input (no silent empty success) -------

def test_byscript_handler_raises_on_empty_input():
    from osm_geocoder.handlers.filters.filter_handlers import _make_byscript_filter_handler
    h = _make_byscript_filter_handler("ByScript")
    with pytest.raises(ValueError, match="input_path is empty"):
        h({"input_path": "", "script": "True"})


def test_renderheatmap_handler_raises_on_empty_points():
    from osm_geocoder.handlers.visualization.visualization_handlers import _make_heatmap_handler
    h = _make_heatmap_handler("RenderHeatmap")
    with pytest.raises(ValueError, match="points is empty"):
        h({"points": "", "style": "kernel"})
