"""Offline unit tests for the Overture Maps source adapter.

These run with NO optional dependency (pyarrow/duckdb) and NO network. The
swappable reader ``_read_overture_records`` is monkeypatched to feed synthetic
Overture-shaped records; the tests assert that:

  * the adapter emits GeoJSON staged to disk with the SAME unified result-dict
    schema as the PostGIS / GeoJSON adapters for the same category, and
  * the staged features carry the OSM-style property (``amenity``/``building``/
    ``highway`` …) that downstream analysis facets expect, and
  * with the optional dependency absent, the real read path raises
    ``OvertureDependencyError`` rather than silently returning empty.

Categories covered with full schema-parity + property-mapping assertions:
amenities, buildings, roads, parks, boundaries, population, routes, POIs.
"""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.sources import overture_source as ov
from osm_geocoder.handlers.sources.geojson_source import (
    _empty_amenity_result,
    _empty_building_result,
)

# Schemas of the unified result dicts, as produced by the PostGIS / GeoJSON
# adapters. Schema parity == identical key sets per category.
_AMENITY_KEYS = set(_empty_amenity_result({}).keys())
_BUILDING_KEYS = set(_empty_building_result({}).keys())


def _point(lon, lat):
    return {"type": "Point", "coordinates": [lon, lat]}


def _line():
    return {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}


def _poly():
    return {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}


@pytest.fixture
def source_payload(tmp_path, monkeypatch):
    """Point staged output at tmp and return a base OvertureSource payload."""
    monkeypatch.setattr(ov, "_LOCAL_OUTPUT", str(tmp_path))
    return {
        "source": {
            "theme": "places",
            "release": "2024-09-18.0",
            "min_lon": -123.0,
            "min_lat": 37.0,
            "max_lon": -122.0,
            "max_lat": 38.0,
            "region": "San Francisco",
        }
    }


def _feed(monkeypatch, records):
    """Monkeypatch the swappable reader to yield the given synthetic records."""
    monkeypatch.setattr(
        ov, "_read_overture_records", lambda source, theme, type_filter, bbox: iter(records)
    )


def _read_fc(path):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Amenities — schema parity with the other adapters + property mapping
# ---------------------------------------------------------------------------

def test_amenities_schema_parity_and_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _point(-122.4, 37.7),
         "properties": {"category": "eat_and_drink.restaurant", "name": "Synth Diner"}},
        {"geometry": _point(-122.41, 37.71),
         "properties": {"category": "eat_and_drink.cafe", "name": "Synth Cafe"}},
        {"geometry": _point(-122.42, 37.72),
         "properties": {"category": "education.school", "name": "Synth School"}},
        {"geometry": _point(-122.43, 37.73),
         "properties": {"name": "No Category"}},  # dropped: no amenity resolvable
    ]
    _feed(monkeypatch, records)

    payload = {**source_payload, "category": "food"}
    rv = ov._extract_amenities(payload)

    # Unified result-dict schema must match the GeoJSON/PostGIS adapter exactly.
    assert set(rv["result"].keys()) == _AMENITY_KEYS
    assert rv["result"]["amenity_category"] == "food"
    assert rv["result"]["format"] == "GeoJSON"

    # category="food" keeps restaurant + cafe, drops school + uncategorized.
    assert rv["result"]["feature_count"] == 2
    fc = _read_fc(rv["result"]["output_path"])
    assert fc["type"] == "FeatureCollection"
    amenities = sorted(f["properties"]["amenity"] for f in fc["features"])
    assert amenities == ["cafe", "restaurant"]
    # Each staged feature is a real GeoJSON Feature with geometry + amenity prop.
    for feat in fc["features"]:
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "Point"
        assert "amenity" in feat["properties"]


def test_amenities_all_category(monkeypatch, source_payload):
    records = [
        {"geometry": _point(0, 0), "properties": {"category": "x.restaurant"}},
        {"geometry": _point(1, 1), "properties": {"amenity": "bench"}},
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_amenities({**source_payload, "category": "all"})
    assert rv["result"]["feature_count"] == 2  # no filtering when category=all


# ---------------------------------------------------------------------------
# Buildings — schema parity + property mapping
# ---------------------------------------------------------------------------

def test_buildings_schema_parity_and_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _poly(), "properties": {"subtype": "residential"}},
        {"geometry": _poly(), "properties": {"subtype": "commercial"}},
        {"geometry": _poly(), "properties": {"class": "industrial"}},
    ]
    _feed(monkeypatch, records)

    rv = ov._extract_buildings({**source_payload, "building_type": "residential"})

    assert set(rv["result"].keys()) == _BUILDING_KEYS
    assert rv["result"]["building_type"] == "residential"
    assert rv["result"]["feature_count"] == 1
    fc = _read_fc(rv["result"]["output_path"])
    assert fc["features"][0]["properties"]["building"] == "residential"
    assert fc["features"][0]["geometry"]["type"] == "Polygon"


def test_buildings_all_keeps_everything(monkeypatch, source_payload):
    records = [
        {"geometry": _poly(), "properties": {"subtype": "residential"}},
        {"geometry": _poly(), "properties": {"class": "shed"}},
        {"geometry": _poly(), "properties": {}},  # -> building=yes
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_buildings({**source_payload, "building_type": "all"})
    assert rv["result"]["feature_count"] == 3
    fc = _read_fc(rv["result"]["output_path"])
    assert {f["properties"]["building"] for f in fc["features"]} == {
        "residential",
        "shed",
        "yes",
    }


# ---------------------------------------------------------------------------
# Roads / Parks / Boundaries / Population / Routes / POIs — parity + mapping
# ---------------------------------------------------------------------------

def test_roads_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _line(), "properties": {"class": "motorway"}},
        {"geometry": _line(), "properties": {"class": "residential"}},
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_roads({**source_payload, "road_class": "motorway"})
    assert set(rv["result"].keys()) == {
        "output_path", "feature_count", "road_class", "total_length_km",
        "with_speed_limit", "format", "extraction_date",
    }
    assert rv["result"]["feature_count"] == 1
    fc = _read_fc(rv["result"]["output_path"])
    assert fc["features"][0]["properties"]["highway"] == "motorway"


def test_parks_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _poly(), "properties": {"subtype": "national_park"}},
        {"geometry": _poly(), "properties": {"subtype": "park"}},
        {"geometry": _poly(), "properties": {"subtype": "industrial"}},  # dropped
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_parks({**source_payload, "park_type": "all"})
    assert set(rv["result"].keys()) == {
        "output_path", "feature_count", "park_type", "protect_classes",
        "total_area_km2", "format", "extraction_date",
    }
    assert rv["result"]["feature_count"] == 2
    fc = _read_fc(rv["result"]["output_path"])
    assert all(f["properties"].get("leisure") == "park" for f in fc["features"])


def test_boundaries_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _poly(), "properties": {"subtype": "country"}},
        {"geometry": _poly(), "properties": {"subtype": "region"}},  # admin_level 4
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_boundaries({**source_payload, "boundary_type": "country"})
    assert set(rv["result"].keys()) == {
        "output_path", "feature_count", "boundary_type", "admin_levels",
        "format", "extraction_date",
    }
    assert rv["result"]["feature_count"] == 1  # only country (admin_level 2)
    fc = _read_fc(rv["result"]["output_path"])
    assert fc["features"][0]["properties"]["admin_level"] == "2"
    assert fc["features"][0]["properties"]["boundary"] == "administrative"


def test_population_mapping_and_filter(monkeypatch, source_payload):
    records = [
        {"geometry": _point(0, 0), "properties": {"subtype": "locality", "population": 100000}},
        {"geometry": _point(1, 1), "properties": {"subtype": "locality", "population": 5000}},
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_population(
        {**source_payload, "place_type": "all", "min_population": 50000}
    )
    assert set(rv["result"].keys()) == {
        "output_path", "feature_count", "original_count", "place_type",
        "min_population", "max_population", "filter_applied", "format",
        "extraction_date",
    }
    assert rv["result"]["feature_count"] == 1
    assert rv["result"]["filter_applied"] == "population >= 50000"


def test_routes_mapping(monkeypatch, source_payload):
    records = [
        {"geometry": _line(), "properties": {"class": "cycleway"}},
        {"geometry": _line(), "properties": {"class": "motorway"}},
    ]
    _feed(monkeypatch, records)
    rv = ov._extract_routes({**source_payload, "route_type": "bicycle"})
    assert set(rv["result"].keys()) == {
        "output_path", "feature_count", "route_type", "network_level",
        "include_infrastructure", "format", "extraction_date",
    }
    assert rv["result"]["feature_count"] == 1


def test_pois_returns_oscmache_shape(monkeypatch, source_payload):
    records = [{"geometry": _point(0, 0), "properties": {"category": "x.restaurant"}}]
    _feed(monkeypatch, records)
    rv = ov._extract_pois(source_payload)
    assert set(rv["pois"].keys()) == {"url", "path", "date", "size", "wasInCache"}
    assert rv["pois"]["path"]
    assert rv["pois"]["size"] > 0


# ---------------------------------------------------------------------------
# Dispatch wiring
# ---------------------------------------------------------------------------

def test_dispatch_keys_match_other_adapters():
    from osm_geocoder.handlers.sources.postgis_source import POSTGIS_DISPATCH

    ov_suffixes = {k.split(".")[-1] for k in ov.OVERTURE_DISPATCH}
    pg_suffixes = {k.split(".")[-1] for k in POSTGIS_DISPATCH}
    assert ov_suffixes == pg_suffixes  # same set of Extract* facets
    assert all(k.startswith("osm.Source.Overture.") for k in ov.OVERTURE_DISPATCH)


def test_handle_dispatches_by_facet_name(monkeypatch, source_payload):
    _feed(monkeypatch, [{"geometry": _point(0, 0), "properties": {"category": "x.restaurant"}}])
    payload = {**source_payload, "_facet_name": "osm.Source.Overture.ExtractAmenities",
               "category": "all"}
    rv = ov.handle(payload)
    assert "result" in rv


def test_handle_unknown_facet_raises():
    with pytest.raises(ValueError, match="Unknown Overture source facet"):
        ov.handle({"_facet_name": "osm.Source.Overture.Nope"})


# ---------------------------------------------------------------------------
# Missing-dependency contract — must RAISE, never silent-empty
# ---------------------------------------------------------------------------

def test_missing_dep_raises_not_silent_empty(monkeypatch, source_payload):
    # Force the "no optional backend installed" condition.
    monkeypatch.setattr(ov, "_has_reader_dep", lambda: False)
    with pytest.raises(ov.OvertureDependencyError):
        # Drive through the real reader seam (NOT the monkeypatched _feed).
        list(ov._read_overture_records(
            ov._source_from_payload(source_payload), "places", "place", (-1, -1, 1, 1)
        ))


def test_missing_dep_propagates_through_extractor(monkeypatch, source_payload):
    monkeypatch.setattr(ov, "_has_reader_dep", lambda: False)
    with pytest.raises(ov.OvertureDependencyError):
        ov._extract_amenities({**source_payload, "category": "all"})
