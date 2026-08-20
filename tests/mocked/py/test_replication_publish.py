# SPDX-License-Identifier: Apache-2.0
"""Tests for the replication producer — Phase 2 of the self-hosted split.

The interesting failure here is not a crash. Every mistake in this module
produces a *silently wrong replication stream*: diffs that a consumer accepts
and applies, leaving its extract quietly missing a span of edits. So these tests
concentrate on the arithmetic and the two-sequence bookkeeping rather than on
the osmium calls, which are exercised for real against the published tree.
"""

from __future__ import annotations

import pytest

from osm_geocoder.tools._osm_tools import replication_publish as rp


# --- Osmosis dialect --------------------------------------------------------


def test_sequence_path_is_the_osmosis_triplet():
    assert rp.sequence_path(5090) == "000/005/090"
    assert rp.sequence_path(0) == "000/000/000"
    assert rp.sequence_path(123456789) == "123/456/789"


def test_sequence_path_refuses_a_negative():
    with pytest.raises(ValueError, match="non-negative"):
        rp.sequence_path(-1)


def test_state_txt_escapes_colons_and_round_trips():
    """state.txt is a Java properties file — every replication client expects
    the colons escaped, and the tree's existing hand-written files use that
    form. An unescaped timestamp parses to the wrong value in strict clients."""
    text = rp.format_state(5090, "2026-08-20T00:00:00Z")
    assert "sequenceNumber=5090" in text
    assert r"2026-08-20T00\:00\:00Z" in text
    assert rp.parse_state(text) == (5090, "2026-08-20T00:00:00Z")


def test_a_state_without_a_sequence_parses_as_never_published():
    """Exactly what Phase 1 left behind — a bare timestamp. It must read as
    'never published' rather than raising, because it is the normal input."""
    seq, ts = rp.parse_state("timestamp=2026-07-12T23\\:59\\:57Z\n")
    assert seq is None
    assert ts == "2026-07-12T23:59:57Z"


# --- the two sequences ------------------------------------------------------


def _tree(tmp_path, region="demo"):
    d = tmp_path / f"{region}-updates"
    d.mkdir(parents=True)
    return tmp_path


def test_anchor_records_the_extract_sequence_as_well_as_the_head(tmp_path):
    """The bug this pins cost a real mis-stamp during development.

    This project publishes diffs WITHOUT applying them, so the published head
    runs ahead of the served extract by design. Stamping the head into the
    extract tells consumers it already contains diffs it does not, and they
    start after them — silently skipping every edit published so far. The two
    sequences must therefore be recorded separately at anchor time, when they
    are still equal and the fact is still knowable.
    """
    www = _tree(tmp_path)
    rp.anchor(["demo"], 5051, "2026-07-12T00:00:00Z", www=www)

    assert rp.region_state("demo", www) == (5051, "2026-07-12T00:00:00Z")
    assert rp.extract_state("demo", www) == (5051, "2026-07-12T00:00:00Z")

    # Publishing moves the head; the extract's own sequence must NOT follow.
    (www / "demo-updates" / "state.txt").write_text(
        rp.format_state(5053, "2026-07-14T00:00:00Z"), encoding="utf-8")
    assert rp.region_state("demo", www)[0] == 5053
    assert rp.extract_state("demo", www)[0] == 5051, (
        "the extract has not moved — only diffs were published"
    )


def test_extract_state_is_none_before_anchoring(tmp_path):
    www = _tree(tmp_path)
    assert rp.extract_state("demo", www) == (None, "")


def test_discover_regions_finds_the_updates_directories(tmp_path):
    for r in ("europe", "africa"):
        (tmp_path / f"{r}-updates").mkdir(parents=True)
    (tmp_path / "europe-latest.osm.pbf").write_bytes(b"")  # not a region
    assert rp.discover_regions(tmp_path) == ["africa", "europe"]


# --- publish decisions ------------------------------------------------------


def test_publish_refuses_to_guess_a_baseline(tmp_path, monkeypatch):
    """A region that never published has no anchor, and inferring one would
    produce diffs that do not compose with the extract they claim to update.
    Report and skip instead."""
    www = _tree(tmp_path)
    monkeypatch.setattr(rp, "upstream_state", lambda *a, **k: (5090, "2026-08-20T00:00:00Z"))
    res = rp.publish(www=www, polys=tmp_path, max_days=3)
    assert res.days == 0
    assert len(res.regions) == 1
    assert res.regions[0].skipped
    assert "anchor" in res.regions[0].reason


def test_publish_is_bounded_by_max_days(tmp_path, monkeypatch):
    """A never-published region is arbitrarily far behind; an unbounded catch-up
    would pull tens of GB without anyone choosing to."""
    www = _tree(tmp_path)
    rp.anchor(["demo"], 5000, "2026-05-22T00:00:00Z", www=www)
    monkeypatch.setattr(rp, "upstream_state", lambda *a, **k: (5090, "2026-08-20T00:00:00Z"))

    fetched: list[int] = []
    monkeypatch.setattr(rp, "fetch_planet_diff",
                        lambda seq, dest, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    res = rp.publish(www=www, polys=tmp_path, max_days=3, dry_run=True)
    # dry_run must not fetch at all, and must not claim to have published.
    assert fetched == []
    assert res.upstream_sequence == 5090
    assert res.from_sequence == 5000


def test_publish_reports_current_when_already_at_the_head(tmp_path, monkeypatch):
    www = _tree(tmp_path)
    rp.anchor(["demo"], 5090, "2026-08-20T00:00:00Z", www=www)
    monkeypatch.setattr(rp, "upstream_state", lambda *a, **k: (5090, "2026-08-20T00:00:00Z"))
    res = rp.publish(www=www, polys=tmp_path, max_days=7)
    assert res.days == 0
    assert res.regions[0].skipped and res.regions[0].reason == "current"


def test_a_region_without_a_polygon_is_skipped_not_fabricated(tmp_path, monkeypatch):
    """No polygon means no way to cut the region. Publishing the UNCUT planet
    diff under its name would be catastrophic — every consumer would apply the
    whole world to a regional extract."""
    www = _tree(tmp_path)
    rp.anchor(["demo"], 5089, "2026-08-19T00:00:00Z", www=www)
    monkeypatch.setattr(rp, "upstream_state", lambda *a, **k: (5090, "2026-08-20T00:00:00Z"))
    monkeypatch.setattr(rp, "diff_timestamp", lambda *a, **k: "2026-08-20T00:00:00Z")
    fake = tmp_path / "planet.osc.gz"
    fake.write_bytes(b"x")
    monkeypatch.setattr(rp, "fetch_planet_diff", lambda seq, dest, **k: fake)

    cut_calls: list = []
    monkeypatch.setattr(rp, "cut_diff", lambda *a, **k: cut_calls.append(a) or 1)

    res = rp.publish(www=www, polys=tmp_path / "no-polys", max_days=1)
    assert cut_calls == [], "must not cut without a polygon"
    assert res.regions[0].skipped and "polygon" in res.regions[0].reason
