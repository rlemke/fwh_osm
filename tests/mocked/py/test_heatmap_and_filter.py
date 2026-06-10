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
