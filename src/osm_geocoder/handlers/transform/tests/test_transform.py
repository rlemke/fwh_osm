"""Deterministic tests for osm.Transform (MergeLayers / Summarize / Dissolve).

Synthetic GeoJSON with known features/geometry — no network, no PBF.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("shapely")

from shapely.geometry import shape as shp

from osm_geocoder.handlers.transform import transform_ops as ops


def _point(lon, lat, props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _square(x, y, props, side=1.0):
    s = side
    return {"type": "Feature", "properties": props, "geometry": {
        "type": "Polygon",
        "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]}}


def _write(path, features):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return str(path)


def _features(path):
    with open(path) as f:
        return json.load(f)["features"]


# --- MergeLayers ---------------------------------------------------------------


def test_merge_layers_concatenates(tmp_path):
    a = _write(tmp_path / "a.geojson", [_point(0, 0, {"id": 1}), _point(1, 1, {"id": 2})])
    b = _write(tmp_path / "b.geojson", [_point(2, 2, {"id": 3})])
    res = ops.merge_layers([a, b], output_path=str(tmp_path / "m.geojson"))
    assert res.operation == "merge"
    assert res.feature_count == 3
    assert res.group_count == 2
    assert {f["properties"]["id"] for f in _features(res.output_path)} == {1, 2, 3}


def test_merge_layers_skips_missing_inputs(tmp_path):
    a = _write(tmp_path / "a.geojson", [_point(0, 0, {"id": 1})])
    res = ops.merge_layers([a, str(tmp_path / "nope.geojson")], output_path=str(tmp_path / "m.geojson"))
    assert res.feature_count == 1
    assert res.group_count == 1   # only the existing layer counted


# --- Summarize -----------------------------------------------------------------


def test_summarize_count_grouped(tmp_path):
    src = _write(tmp_path / "s.geojson", [
        _point(0, 0, {"amenity": "cafe"}),
        _point(1, 0, {"amenity": "cafe"}),
        _point(2, 0, {"amenity": "bank"}),
    ])
    res = ops.summarize(src, group_by="amenity", op="count", output_path=str(tmp_path / "out.json"))
    assert res.operation == "summarize:count"
    assert res.feature_count == 3
    assert res.group_count == 2
    breakdown = json.load(open(res.output_path))["groups"]
    assert breakdown["cafe"]["value"] == 2
    assert breakdown["bank"]["value"] == 1


def test_summarize_plain_count_no_group(tmp_path):
    src = _write(tmp_path / "s.geojson", [_point(0, 0, {}), _point(1, 1, {})])
    res = ops.summarize(src, output_path=str(tmp_path / "out.json"))
    assert res.feature_count == 2
    assert res.group_count == 0   # no grouping requested


def test_summarize_avg_measure(tmp_path):
    src = _write(tmp_path / "s.geojson", [
        _point(0, 0, {"city": "X", "pop": 10}),
        _point(1, 0, {"city": "X", "pop": 30}),
        _point(2, 0, {"city": "Y", "pop": 5}),
    ])
    res = ops.summarize(src, group_by="city", measure="pop", op="avg",
                        output_path=str(tmp_path / "out.json"))
    groups = json.load(open(res.output_path))["groups"]
    assert groups["X"]["value"] == pytest.approx(20.0)   # (10+30)/2
    assert groups["Y"]["value"] == pytest.approx(5.0)


def test_summarize_rejects_bad_op(tmp_path):
    src = _write(tmp_path / "s.geojson", [_point(0, 0, {})])
    with pytest.raises(ValueError, match="op must be one of"):
        ops.summarize(src, op="median", output_path=str(tmp_path / "x.json"))


def test_summarize_sum_requires_measure(tmp_path):
    src = _write(tmp_path / "s.geojson", [_point(0, 0, {})])
    with pytest.raises(ValueError, match="requires a measure"):
        ops.summarize(src, op="sum", output_path=str(tmp_path / "x.json"))


# --- Dissolve ------------------------------------------------------------------


def test_dissolve_unions_by_group(tmp_path):
    # Two adjacent squares in group A (union into one polygon) + one in group B.
    src = _write(tmp_path / "d.geojson", [
        _square(0, 0, {"zone": "A"}),
        _square(1, 0, {"zone": "A"}),   # shares an edge with the first
        _square(5, 5, {"zone": "B"}),
    ])
    res = ops.dissolve(src, group_by="zone", output_path=str(tmp_path / "out.geojson"))
    assert res.operation == "dissolve"
    assert res.original_count == 3
    assert res.feature_count == 2     # one feature per group
    assert res.group_count == 2
    by_zone = {f["properties"]["zone"]: f for f in _features(res.output_path)}
    assert by_zone["A"]["properties"]["dissolved_count"] == 2
    # The two adjacent A squares union into a single 2x1 polygon (area 2).
    assert shp(by_zone["A"]["geometry"]).area == pytest.approx(2.0)
    assert shp(by_zone["B"]["geometry"]).area == pytest.approx(1.0)


def test_dissolve_requires_group_by(tmp_path):
    src = _write(tmp_path / "d.geojson", [_square(0, 0, {"zone": "A"})])
    with pytest.raises(ValueError, match="requires a group_by"):
        ops.dissolve(src, group_by="", output_path=str(tmp_path / "x.geojson"))
