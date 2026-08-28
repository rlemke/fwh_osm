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
