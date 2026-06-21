"""Offline tests for osm.Change.ExtractChanges (network seam mocked)."""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.change import change_handlers as ch
from osm_geocoder.tools._osm_tools.osm_changes import ChangeObj, classify_changes


_CHANGES = [
    ChangeObj("node", 1, 1, True, lat=1.0, lon=2.0, tags={"amenity": "cafe", "name": "New Cafe"}),
    ChangeObj("node", 2, 3, True, lat=3.0, lon=4.0, tags={"shop": "bakery"}),
    ChangeObj("node", 3, 5, False),          # deleted (no geometry/tags)
    ChangeObj("way", 10, 2, True),           # counted, not emitted
    ChangeObj("relation", 20, 1, True),      # counted, not emitted
]


def test_classify_buckets_and_counts():
    out = classify_changes(_CHANGES)
    c = out["counts"]
    assert (c["added"], c["modified"], c["deleted"]) == (1, 1, 1)
    assert c["ways_changed"] == 1 and c["relations_changed"] == 1

    # added: new node with full Point geometry + tags + change metadata
    feat = out["added"]["features"][0]
    assert feat["geometry"] == {"type": "Point", "coordinates": [2.0, 1.0]}
    assert feat["properties"]["amenity"] == "cafe"
    assert feat["properties"]["change_type"] == "added" and feat["properties"]["osm_id"] == 1

    # modified: version > 1
    assert out["modified"]["features"][0]["properties"]["change_type"] == "modified"
    # deleted: invisible -> null geometry, still identifies the osm_id
    dfeat = out["deleted"]["features"][0]
    assert dfeat["geometry"] is None and dfeat["properties"]["osm_id"] == 3


def test_handler_writes_collections_and_returns_changeset(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_collect_changes", lambda gp, since, mb, lp: (12345, _CHANGES))
    monkeypatch.setattr(ch, "resolve_output_dir", lambda category: str(tmp_path))
    monkeypatch.setattr(ch, "open_output", lambda p, mode="w": open(p, mode))

    out = ch.handle_extract_changes({
        "region": {"geofabrik_path": "europe/germany", "name": "Germany"},
        "since": "1000",   # explicit sequence -> no cache localize needed
    })["changes"]

    assert out["since_sequence"] == 12345
    assert (out["added_count"], out["modified_count"], out["deleted_count"]) == (1, 1, 1)
    assert out["ways_changed"] == 1 and out["relations_changed"] == 1
    # the added GeoJSON was actually written
    added = json.load(open(out["added"]))
    assert added["type"] == "FeatureCollection"
    assert added["features"][0]["properties"]["name"] == "New Cafe"


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
