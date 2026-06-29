"""Deterministic tests for the contains / regex GeoJSON tag filters."""

from __future__ import annotations

import json

from osm_geocoder.handlers.filters.osm_type_filter import (
    filter_geojson_by_tag_contains,
    filter_geojson_by_tag_regex,
)


def _write(path, *names_key_vals):
    """Write a point FeatureCollection from (tag_key, value) property dicts."""
    feats = [
        {
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [i, 0]},
        }
        for i, props in enumerate(names_key_vals)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(path)


def _values(path, key):
    with open(path) as f:
        return [feat["properties"].get(key) for feat in json.load(f)["features"]]


def test_contains_case_insensitive_by_default(tmp_path):
    src = _write(
        tmp_path / "in.geojson",
        {"name": "Starbucks Coffee"},
        {"name": "Blue Bottle"},
        {"name": "STARBUCKS Reserve"},
    )
    res = filter_geojson_by_tag_contains(
        src, "name", "starbucks", output_path=str(tmp_path / "o.geojson")
    )
    assert res.feature_count == 2
    assert set(_values(res.output_path, "name")) == {"Starbucks Coffee", "STARBUCKS Reserve"}


def test_contains_case_sensitive(tmp_path):
    src = _write(
        tmp_path / "in.geojson", {"name": "Starbucks Coffee"}, {"name": "STARBUCKS Reserve"}
    )
    res = filter_geojson_by_tag_contains(
        src, "name", "Starbucks", case_sensitive=True, output_path=str(tmp_path / "o.geojson")
    )
    assert res.feature_count == 1
    assert _values(res.output_path, "name") == ["Starbucks Coffee"]


def test_regex_alternation(tmp_path):
    src = _write(
        tmp_path / "in.geojson",
        {"cuisine": "pizza"},
        {"cuisine": "italian_pizza"},
        {"cuisine": "sushi"},
    )
    res = filter_geojson_by_tag_regex(
        src, "cuisine", "pizza|italian", output_path=str(tmp_path / "o.geojson")
    )
    assert res.feature_count == 2
    assert set(_values(res.output_path, "cuisine")) == {"pizza", "italian_pizza"}


def test_regex_anchored(tmp_path):
    src = _write(tmp_path / "in.geojson", {"ref": "I 5"}, {"ref": "US 101"}, {"ref": "I 80"})
    res = filter_geojson_by_tag_regex(
        src, "ref", r"^I \d+$", output_path=str(tmp_path / "o.geojson")
    )
    assert set(_values(res.output_path, "ref")) == {"I 5", "I 80"}


def test_filters_skip_non_string_and_missing(tmp_path):
    src = _write(tmp_path / "in.geojson", {"name": "Cafe"}, {"other": "x"}, {"name": 42})
    res = filter_geojson_by_tag_contains(
        src, "name", "cafe", output_path=str(tmp_path / "o.geojson")
    )
    assert res.feature_count == 1  # missing key + non-string value are skipped
    assert res.original_count == 3
