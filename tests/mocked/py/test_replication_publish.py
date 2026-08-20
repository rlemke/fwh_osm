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
    # Patch the MULTI cutter — the single-region cut_diff is no longer on this
    # path, so patching it would make this assertion vacuous and the test
    # unable to fail on the thing it exists to catch.
    monkeypatch.setattr(rp, "cut_diff_multi",
                        lambda *a, **k: cut_calls.append(a) or {})

    res = rp.publish(www=www, polys=tmp_path / "no-polys", max_days=1)
    assert cut_calls == [], "must not cut without a polygon"
    assert res.regions[0].skipped and "polygon" in res.regions[0].reason


def test_all_regions_are_cut_in_ONE_pass_per_day(tmp_path, monkeypatch):
    """The optimisation that makes a 39-day catch-up practical.

    Decoding the day's diff dominates; polygon testing is nearly free.
    Measured: one region 27.3s, three 29.3s. Cutting per region would pay the
    decode N times — ~2.3 hours across 8 regions instead of ~20 minutes.
    """
    www = tmp_path
    for r in ("alpha", "beta", "gamma"):
        (www / f"{r}-updates").mkdir(parents=True)
        (tmp_path / f"{r}.poly").write_text("poly\nEND\n")
    rp.anchor(["alpha", "beta", "gamma"], 5088, "2026-08-18T00:00:00Z", www=www)
    monkeypatch.setattr(rp, "upstream_state", lambda *a, **k: (5090, "2026-08-20T00:00:00Z"))
    monkeypatch.setattr(rp, "diff_timestamp", lambda *a, **k: "2026-08-20T00:00:00Z")
    fake = tmp_path / "planet.osc.gz"
    fake.write_bytes(b"x")
    monkeypatch.setattr(rp, "fetch_planet_diff", lambda seq, dest, **k: fake)

    passes: list[list[str]] = []

    def _multi(planet_diff, regions, polys, staging, **k):
        passes.append(sorted(regions))
        staging.mkdir(parents=True, exist_ok=True)
        out = {}
        for r in regions:
            f = staging / f"{r}.osc.gz"
            f.write_bytes(b"diff")
            out[r] = f
        return out

    monkeypatch.setattr(rp, "cut_diff_multi", _multi)
    res = rp.publish(www=www, polys=tmp_path, max_days=2)

    assert res.days == 2
    # TWO days, so exactly two passes — not two per region.
    assert passes == [["alpha", "beta", "gamma"], ["alpha", "beta", "gamma"]]
    for r in res.regions:
        assert r.published == [5089, 5090], r


# --- enumeration is the operation that fails -------------------------------


def test_an_unreadable_tree_raises_instead_of_reporting_no_regions(tmp_path, monkeypatch):
    """The failure that made the nightly timer a no-op.

    A macOS LaunchAgent can `stat` an external-volume path while being denied
    `readdir` on it. `Path.glob` swallows that OSError and yields nothing, so a
    DENIED directory looked exactly like a correctly configured tree with no
    regions — the job reported success having published nothing, silently, for
    as long as anyone left it running.
    """
    monkeypatch.delenv(rp.REGIONS_ENV, raising=False)

    def _denied(self):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("pathlib.Path.iterdir", _denied)
    with pytest.raises(rp.ReplicationError, match="cannot list"):
        rp.discover_regions(tmp_path)


def test_an_explicit_region_list_avoids_enumeration_entirely(tmp_path, monkeypatch):
    """What a scheduled job should use: no readdir, so no permission to deny."""
    monkeypatch.setenv(rp.REGIONS_ENV, "europe, africa  asia")

    def _explode(self):
        raise AssertionError("must not enumerate when the list is explicit")

    monkeypatch.setattr("pathlib.Path.iterdir", _explode)
    assert rp.discover_regions(tmp_path) == ["africa", "asia", "europe"]


def test_regions_json_is_used_for_keys_only(tmp_path, monkeypatch):
    """The manifest on this deployment still carries paths from a previous
    volume name, so trusting its `poly` values would point at nothing."""
    import json

    monkeypatch.delenv(rp.REGIONS_ENV, raising=False)
    www = tmp_path / "www"
    www.mkdir()
    (tmp_path / "regions.json").write_text(json.dumps([
        {"key": "europe", "poly": "/Volumes/gone/europe.poly"},
        {"key": "africa", "poly": "/Volumes/gone/africa.poly"},
    ]))
    assert rp.discover_regions(www) == ["africa", "europe"]


# --- rolling the served extract forward -------------------------------------


def test_apply_refuses_to_cross_a_gap(tmp_path, monkeypatch):
    """The corruption this design works hardest to avoid.

    If state.txt advertises a sequence whose diff is missing, applying across
    the hole yields an extract that is quietly missing a day while its header
    asserts it is current — and every consumer then trusts that header.
    """
    www = tmp_path
    (www / "demo-updates").mkdir(parents=True)
    (www / "demo-latest.osm.pbf").write_bytes(b"pbf")
    rp.anchor(["demo"], 5088, "2026-08-18T00:00:00Z", www=www)
    # Head claims 5090, but only 5089 was ever written.
    (www / "demo-updates" / "state.txt").write_text(
        rp.format_state(5090, "2026-08-20T00:00:00Z"), encoding="utf-8")
    d = www / "demo-updates" / rp.sequence_path(5089)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.with_suffix(".osc.gz").write_bytes(b"x")

    with pytest.raises(rp.ReplicationError, match="Refusing to apply across a gap"):
        rp.apply_published("demo", "http://example/", www=www)


def test_apply_is_a_noop_when_the_extract_is_already_at_the_head(tmp_path):
    www = tmp_path
    (www / "demo-updates").mkdir(parents=True)
    (www / "demo-latest.osm.pbf").write_bytes(b"pbf")
    rp.anchor(["demo"], 5090, "2026-08-20T00:00:00Z", www=www)
    frm, to, size = rp.apply_published("demo", "http://example/", www=www)
    assert (frm, to, size) == (5090, 5090, 0)


def test_apply_moves_the_recorded_extract_sequence(tmp_path, monkeypatch):
    """The extract has moved, so its recorded sequence must move with it —
    otherwise the next apply re-does everything and --stamp-extracts would
    write a stale baseline."""
    www = tmp_path
    (www / "demo-updates").mkdir(parents=True)
    (www / "demo-latest.osm.pbf").write_bytes(b"pbf")
    rp.anchor(["demo"], 5089, "2026-08-19T00:00:00Z", www=www)
    (www / "demo-updates" / "state.txt").write_text(
        rp.format_state(5090, "2026-08-20T00:00:00Z"), encoding="utf-8")
    d = (www / "demo-updates" / rp.sequence_path(5090)).with_suffix(".osc.gz")
    d.parent.mkdir(parents=True, exist_ok=True)
    d.write_bytes(b"x")

    def _fake_run(cmd, **k):
        # stand in for osmium: produce the output file it was asked for
        out = cmd[cmd.index("-o") + 1]
        pathlib.Path(out).write_bytes(b"applied")
        class R: returncode = 0
        return R()

    import pathlib
    import subprocess as sp
    monkeypatch.setattr(sp, "run", _fake_run)
    frm, to, _size = rp.apply_published("demo", "http://example/", www=www)
    assert (frm, to) == (5089, 5090)
    assert rp.extract_state("demo", www)[0] == 5090
