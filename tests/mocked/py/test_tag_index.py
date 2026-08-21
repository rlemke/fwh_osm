# SPDX-License-Identifier: Apache-2.0
"""Tests for the incrementally-maintained tag index.

The dangerous failure here is an index that keeps serving while quietly
diverging from reality — cameras that were deleted or untagged months ago still
plotted on the map, with nothing erroring. So these concentrate on the removal
paths, which are the ones easy to omit and impossible to notice.
"""

from __future__ import annotations

import json

import pytest

from osm_geocoder.tools._osm_tools import tag_index as ti


@pytest.fixture(autouse=True)
def _isolated_index_root(tmp_path, monkeypatch):
    monkeypatch.setenv(ti.INDEX_ROOT_ENV, str(tmp_path / "idx"))


# --- the predicate ----------------------------------------------------------


def test_spec_parses_key_value_and_bare_key():
    assert ti.parse_spec("surveillance:type=ALPR") == [("surveillance:type", "ALPR")]
    assert ti.parse_spec("man_made") == [("man_made", None)]
    assert ti.parse_spec("a=1, b") == [("a", "1"), ("b", None)]


def test_spec_refuses_to_be_empty():
    with pytest.raises(ti.IndexError_, match="empty tag spec"):
        ti.parse_spec("   ,  ")


def test_matching_is_or_across_terms_and_exact_on_values():
    terms = ti.parse_spec("surveillance:type=ALPR, man_made")
    assert ti.matches({"surveillance:type": "ALPR"}, terms)
    assert ti.matches({"man_made": "anything"}, terms)
    assert not ti.matches({"surveillance:type": "camera"}, terms)
    assert not ti.matches({"highway": "bus_stop"}, terms)


def test_the_osmium_filter_mirrors_the_predicate():
    """One spec drives BOTH the initial osmium scan and the per-node predicate.
    If they disagree the index is built from one set and maintained against
    another, which diverges silently."""
    terms = ti.parse_spec("surveillance:type=ALPR, man_made")
    assert ti.osmium_filter(terms) == ["n/surveillance:type=ALPR", "n/man_made"]


# --- the update rules -------------------------------------------------------


def _seed(name="t", spec="surveillance:type=ALPR", seq=100, rows=()):
    con = ti._connect(name)
    ti._meta_set(con, "expression", spec)
    ti._meta_set(con, "sequence", str(seq))
    for oid, lon, lat, tags in rows:
        con.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?)",
                    (oid, lon, lat, json.dumps(tags)))
    con.commit()
    con.close()


class _FakeTag:
    def __init__(self, k, v):
        self.k, self.v = k, v


class _FakeNode:
    def __init__(self, nid, visible=True, tags=None, lon=1.0, lat=2.0):
        self.id, self.visible = nid, visible
        self.tags = [_FakeTag(k, v) for k, v in (tags or {}).items()]
        self.location = type("L", (), {"lon": lon, "lat": lat})()


def _apply(monkeypatch, name, nodes, seq):
    """Drive update_from_diff with a fabricated change file."""
    import osmium

    class _Fake(osmium.SimpleHandler):
        pass

    def _fake_apply_file(self, _path):
        for n in nodes:
            self.node(n)

    monkeypatch.setattr(osmium.SimpleHandler, "apply_file", _fake_apply_file, raising=False)
    from pathlib import Path
    return ti.update_from_diff(name, Path("ignored.osc.gz"), seq)


def test_a_deleted_node_is_removed(monkeypatch):
    """An OSM delete carries NO TAGS, so the index cannot tell whether the
    deleted node was one of its own — it must remove by id regardless."""
    _seed(rows=[(7, 1.0, 2.0, {"surveillance:type": "ALPR"})])
    up, rm = _apply(monkeypatch, "t", [_FakeNode(7, visible=False)], 101)
    assert (up, rm) == (0, 1)
    assert ti.stats("t").count == 0


def test_an_untagged_node_is_removed(monkeypatch):
    """The subtle one. A camera whose tag is removed arrives as an ordinary
    MODIFY that no longer matches. An index that only adds matches would keep
    it forever, drifting from reality every night while looking healthy."""
    _seed(rows=[(7, 1.0, 2.0, {"surveillance:type": "ALPR"})])
    up, rm = _apply(monkeypatch, "t",
                    [_FakeNode(7, tags={"amenity": "bench"})], 101)
    assert (up, rm) == (0, 1)
    assert ti.stats("t").count == 0


def test_a_newly_tagged_node_is_added(monkeypatch):
    _seed()
    up, _rm = _apply(monkeypatch, "t",
                     [_FakeNode(9, tags={"surveillance:type": "ALPR"})], 101)
    assert up == 1
    assert ti.stats("t").count == 1


def test_removing_an_absent_id_is_harmless(monkeypatch):
    """Every non-matching node in the diff produces a removal, and the vast
    majority were never in the index. That must be a cheap no-op, not an error."""
    _seed()
    up, rm = _apply(monkeypatch, "t", [_FakeNode(12345, tags={"highway": "bus_stop"})], 101)
    assert (up, rm) == (0, 1)
    assert ti.stats("t").count == 0


# --- sequence discipline ----------------------------------------------------


def test_replaying_an_applied_sequence_is_a_noop(monkeypatch):
    _seed(seq=100)
    assert _apply(monkeypatch, "t", [_FakeNode(1, tags={"surveillance:type": "ALPR"})], 100) == (0, 0)
    assert ti.stats("t").sequence == 100


def test_skipping_a_sequence_is_refused(monkeypatch):
    """An index quietly missing a day keeps serving, looks healthy, and is
    wrong — the failure this whole project keeps meeting in other forms."""
    _seed(seq=100)
    with pytest.raises(ti.IndexError_, match="refusing to jump"):
        _apply(monkeypatch, "t", [], 103)
    assert ti.stats("t").sequence == 100


def test_an_index_without_a_sequence_cannot_be_updated(monkeypatch):
    con = ti._connect("t")
    ti._meta_set(con, "expression", "surveillance:type=ALPR")
    con.commit()
    con.close()
    with pytest.raises(ti.IndexError_, match="no sequence"):
        _apply(monkeypatch, "t", [], 101)


# --- export -----------------------------------------------------------------


def test_export_writes_a_feature_collection(tmp_path):
    _seed(rows=[(7, 10.5, -3.25, {"surveillance:type": "ALPR", "operator": "x"})])
    out = tmp_path / "alpr.geojson"
    assert ti.export_geojson("t", out) == 1
    doc = json.loads(out.read_text())
    assert doc["type"] == "FeatureCollection"
    f = doc["features"][0]
    assert f["geometry"]["coordinates"] == [10.5, -3.25]
    assert f["properties"]["osm_id"] == 7
    assert f["properties"]["operator"] == "x", "verbatim tags are preserved"


def test_the_last_operation_in_a_diff_wins(monkeypatch):
    """A change file may carry several versions of one node in a single day.

    The first implementation collected all removals and all upserts and ran
    removals-then-upserts, which REORDERS them: a node upserted early and
    deleted later had the delete applied first and the insert second, leaving a
    camera in the index that no longer exists. Real data showed the symptom —
    1325 upserts producing 1256 rows.
    """
    _seed()
    # created as ALPR, then deleted, within one diff
    up, _rm = _apply(monkeypatch, "t", [
        _FakeNode(42, tags={"surveillance:type": "ALPR"}),
        _FakeNode(42, visible=False),
    ], 101)
    assert ti.stats("t").count == 0, "the delete came last and must win"
    assert up == 0


def test_a_delete_then_recreate_in_one_diff_keeps_the_node(monkeypatch):
    """The mirror case, which the old code got right only by accident."""
    _seed(rows=[(42, 1.0, 2.0, {"surveillance:type": "ALPR"})])
    _apply(monkeypatch, "t", [
        _FakeNode(42, visible=False),
        _FakeNode(42, tags={"surveillance:type": "ALPR"}, lon=9.0, lat=8.0),
    ], 101)
    con = ti._connect("t")
    row = con.execute("SELECT lon, lat FROM nodes WHERE id = 42").fetchone()
    con.close()
    assert row == (9.0, 8.0), "the recreate came last and must win"
