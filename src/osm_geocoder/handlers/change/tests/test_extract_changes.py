"""Offline tests for osm.Change.ExtractChanges (network + osmium seams mocked).

Geometry now comes from the osmium CLI pass (apply-changes -> getid -r -> export),
mocked here as ``_assemble_geometry`` returning a ``(osm_type, osm_id) -> geometry``
map. The pure classification, the geometry-token selection, the export parser, and
the handler's lazy-escalation bulkhead are all exercised offline.
"""

from __future__ import annotations

import json

import pytest

from osm_geocoder.handlers.change import change_handlers as ch
from osm_geocoder.tools._osm_tools.osm_changes import (
    ChangeObj,
    _parse_export,
    changed_geometry_tokens,
    classify_changes,
)


_CHANGES = [
    ChangeObj("node", 1, 1, True, lat=1.0, lon=2.0, tags={"amenity": "cafe", "name": "New Cafe"}),
    ChangeObj("node", 2, 3, True, lat=3.0, lon=4.0, tags={"shop": "bakery"}),
    ChangeObj("node", 3, 5, False),                                  # deleted node
    ChangeObj("way", 10, 2, True, tags={"highway": "residential"}),  # modified way
    ChangeObj("way", 11, 1, True, tags={"building": "yes"}),         # added (area) way
    ChangeObj("relation", 20, 4, True, tags={"type": "multipolygon", "leisure": "park"}),
    ChangeObj("relation", 21, 6, False),                             # deleted relation
]

# Geometry the osmium pass would return for the visible changed ways/relations.
_GEOM = {
    ("way", 10): {"type": "LineString", "coordinates": [[10.0, 50.0], [10.1, 50.1]]},
    ("way", 11): {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]},
    ("relation", 20): {"type": "MultiPolygon",
                       "coordinates": [[[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 0.0]]]]},
}


def test_classify_buckets_and_counts():
    out = classify_changes(_CHANGES, _GEOM)
    c = out["counts"]
    # nodes: 1 added, 1 modified, 1 deleted; way 11 added, way 10 modified;
    # relation 20 modified, relation 21 deleted
    assert (c["added"], c["modified"], c["deleted"]) == (2, 3, 2)
    assert (c["ways_added"], c["ways_modified"], c["ways_deleted"]) == (1, 1, 0)
    assert c["ways_changed"] == 2
    assert (c["relations_added"], c["relations_modified"], c["relations_deleted"]) == (0, 1, 1)
    assert c["relations_changed"] == 2

    # added node keeps full Point + tags + change metadata
    nfeat = next(f for f in out["added"]["features"] if f["properties"]["osm_type"] == "node")
    assert nfeat["geometry"] == {"type": "Point", "coordinates": [2.0, 1.0]}
    assert nfeat["properties"]["amenity"] == "cafe" and nfeat["properties"]["change_type"] == "added"

    # deleted node -> null geometry, id kept
    dnode = next(f for f in out["deleted"]["features"] if f["properties"]["osm_type"] == "node")
    assert dnode["geometry"] is None and dnode["properties"]["osm_id"] == 3


def test_classify_way_geometry_from_map():
    out = classify_changes(_CHANGES, _GEOM)
    way10 = next(f for f in out["modified"]["features"]
                 if f["properties"].get("osm_type") == "way" and f["properties"]["osm_id"] == 10)
    assert way10["geometry"]["type"] == "LineString"
    way11 = next(f for f in out["added"]["features"]
                 if f["properties"].get("osm_type") == "way")
    assert way11["geometry"]["type"] == "Polygon"
    assert way11["properties"]["building"] == "yes"


def test_classify_relation_multipolygon():
    out = classify_changes(_CHANGES, _GEOM)
    rel = next(f for f in out["modified"]["features"] if f["properties"]["osm_type"] == "relation")
    assert rel["geometry"]["type"] == "MultiPolygon"
    assert rel["properties"]["osm_id"] == 20
    assert rel["properties"]["leisure"] == "park"
    assert rel["properties"]["change_type"] == "modified"


def test_classify_null_geometry_when_absent_or_deleted():
    # no geom_map -> ways/relations classified + counted but null geometry; nodes keep points
    out = classify_changes(_CHANGES)
    way = next(f for f in out["modified"]["features"] if f["properties"]["osm_type"] == "way")
    assert way["geometry"] is None
    assert out["counts"]["ways_modified"] == 1 and out["counts"]["relations_modified"] == 1
    # deleted relation -> null geometry, id kept
    drel = next(f for f in out["deleted"]["features"] if f["properties"]["osm_type"] == "relation")
    assert drel["geometry"] is None and drel["properties"]["osm_id"] == 21


def test_changed_geometry_tokens():
    # visible added/modified ways + relations only; deleted + nodes excluded
    assert sorted(changed_geometry_tokens(_CHANGES)) == ["r20", "w10", "w11"]
    assert changed_geometry_tokens(
        [ChangeObj("node", 1, 1, True, lat=0.0, lon=0.0)]) == []
    assert changed_geometry_tokens(
        [ChangeObj("way", 5, 3, False)]) == []   # deleted way -> no geometry needed


def test_parse_export_dedupes_and_filters():
    rs = "\x1e"
    lines = [
        rs + json.dumps({"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                         "properties": {"@type": "way", "@id": 11, "building": "yes"}}),
        # same way also emitted as area -> area must win
        rs + json.dumps({"geometry": {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]},
                         "properties": {"@type": "way", "@id": 11, "building": "yes"}}),
        # a member way pulled by getid -r but NOT in `wanted` -> dropped
        rs + json.dumps({"geometry": {"type": "LineString", "coordinates": [[2, 2], [3, 3]]},
                         "properties": {"@type": "way", "@id": 99}}),
        rs + json.dumps({"geometry": {"type": "MultiPolygon", "coordinates": [[[[0, 0], [2, 0], [2, 2], [0, 0]]]]},
                         "properties": {"@type": "relation", "@id": 20}}),
    ]
    out = _parse_export("\n".join(lines), wanted={"w11", "r20"})
    assert out[("way", 11)]["type"] == "MultiPolygon"   # area beat the line
    assert out[("relation", 20)]["type"] == "MultiPolygon"
    assert ("way", 99) not in out                       # member way filtered out


def _mock_storage(localized="/tmp/base.pbf"):
    return type("S", (), {"localize": staticmethod(lambda p: localized)})()


def test_handler_writes_collections_and_returns_changeset(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, "_collect_changes",
                        lambda gp, since, mb, lp: (12345, _CHANGES, str(tmp_path / "osc" / "changes.osc.gz")))
    monkeypatch.setattr(ch, "_assemble_geometry", lambda base, osc, tokens: dict(_GEOM))
    monkeypatch.setattr(ch, "is_region_cached", lambda r, **k: True)
    monkeypatch.setattr(ch, "get_storage", _mock_storage)
    monkeypatch.setattr(ch, "cached_path", lambda gp: "cache/x.pbf")
    monkeypatch.setattr(ch, "resolve_output_dir", lambda category: str(tmp_path))
    monkeypatch.setattr(ch, "open_output", lambda p, mode="w": open(p, mode))

    out = ch.handle_extract_changes({
        "region": {"geofabrik_path": "europe/germany", "name": "Germany"},
        "since": "1000",
    })["changes"]

    assert out["since_sequence"] == 12345
    assert (out["added_count"], out["modified_count"], out["deleted_count"]) == (2, 3, 2)
    assert out["ways_changed"] == 2 and out["ways_modified"] == 1
    assert out["relations_changed"] == 2 and out["relations_modified"] == 1 and out["relations_deleted"] == 1
    # the changed relation got MultiPolygon geometry in the written file
    modified = json.load(open(out["modified"]))
    rel = next(f for f in modified["features"] if f["properties"]["osm_type"] == "relation")
    assert rel["geometry"]["type"] == "MultiPolygon"
    # the added node was written with its name
    added = json.load(open(out["added"]))
    assert any(f["properties"].get("name") == "New Cafe" for f in added["features"])


def test_handler_skips_osmium_pass_for_node_only_diff(tmp_path, monkeypatch):
    node_only = [ChangeObj("node", 1, 1, True, lat=1.0, lon=2.0, tags={"amenity": "cafe"})]
    monkeypatch.setattr(ch, "_collect_changes",
                        lambda gp, since, mb, lp: (7, node_only, str(tmp_path / "osc" / "c.osc.gz")))

    def _boom(*a, **k):
        raise AssertionError("osmium geometry pass must NOT run for a node-only diff")

    monkeypatch.setattr(ch, "_assemble_geometry", _boom)
    monkeypatch.setattr(ch, "is_region_cached", lambda r, **k: True)
    monkeypatch.setattr(ch, "get_storage", _mock_storage)
    monkeypatch.setattr(ch, "cached_path", lambda gp: "cache/x.pbf")
    monkeypatch.setattr(ch, "resolve_output_dir", lambda category: str(tmp_path))
    monkeypatch.setattr(ch, "open_output", lambda p, mode="w": open(p, mode))

    out = ch.handle_extract_changes({
        "region": {"geofabrik_path": "europe/germany"}, "since": "5",
    })["changes"]
    assert out["added_count"] == 1 and out["ways_changed"] == 0 and out["relations_changed"] == 0
    added = json.load(open(out["added"]))
    assert added["features"][0]["geometry"]["type"] == "Point"


def test_handler_null_geometry_when_region_not_cached(tmp_path, monkeypatch):
    # ways/relations changed but region not cached -> null geometry, no crash
    monkeypatch.setattr(ch, "_collect_changes",
                        lambda gp, since, mb, lp: (9, _CHANGES, str(tmp_path / "osc" / "c.osc.gz")))
    monkeypatch.setattr(ch, "is_region_cached", lambda r, **k: False)
    monkeypatch.setattr(ch, "get_storage", _mock_storage)
    monkeypatch.setattr(ch, "cached_path", lambda gp: "cache/x.pbf")
    monkeypatch.setattr(ch, "resolve_output_dir", lambda category: str(tmp_path))
    monkeypatch.setattr(ch, "open_output", lambda p, mode="w": open(p, mode))

    out = ch.handle_extract_changes({
        "region": {"geofabrik_path": "europe/germany"}, "since": "5",
    })["changes"]
    assert out["ways_changed"] == 2 and out["relations_changed"] == 2
    modified = json.load(open(out["modified"]))
    way = next(f for f in modified["features"] if f["properties"]["osm_type"] == "way")
    assert way["geometry"] is None


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


# ---------------------------------------------------------------------------
# Real osmium pipeline (apply-changes -> getid -r -> export -> parse). This is
# the IO seam the unit tests above mock; here we run the actual CLI against tiny
# synthetic PBFs to lock the real behavior (multipolygon-relation assembly with a
# hole, area-vs-line determination, member filtering). Skipped if osmium is absent.
# ---------------------------------------------------------------------------
import shutil
import subprocess

_OSMIUM = shutil.which("osmium")

_BASE_OSM = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="0.0" lon="0.0"/>
  <node id="2" version="1" lat="0.0" lon="1.0"/>
  <node id="3" version="1" lat="1.0" lon="1.0"/>
  <node id="4" version="1" lat="1.0" lon="0.0"/>
  <node id="5" version="1" lat="0.3" lon="0.3"/>
  <node id="6" version="1" lat="0.3" lon="0.6"/>
  <node id="7" version="1" lat="0.6" lon="0.6"/>
  <node id="8" version="1" lat="0.6" lon="0.3"/>
  <way id="20" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
  <way id="21" version="1"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/></way>
  <way id="30" version="1"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way>
  <relation id="500" version="1">
    <member type="way" ref="20" role="outer"/>
    <member type="way" ref="21" role="inner"/>
    <tag k="type" v="multipolygon"/><tag k="building" v="yes"/>
  </relation>
</osm>
"""

_OSC = """<?xml version='1.0' encoding='UTF-8'?>
<osmChange version="0.6">
  <modify><node id="1" version="2" lat="0.0" lon="0.0"><tag k="touched" v="1"/></node></modify>
</osmChange>
"""


@pytest.mark.skipif(_OSMIUM is None, reason="osmium CLI not installed")
def test_assemble_geometry_real_osmium_pipeline(tmp_path):
    from osm_geocoder.tools._osm_tools.osm_changes import _assemble_geometry

    base_osm = tmp_path / "base.osm"
    base_osm.write_text(_BASE_OSM)
    base_pbf = tmp_path / "base.osm.pbf"
    subprocess.run([_OSMIUM, "cat", str(base_osm), "-o", str(base_pbf)],
                   check=True, capture_output=True)
    osc = tmp_path / "chg.osc"
    osc.write_text(_OSC)

    geom = _assemble_geometry(str(base_pbf), str(osc), ["r500", "w30"])

    # multipolygon relation -> MultiPolygon with a hole (outer + inner ring)
    rel = geom[("relation", 500)]
    assert rel["type"] == "MultiPolygon"
    assert len(rel["coordinates"][0]) == 2          # outer ring + 1 inner hole
    # the highway way is a LineString, NOT polygonized despite sharing nodes
    assert geom[("way", 30)]["type"] == "LineString"
    # member ways pulled by getid -r but not requested are filtered out
    assert ("way", 20) not in geom and ("way", 21) not in geom
