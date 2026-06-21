"""Offline tests for osm.Change.ExtractChanges (network + base-scan seams mocked)."""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.change import change_handlers as ch
from osm_geocoder.tools._osm_tools.osm_changes import (
    ChangeObj,
    classify_changes,
    needed_way_node_ids,
)


_CHANGES = [
    ChangeObj("node", 1, 1, True, lat=1.0, lon=2.0, tags={"amenity": "cafe", "name": "New Cafe"}),
    ChangeObj("node", 2, 3, True, lat=3.0, lon=4.0, tags={"shop": "bakery"}),
    ChangeObj("node", 3, 5, False),          # deleted (no geometry/tags)
    ChangeObj("way", 10, 2, True, nodes=[100, 101, 102], tags={"highway": "residential"}),
    ChangeObj("relation", 20, 1, True),      # counted, not emitted
]

# Node-location map for the way in _CHANGES (open way -> LineString).
_WAY10_LOCS = {100: (10.0, 50.0), 101: (10.1, 50.1), 102: (10.2, 50.2)}


def test_classify_buckets_and_counts():
    out = classify_changes(_CHANGES, _WAY10_LOCS)
    c = out["counts"]
    # 1 added node, 1 modified node + 1 modified way, 1 deleted node
    assert (c["added"], c["modified"], c["deleted"]) == (1, 2, 1)
    assert c["ways_changed"] == 1
    assert (c["ways_added"], c["ways_modified"], c["ways_deleted"]) == (0, 1, 0)
    assert c["relations_changed"] == 1

    # added: new node with full Point geometry + tags + change metadata
    feat = out["added"]["features"][0]
    assert feat["geometry"] == {"type": "Point", "coordinates": [2.0, 1.0]}
    assert feat["properties"]["amenity"] == "cafe"
    assert feat["properties"]["change_type"] == "added" and feat["properties"]["osm_id"] == 1

    # deleted: invisible -> null geometry, still identifies the osm_id
    dfeat = out["deleted"]["features"][0]
    assert dfeat["geometry"] is None and dfeat["properties"]["osm_id"] == 3


def test_classify_way_linestring():
    out = classify_changes(_CHANGES, _WAY10_LOCS)
    way = next(f for f in out["modified"]["features"]
               if f["properties"]["osm_type"] == "way")
    assert way["geometry"]["type"] == "LineString"
    assert way["geometry"]["coordinates"] == [[10.0, 50.0], [10.1, 50.1], [10.2, 50.2]]
    assert way["properties"]["osm_id"] == 10
    assert way["properties"]["highway"] == "residential"
    assert way["properties"]["change_type"] == "modified"


def test_classify_closed_ring_is_polygon():
    # first ref == last ref, >= 4 refs -> Polygon (a building outline)
    refs = [1, 2, 3, 1]
    locs = {1: (0.0, 0.0), 2: (1.0, 0.0), 3: (1.0, 1.0)}
    changes = [ChangeObj("way", 99, 1, True, nodes=refs, tags={"building": "yes"})]
    out = classify_changes(changes, locs)
    way = out["added"]["features"][0]
    assert way["geometry"]["type"] == "Polygon"
    assert way["geometry"]["coordinates"] == [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]
    assert out["counts"]["ways_added"] == 1


def test_classify_unresolvable_way_is_null_geometry_not_crash():
    # node 102 missing from the location map -> null geometry, no crash, id kept
    changes = [ChangeObj("way", 11, 2, True, nodes=[100, 101, 102], tags={"highway": "path"})]
    out = classify_changes(changes, {100: (1.0, 2.0), 101: (3.0, 4.0)})
    way = out["modified"]["features"][0]
    assert way["geometry"] is None
    assert way["properties"]["osm_id"] == 11 and way["properties"]["change_type"] == "modified"
    assert out["counts"]["ways_modified"] == 1


def test_classify_no_node_locs_yields_null_way_geometry():
    # default (no map) -> ways still classified/counted but null geometry
    out = classify_changes(_CHANGES)
    way = next(f for f in out["modified"]["features"]
               if f["properties"]["osm_type"] == "way")
    assert way["geometry"] is None
    assert out["counts"]["ways_modified"] == 1


def test_needed_way_node_ids():
    assert needed_way_node_ids(_CHANGES) == {100, 101, 102}
    # deleted ways carry no refs -> contribute nothing
    deleted_way = [ChangeObj("way", 5, 3, False)]
    assert needed_way_node_ids(deleted_way) == set()


def test_handler_writes_collections_and_returns_changeset(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_collect_changes", lambda gp, since, mb, lp: (12345, _CHANGES))
    # base-scan seam: resolve the way's referenced nodes
    monkeypatch.setattr(ch, "_resolve_way_node_locations",
                        lambda lp, needed: dict(_WAY10_LOCS))
    monkeypatch.setattr(ch, "is_region_cached", lambda r, **k: True)
    monkeypatch.setattr(
        ch, "get_storage",
        lambda: type("S", (), {"localize": staticmethod(lambda p: "/tmp/base.pbf")})(),
    )
    monkeypatch.setattr(ch, "cached_path", lambda gp: "cache/x.pbf")
    monkeypatch.setattr(ch, "resolve_output_dir", lambda category: str(tmp_path))
    monkeypatch.setattr(ch, "open_output", lambda p, mode="w": open(p, mode))

    out = ch.handle_extract_changes({
        "region": {"geofabrik_path": "europe/germany", "name": "Germany"},
        "since": "1000",   # explicit sequence -> no cache localize for baseline
    })["changes"]

    assert out["since_sequence"] == 12345
    assert (out["added_count"], out["modified_count"], out["deleted_count"]) == (1, 2, 1)
    assert out["ways_changed"] == 1 and out["ways_modified"] == 1
    assert out["relations_changed"] == 1
    # the added GeoJSON was actually written
    added = json.load(open(out["added"]))
    assert added["type"] == "FeatureCollection"
    assert added["features"][0]["properties"]["name"] == "New Cafe"
    # the changed way got LineString geometry
    modified = json.load(open(out["modified"]))
    way = next(f for f in modified["features"] if f["properties"]["osm_type"] == "way")
    assert way["geometry"]["type"] == "LineString"


def test_handler_requires_geofabrik_path():
    with pytest.raises(ValueError):
        ch.handle_extract_changes({"region": {}})


def test_handler_since_empty_needs_cached_baseline(monkeypatch):
    monkeypatch.setattr(ch, "get_storage", lambda: object())
    monkeypatch.setattr(ch, "is_region_cached", lambda r, **k: False)
    with pytest.raises(ValueError, match="needs the region cached"):
        ch.handle_extract_changes({"region": {"geofabrik_path": "europe/germany"}, "since": ""})


def test_dispatch_and_timeout0():
    from unittest.mock import MagicMock
    assert set(ch._DISPATCH) == {"osm.Change.ExtractChanges"}
    runner = MagicMock()
    ch.register_handlers(runner)
    assert runner.register_handler.call_args.kwargs["timeout_ms"] == 0
