"""Offline unit tests for the ohsome-planet (OSM history) source adapter.

These run with NO optional dependency (pyarrow/shapely) and NO dataset. The
swappable reader ``_read_ohsome_records`` is monkeypatched with synthetic
ohsome-shaped contribution rows, so the schema mapping, category filtering,
temporal identity and result-dict shape are all verified without touching
Parquet.

What matters here beyond parity with the other adapters:

  * OSM ``tags`` map straight through to properties, so a downstream facet
    filtering on ``amenity``/``highway`` sees what the PBF adapter would give it;
  * contribution METADATA cannot shadow a tag;
  * a time-travelled read is a DIFFERENT answer, not a display option — it must
    not collide with the ``latest`` cache entry;
  * with the dependency absent the real path raises rather than returning empty.
"""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.sources import ohsome_source as oh


def _row(**over):
    """A synthetic ohsome-planet contribution row, shaped like contrib.avsc."""
    row = {
        "status": "latest",
        "valid_from": "2024-01-01T00:00:00+00:00",
        "valid_to": None,
        "osm_type": "node",
        "osm_id": 1,
        "osm_version": 3,
        "contrib_type": "CREATION",
        "user": {"id": 7, "name": "mapper"},
        "changeset": {"id": 99, "editor": "iD", "hashtags": ["#hot"]},
        "tags": {"amenity": "cafe", "name": "Kaffee"},
        "area": 0.0,
        "length": 0.0,
        "countries": ["DEU"],
    }
    row.update(over)
    return row


def _rec(**over):
    return {"geometry": {"type": "Point", "coordinates": [8.7, 49.4]},
            "properties": oh._row_properties(_row(**over))}


def _feed(monkeypatch, records):
    monkeypatch.setattr(oh, "_read_ohsome_records", lambda source, tag_filter=None: iter(records))


def _src(tmp_path, **over):
    src = {"dataset": str(tmp_path / "ds"), "region": "heidelberg",
           "min_lon": 8.0, "min_lat": 49.0, "max_lon": 9.0, "max_lat": 50.0}
    src.update(over)
    return src


def _fc(path):
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Property mapping — the reason this adapter maps more faithfully than Overture
# ---------------------------------------------------------------------------


def test_osm_tags_pass_through_as_properties():
    props = oh._row_properties(_row())
    # The tags ARE the properties — no vocabulary translation, unlike Overture.
    assert props["amenity"] == "cafe"
    assert props["name"] == "Kaffee"
    # ...alongside contribution metadata under reserved prefixes.
    assert props["osm_type"] == "node"
    assert props["osm_user"] == "mapper"
    assert props["changeset_editor"] == "iD"
    assert props["osm_contrib_type"] == "CREATION"
    assert props["osm_countries"] == "DEU"


def test_metadata_cannot_shadow_a_real_tag():
    """A feature tagged `name=...` must keep it; metadata is namespaced."""
    props = oh._row_properties(_row(tags={"name": "Real Name", "amenity": "bar"}))
    assert props["name"] == "Real Name"
    assert all(k.startswith(("osm_", "changeset_")) or k in ("name", "amenity") for k in props)


def test_none_valued_metadata_is_dropped_not_stringified():
    """valid_to is null for a live version — it must not become the string 'None'."""
    props = oh._row_properties(_row(valid_to=None))
    assert "osm_valid_to" not in props


# ---------------------------------------------------------------------------
# Category extraction — parity with the other adapters
# ---------------------------------------------------------------------------


def test_extract_amenities_filters_and_reports(monkeypatch, tmp_path):
    _feed(monkeypatch, [
        _rec(tags={"amenity": "cafe"}),
        _rec(tags={"amenity": "hospital"}),
        _rec(tags={"highway": "primary"}),   # no amenity -> dropped
    ])
    out = oh._extract_amenities({"source": _src(tmp_path), "category": "food"})["result"]
    assert out["feature_count"] == 1                      # only the cafe is in `food`
    assert out["amenity_category"] == "food"
    assert out["format"] == "GeoJSON"
    fc = _fc(out["output_path"])
    assert fc["features"][0]["properties"]["amenity"] == "cafe"


def test_extract_roads_reports_length_from_ohsome_metric(monkeypatch, tmp_path):
    """Length comes from the row's own `length`, not a recomputation."""
    _feed(monkeypatch, [
        _rec(tags={"highway": "motorway"}, length=1500.0),
        _rec(tags={"highway": "motorway"}, length=500.0),
        _rec(tags={"highway": "residential"}, length=9999.0),   # wrong class
    ])
    out = oh._extract_roads({"source": _src(tmp_path), "road_class": "motorway"})["result"]
    assert out["feature_count"] == 2
    assert out["total_length_km"] == 2.0


def test_all_snapshot_facets_share_the_unified_shape(monkeypatch, tmp_path):
    _feed(monkeypatch, [_rec()])
    for fn in (oh._extract_amenities, oh._extract_buildings, oh._extract_roads,
               oh._extract_parks, oh._extract_boundaries, oh._extract_population,
               oh._extract_routes, oh._extract_pois):
        res = fn({"source": _src(tmp_path)})["result"]
        for key in ("output_path", "feature_count", "format", "extraction_date"):
            assert key in res, f"{fn.__name__} missing {key}"
        assert res["format"] == "GeoJSON"


# ---------------------------------------------------------------------------
# Time — the capability no other source has
# ---------------------------------------------------------------------------


def test_as_of_is_part_of_the_answers_identity(monkeypatch, tmp_path):
    """A historical read must not collide with the `latest` one.

    Both are 'amenities in Heidelberg'. If `as_of` were not in the cache key and
    the output path, the second read would serve the first one's file — the same
    question answered for the wrong year, silently.
    """
    _feed(monkeypatch, [_rec()])
    now = oh._extract_amenities({"source": _src(tmp_path)})["result"]
    past = oh._extract_amenities(
        {"source": _src(tmp_path, as_of="2019-06-01T00:00:00Z")}
    )["result"]
    assert now["output_path"] != past["output_path"]
    assert past["as_of"] == "2019-06-01T00:00:00Z"
    assert now["as_of"] == ""


def test_extract_changes_counts_by_contrib_type(monkeypatch, tmp_path):
    _feed(monkeypatch, [
        _rec(contrib_type="CREATION"),
        _rec(contrib_type="DELETION"),
        _rec(contrib_type="TAG"),
        _rec(contrib_type="TAG", changeset={"id": 5, "editor": "JOSM", "hashtags": []}),
    ])
    out = oh._extract_changes({
        "source": _src(tmp_path, since="2024-01-01T00:00:00Z", until="2024-02-01T00:00:00Z"),
    })["result"]
    assert out["feature_count"] == 4
    assert out["creations"] == 1 and out["deletions"] == 1 and out["tag_changes"] == 2
    assert out["distinct_editors"] == 2          # iD + JOSM, actually counted
    assert out["distinct_users"] == 1


def test_extract_changes_refuses_an_unbounded_window(monkeypatch, tmp_path):
    _feed(monkeypatch, [_rec()])
    with pytest.raises(ValueError, match="since"):
        oh._extract_changes({"source": _src(tmp_path)})


def test_extract_changes_rejects_a_typod_contrib_type(monkeypatch, tmp_path):
    """A typo must fail, not filter everything out and read as 'no edits'."""
    _feed(monkeypatch, [_rec()])
    with pytest.raises(ValueError, match="unknown contrib_types"):
        oh._extract_changes({
            "source": _src(tmp_path, since="2024-01-01T00:00:00Z"),
            "contrib_types": "CREATION,DELETEION",
        })


# ---------------------------------------------------------------------------
# Missing dependency must be loud
# ---------------------------------------------------------------------------


def test_missing_dependency_raises_rather_than_returning_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(oh, "_has_reader_dep", lambda: False)
    with pytest.raises(oh.OhsomeDependencyError, match="no features found"):
        list(oh._read_ohsome_records(_src(tmp_path)))


def test_dispatch_covers_every_declared_facet():
    assert set(oh.OHSOME_DISPATCH) == {
        f"{oh.NAMESPACE}.{n}" for n in (
            "ExtractRoutes", "ExtractAmenities", "ExtractRoads", "ExtractParks",
            "ExtractBuildings", "ExtractBoundaries", "ExtractPopulation",
            "ExtractPOIs", "ExtractChanges",
        )
    }
    with pytest.raises(ValueError, match="Unknown"):
        oh.handle({"_facet_name": "osm.Source.OhsomePlanet.Nope"})
