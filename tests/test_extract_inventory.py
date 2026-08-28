"""Offline tests for osm.inventory - extract state + Overpass state.

No network: the HTTP probes are stubbed, and the pure logic (currency, verdict,
rendering, handler coercion) is exercised directly.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from osm_geocoder.tools._osm_tools import extract_inventory as inv  # noqa: E402


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_age_is_measured_from_replication_timestamp():
    assert inv._age_hours(_iso(5)) == pytest.approx(5, abs=0.05)
    # Unparseable or absent must be None, never 0 - "no timestamp" is not "current".
    assert inv._age_hours(None) is None
    assert inv._age_hours("not-a-date") is None


def test_tree_filename_uses_the_override_mapping():
    """Our region keys need not match the tree's file names."""
    assert inv.tree_filename("europe") == "europe-latest.osm.pbf"
    assert inv.tree_filename("australia-oceania") == "oceania-latest.osm.pbf"


def test_verdict_distinguishes_problem_from_unverified(monkeypatch):
    """0/1/2 semantics: a fault alarms, an unreachable tree does NOT.

    Alarming merely because we are offline trains the reader to ignore the
    alarm, which is how a silent failure survives (the fleet's dead-letters and
    osm-watchdog use the same rule).
    """
    monkeypatch.setattr(inv, "regions", lambda: ["a", "b"])
    monkeypatch.setattr(inv, "overpass_state", lambda *a, **k: {
        "mirrors": [], "usable_count": 0, "total_count": 0})

    def tree(present, age):
        return {"store": "http-tree", "base": "x", "scope": "s", "regions": {
            r: {"present": present, "age_hours": age} for r in ("a", "b")}}

    monkeypatch.setattr(inv, "survey_tree", lambda *a, **k: tree(True, 1.0))
    assert inv.build_report(include_object_store=False)["status"] == "ok"

    monkeypatch.setattr(inv, "survey_tree", lambda *a, **k: tree(False, None))
    assert inv.build_report(include_object_store=False)["status"] == "problem"

    monkeypatch.setattr(inv, "survey_tree", lambda *a, **k: tree(True, 999.0))
    r = inv.build_report(include_object_store=False, stale_after_hours=48)
    assert r["status"] == "problem" and r["summary"]["stale"] == ["a", "b"]

    monkeypatch.setattr(inv, "survey_tree", lambda *a, **k: {
        "store": "t", "base": "x", "scope": "s",
        "regions": {r: {"present": False, "error": "TimeoutError"} for r in ("a", "b")}})
    # Every probe errored -> we cannot tell. Reported, not alarmed.
    assert inv.build_report(include_object_store=False)["status"] == "problem"


def test_overpass_probes_are_reported_independently(monkeypatch):
    """A mirror serving /status but not /timestamp is NOT usable; collapsing the
    two into one flag produced 'reachable=True, error=URLError'."""
    def fake_get(url, timeout=30):
        if url.endswith("/timestamp"):
            raise TimeoutError("no timestamp")
        return "Rate limit: 2\n2 slots available now.\n"
    monkeypatch.setattr(inv, "_get_text", fake_get)
    monkeypatch.setattr(inv, "overpass_endpoints", lambda: ["https://x/api/interpreter"])
    st = inv.overpass_state()
    m = st["mirrors"][0]
    assert m["status_ok"] is True
    assert m["timestamp_ok"] is False
    assert m["usable"] is False          # the whole point
    assert st["usable_count"] == 0


def test_overpass_usable_when_both_probes_answer(monkeypatch):
    def fake_get(url, timeout=30):
        if url.endswith("/timestamp"):
            return _iso(0.02)
        return "Rate limit: 2\n2 slots available now.\n"
    monkeypatch.setattr(inv, "_get_text", fake_get)
    monkeypatch.setattr(inv, "overpass_endpoints", lambda: ["https://x/api/interpreter"])
    st = inv.overpass_state()
    m = st["mirrors"][0]
    assert m["usable"] and m["slots_available"] == "2" and m["rate_limit"] == "2"
    assert m["data_lag_hours"] == pytest.approx(0.02, abs=0.02)


def test_remote_probe_never_downloads_the_whole_file():
    """The header probe must be bounded. A 40 GB extract is probed with a 64 KB
    Range request; anything that could fetch the body is a bug."""
    assert inv.HEADER_PROBE_BYTES <= 1024 * 1024
    src = Path(inv.__file__).read_text()
    assert 'f"bytes=0-{HEADER_PROBE_BYTES - 1}"' in src


def test_report_states_its_cost_tier(tmp_path):
    """A header survey never measures counts, so blank count cells must not be
    readable as zero."""
    rep = {"generated_at": "now", "stale_after_hours": 48.0, "status": "ok",
           "summary": {"present": 1, "expected": 1, "overpass_usable": "1/1"},
           "tree": {"store": "http-tree", "base": "b", "scope": "continents only",
                    "regions": {"europe": {"present": True, "age_hours": 3.0,
                                           "size_bytes": 40_000_000_000,
                                           "replication_sequence": "5098"}}},
           "overpass": {"mirrors": [{"endpoint": "e", "usable": True,
                                     "data_lag_hours": 0.03, "slots_available": "2"}]}}
    html_path, json_path = inv.render_report(rep, dest=str(tmp_path))
    html = Path(html_path).read_text()
    assert "header only" in html
    assert "Feature counts were NOT measured" in html
    assert "europe" in html and "40.0 GB" in html and "5098" in html
    assert json.loads(Path(json_path).read_text())["status"] == "ok"

    rep["local"] = {"counted_features": True, "regions": {}}
    html2 = Path(inv.render_report(rep, dest=str(tmp_path))[0]).read_text()
    assert "whole-file scan" in html2
    assert "Feature counts were NOT measured" not in html2


def test_handler_reports_missing_counts_as_minus_one_not_zero():
    """FFL Long fields cannot carry null. A header-only probe must return -1
    ('not measured'), never 0, which would read as an empty extract."""
    from osm_geocoder.handlers.inventory import inventory_handlers as ih
    rec = {"present": True, "size_bytes": 5, "replication_sequence": "1", "age_hours": 2.0}
    assert ih._i(rec.get("node_count")) == -1
    assert ih._f(rec.get("age_hours")) == 2.0
    assert ih._f(None) == -1.0


def test_survey_handler_does_not_probe_overpass(monkeypatch):
    """The workflow has a dedicated ProbeOverpass step; probing here too made the
    report disagree with itself ('2/3 usable' beside '1/3 usable')."""
    from osm_geocoder.handlers.inventory import inventory_handlers as ih
    seen = {}

    def fake_build(**kw):
        seen.update(kw)
        return {"summary": {"expected": 1, "present": 1, "missing": [], "stale": [],
                            "oldest_age_hours": 1.0, "overpass_usable": "not probed"},
                "status": "ok", "regions_expected": ["europe"],
                "tree": {"regions": {"europe": {"present": True}}}}
    monkeypatch.setattr(ih.extract_inventory, "build_report", fake_build)
    ih.handle_survey_extracts({})
    assert seen["include_overpass"] is False


def test_build_report_summary_follows_the_mirrors_it_was_given():
    from osm_geocoder.handlers.inventory import inventory_handlers as ih
    survey = {"summary": {"present": 1, "expected": 1, "overpass_usable": "9/9"},
              "status": "ok", "tree": {"store": "t", "base": "", "scope": "",
                                       "regions": {}}, "generated_at": "now"}
    mirrors = [{"endpoint": "a", "usable": True}, {"endpoint": "b", "usable": False}]
    out = ih.handle_build_state_report({"survey": json.dumps(survey),
                                        "overpass": json.dumps(mirrors)})
    assert "1/2 usable" in out["detail"]      # not the stale 9/9 it was handed


# --------------------------------------------------------------------------- #
# sub-regions: countries / states / counties
# --------------------------------------------------------------------------- #
def _obj(key: str, days_old: float, size: int = 10):
    return {"Key": key, "Size": size,
            "LastModified": datetime.now(UTC) - timedelta(days=days_old)}


class _FakeS3:
    """Enough of the boto3 client for the survey: list + ranged get."""

    def __init__(self, objects, header_bytes=b""):
        self._objects = objects
        self._header = header_bytes

    def get_paginator(self, _name):
        objs = self._objects

        class _P:
            def paginate(self, **_kw):
                return [{"Contents": objs}]
        return _P()

    def get_object(self, **_kw):
        class _B:
            def __init__(self, data): self._d = data
            def read(self): return self._d
        return {"Body": _B(self._header)}


def test_key_depth_maps_to_administrative_tier():
    assert inv.DEPTH_TIERS[0] == "continent"
    assert inv.DEPTH_TIERS[1] == "country"
    assert inv.DEPTH_TIERS[2] == "state-or-province"
    assert inv.DEPTH_TIERS[3] == "county-or-district"


def test_subregion_survey_groups_by_tier_and_counts_stale(monkeypatch):
    objs = [
        _obj("europe-latest.osm.pbf", 1),
        _obj("europe/france-latest.osm.pbf", 30),
        _obj("north-america/us/texas-latest.osm.pbf", 30),
        _obj("north-america/us/texas/harris-latest.osm.pbf", 33),
        _obj("north-america/us/texas/travis-latest.osm.pbf", 2),
    ]
    monkeypatch.setattr(inv, "_s3_client", lambda *a, **k: _FakeS3(objs))
    monkeypatch.setattr(inv, "_probe_s3_header",
                        lambda *a, **k: {"has_replication_timestamp": False})
    out = inv.survey_subregions(sample_per_tier=1, stale_after_days=14)
    assert out["total_objects"] == 5
    assert out["tiers"]["county-or-district"]["count"] == 2
    # only the 33-day county is stale; the 2-day one is not
    assert out["tiers"]["county-or-district"]["mtime_stale_count"] == 1
    assert out["tiers"]["continent"]["mtime_stale_count"] == 0
    assert out["mtime_stale_count"] == 3


def test_missing_data_vintage_is_surfaced_as_a_finding(monkeypatch):
    """Sub-regions carry a replication BASE URL but no timestamp/sequence, so
    their diffs cannot be applied and their age cannot be stated from content.
    That must be reported, not silently replaced by mtime."""
    objs = [_obj("europe-latest.osm.pbf", 1),                  # continent: has one
            _obj("europe/france-latest.osm.pbf", 1)]           # country: does not

    def probe(_c, _b, key):
        return {"has_replication_timestamp": key.count("/") == 0}
    monkeypatch.setattr(inv, "_s3_client", lambda *a, **k: _FakeS3(objs))
    monkeypatch.setattr(inv, "_probe_s3_header", probe)
    out = inv.survey_subregions(sample_per_tier=1)
    # Exactly the tier whose samples lack a timestamp, and only that one.
    assert out["tiers_without_data_vintage"] == ["country"]
    assert out["tiers"]["continent"]["sampled_with_replication_timestamp"] == 1
    assert out["tiers"]["country"]["sampled_with_replication_timestamp"] == 0


def test_mtime_fields_are_named_so_they_cannot_be_read_as_data_vintage(monkeypatch):
    """A weaker signal must not wear the same name as a stronger one."""
    objs = [_obj("europe/france-latest.osm.pbf", 5)]
    monkeypatch.setattr(inv, "_s3_client", lambda *a, **k: _FakeS3(objs))
    monkeypatch.setattr(inv, "_probe_s3_header",
                        lambda *a, **k: {"has_replication_timestamp": False})
    tier = inv.survey_subregions(sample_per_tier=1)["tiers"]["country"]
    assert "mtime_oldest_days" in tier and "mtime_stale_count" in tier
    # nothing in this tier may claim a header-derived age
    assert "age_hours" not in tier and "oldest_age_hours" not in tier


def test_subregion_staleness_does_not_turn_the_headline_red(monkeypatch):
    """The sub-region tier refreshes on its own schedule. Folding it into the
    top-level verdict would leave the report permanently red, which is how an
    alarm stops being read."""
    monkeypatch.setattr(inv, "regions", lambda: ["a"])
    monkeypatch.setattr(inv, "survey_tree", lambda *a, **k: {
        "store": "t", "base": "", "scope": "", "regions": {"a": {"present": True,
                                                                 "age_hours": 1.0}}})
    monkeypatch.setattr(inv, "survey_subregions", lambda **k: {
        "total_objects": 3917, "total_bytes": 1, "mtime_stale_count": 3419,
        "tiers": {}, "tiers_without_data_vintage": ["county-or-district"]})
    r = inv.build_report(include_object_store=False, include_overpass=False,
                         include_subregions=True)
    assert r["status"] == "ok"                 # continents are healthy
    assert r["subregion_status"] == "stale"    # and this is reported separately
    assert r["summary"]["subregion_stale"] == 3419


def test_subregion_html_explains_why_there_is_no_vintage():
    sub = {"total_objects": 2, "total_bytes": 2e9, "bucket": "b", "stale_after_days": 14,
           "tiers": {"county-or-district": {"count": 2, "bytes": 2e9,
                                            "mtime_oldest_days": 33.0,
                                            "mtime_stale_count": 2,
                                            "sampled": 3,
                                            "sampled_with_replication_timestamp": 0}},
           "tiers_without_data_vintage": ["county-or-district"]}
    html = inv._subregion_html(sub)
    assert "No data vintage" in html
    assert "osmium extract" in html and "sequence number" in html
    assert "LAST-MODIFIED" in html
    # and the not-surveyed case must not pretend everything is fine
    assert "Not surveyed" in inv._subregion_html(None)
