"""Event facet handlers for osm.inventory - state of the region extracts.

A thin coercion layer over tools/_osm_tools/extract_inventory.py: the handlers
own the FFL contract (flat, typed return fields) and the tools module owns the
behaviour, per the domain's tools/handlers split.

⚠️ `_step_log` is a CALLBACK, `step_log(msg, level=...)` - NOT a list. Calling
`.append` on it kills the handler at its first log line, and a domain's own
tests do not catch it because they pass params without `_step_log` so the branch
never runs (that is exactly how six handlers in another domain shipped dead with
48 tests green). `fw util ffl-audit` checks for this.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ...tools._osm_tools import extract_inventory

NAMESPACE = "osm.inventory"


def _log(step_log, msg: str, level: str = "info") -> None:
    if callable(step_log):
        step_log(msg, level=level)


def _f(value: Any, default: float = -1.0) -> float:
    """FFL Double fields cannot carry null; -1 is the explicit 'not measured'."""
    return float(value) if isinstance(value, (int, float)) else default


def _i(value: Any, default: int = -1) -> int:
    return int(value) if isinstance(value, (int, float)) else default


def handle_survey_extracts(params: dict[str, Any]) -> dict[str, Any]:
    """Handle SurveyExtracts - header-only unless count_features."""
    count = bool(params.get("count_features", False))
    local_dir = params.get("local_dir", "") or ""
    include_os = bool(params.get("include_object_store", True))
    stale_h = float(params.get("stale_after_hours", 48.0) or 48.0)
    step_log = params.get("_step_log")

    _log(step_log, f"SurveyExtracts count_features={count} "
                   f"object_store={include_os} stale_after={stale_h}h")
    if count and not local_dir:
        # Counting means reading whole files; there is nothing to read remotely
        # without downloading, which is never done implicitly.
        _log(step_log, "count_features requested with no local_dir - counts need "
                       "extracts on local disk; surveying headers only", "warning")
        count = False

    rep = extract_inventory.build_report(
        count_features=count, local_dir=local_dir,
        include_object_store=include_os, stale_after_hours=stale_h,
        # ProbeOverpass is a separate step in every workflow that uses this;
        # probing here too would report a different moment than that step.
        include_overpass=False)
    s = rep["summary"]
    for region, v in sorted(rep["tree"]["regions"].items()):
        if not v.get("present"):
            _log(step_log, f"MISSING from tree: {region} ({v.get('error', 'no reason given')})",
                 "error")
    if s["missing"]:
        _log(step_log, f"missing: {', '.join(s['missing'])}", "error")
    if s["stale"]:
        _log(step_log, f"stale (>{stale_h}h): {', '.join(s['stale'])}", "warning")
    _log(step_log, f"[{rep['status']}] {s['present']}/{s['expected']} present, "
                   f"oldest {_f(s['oldest_age_hours']):.1f}h, "
                   f"Overpass {s['overpass_usable']} usable",
         "success" if rep["status"] == "ok" else "warning")

    return {
        "report": json.dumps(rep),
        "regions": json.dumps(rep["regions_expected"]),
        "expected": int(s["expected"]),
        "present": int(s["present"]),
        "missing_count": len(s["missing"]),
        "stale_count": len(s["stale"]),
        "oldest_age_hours": _f(s["oldest_age_hours"]),
        "status": rep["status"],
    }


def handle_probe_region(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ProbeRegion - one region, optionally with a full-file count."""
    region = params.get("region", "") or ""
    if not region:
        raise ValueError("ProbeRegion requires a region")
    count = bool(params.get("count_features", False))
    local_dir = params.get("local_dir", "") or ""
    step_log = params.get("_step_log")

    fname = extract_inventory.tree_filename(region)
    if local_dir:
        path = os.path.join(local_dir, fname)
        if count:
            _log(step_log, f"ProbeRegion {region}: counting features reads the ENTIRE "
                           f"file (measured 710 s on a 40 GB extract)", "warning")
        rec = extract_inventory.probe_local(path, count_features=count)
    else:
        if count:
            _log(step_log, f"ProbeRegion {region}: no local_dir, so counts are "
                           f"unavailable - probing the remote header only", "warning")
        rec = extract_inventory.probe_remote(f"{extract_inventory.tree_base()}/{fname}")

    _log(step_log,
         f"[{'present' if rec.get('present') else 'MISSING'}] {region} "
         f"seq={rec.get('replication_sequence', '-')} "
         f"age={_f(rec.get('age_hours')):.1f}h",
         "success" if rec.get("present") else "error")
    return {
        "region": region,
        "present": bool(rec.get("present")),
        "size_bytes": _i(rec.get("size_bytes"), 0),
        "replication_sequence": str(rec.get("replication_sequence") or ""),
        "age_hours": _f(rec.get("age_hours")),
        "node_count": _i(rec.get("node_count")),
        "way_count": _i(rec.get("way_count")),
        "relation_count": _i(rec.get("relation_count")),
    }


def handle_survey_subregions(params: dict[str, Any]) -> dict[str, Any]:
    """Handle SurveySubRegions - the country/state/county tier."""
    sample = int(params.get("sample_per_tier", 3) or 3)
    stale_days = float(params.get("stale_after_days", 14.0) or 14.0)
    step_log = params.get("_step_log")
    _log(step_log, f"SurveySubRegions sample_per_tier={sample} stale_after={stale_days}d")

    sub = extract_inventory.survey_subregions(sample_per_tier=sample,
                                              stale_after_days=stale_days)
    if sub.get("error"):
        raise RuntimeError(f"sub-region survey failed: {sub['error']}")

    for tier, v in sorted(sub["tiers"].items(), key=lambda kv: -kv[1]["count"]):
        level = "warning" if v["mtime_stale_count"] else "info"
        _log(step_log,
             f"{tier}: {v['count']:,} objects, {v['bytes'] / 1e9:.1f} GB, "
             f"oldest {v['mtime_oldest_days']:.1f}d (mtime), "
             f"{v['mtime_stale_count']:,} stale, "
             f"{v['sampled_with_replication_timestamp']}/{v['sampled']} sampled carry a "
             f"replication timestamp", level)
    missing = sub.get("tiers_without_data_vintage") or []
    if missing:
        _log(step_log,
             "no data vintage for " + ", ".join(missing) + ": these carry an "
             "osmosis_replication_base_url but NO sequence number, so their diffs cannot "
             "be applied and their age is only the object's last-modified time",
             "warning")
    status = "stale" if sub.get("mtime_stale_count") else "ok"
    _log(step_log,
         f"[{status}] {sub['total_objects']:,} sub-region objects, "
         f"{sub['total_bytes'] / 1e9:.1f} GB, {sub['mtime_stale_count']:,} stale",
         "warning" if status == "stale" else "success")
    return {
        "report": json.dumps(sub),
        "total_objects": int(sub["total_objects"]),
        "total_bytes": int(sub["total_bytes"]),
        "stale_count": int(sub["mtime_stale_count"]),
        "tiers_without_vintage": json.dumps(missing),
        "status": status,
    }


def handle_probe_overpass(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ProbeOverpass - mirror reachability, data currency and quota."""
    step_log = params.get("_step_log")
    st = extract_inventory.overpass_state()
    lags = [m["data_lag_hours"] for m in st["mirrors"]
            if isinstance(m.get("data_lag_hours"), (int, float))]
    for m in st["mirrors"]:
        _log(step_log,
             f"{'usable ' if m['usable'] else 'DOWN   '} {m['endpoint']} "
             f"lag={_f(m.get('data_lag_hours')):.2f}h slots={m.get('slots_available', '-')}"
             + ("" if m["usable"] else
                f" ts_err={m.get('timestamp_error', '-')} status_err={m.get('status_error', '-')}"),
             "info" if m["usable"] else "warning")
    _log(step_log, f"Overpass usable {st['usable_count']}/{st['total_count']}",
         "success" if st["usable_count"] else "warning")
    return {
        "mirrors": json.dumps(st["mirrors"]),
        "usable": int(st["usable_count"]),
        "total": int(st["total_count"]),
        "best_lag_hours": min(lags) if lags else -1.0,
    }


def handle_build_state_report(params: dict[str, Any]) -> dict[str, Any]:
    """Handle BuildStateReport - render HTML + JSON, decide the verdict."""
    survey_raw = params.get("survey") or "{}"
    overpass_raw = params.get("overpass") or "[]"
    dest = params.get("dest", "") or ""
    stale_h = float(params.get("stale_after_hours", 48.0) or 48.0)
    step_log = params.get("_step_log")

    survey = json.loads(survey_raw) if isinstance(survey_raw, str) else survey_raw
    sub_raw = params.get("subregions") or ""
    if sub_raw:
        sub = json.loads(sub_raw) if isinstance(sub_raw, str) else sub_raw
        if isinstance(sub, dict) and sub:
            survey["subregions"] = sub
            survey.setdefault("summary", {}).update(
                subregion_objects=sub.get("total_objects"),
                subregion_stale=sub.get("mtime_stale_count"))
            # A separate verdict on purpose: the sub-region tier refreshes on its
            # own schedule, so folding it into the headline would leave the report
            # permanently red - which is how an alarm stops being read.
            survey["subregion_status"] = "stale" if sub.get("mtime_stale_count") else "ok"
    mirrors = json.loads(overpass_raw) if isinstance(overpass_raw, str) else overpass_raw
    if isinstance(mirrors, list):
        survey.setdefault("overpass", {})["mirrors"] = mirrors
        # Recompute the headline from the mirrors ACTUALLY passed in, so the
        # summary line and the mirror table can never disagree.
        usable = sum(1 for m in mirrors if m.get("usable"))
        survey["overpass"].update(usable_count=usable, total_count=len(mirrors),
                                  not_probed=False)
        survey.setdefault("summary", {})["overpass_usable"] = f"{usable}/{len(mirrors)}"

    html_path, json_path = extract_inventory.render_report(survey, dest=dest)
    s = survey.get("summary", {})
    status = survey.get("status", "unverified")
    detail = (f"{s.get('present', 0)}/{s.get('expected', 0)} regions present; "
              f"missing={len(s.get('missing', []))} stale={len(s.get('stale', []))}; "
              f"oldest {_f(s.get('oldest_age_hours')):.1f}h; "
              f"Overpass {s.get('overpass_usable', '?')} usable")
    if s.get("subregion_objects"):
        detail += (f"; sub-regions {s['subregion_objects']:,} objects, "
                   f"{s.get('subregion_stale', 0):,} stale ({survey.get('subregion_status')})")
    _log(step_log, f"[{status}] {detail} -> {html_path}",
         "success" if status == "ok" else "warning")
    return {"status": status, "html_path": html_path,
            "json_path": json_path, "detail": detail}


_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.SurveyExtracts": handle_survey_extracts,
    f"{NAMESPACE}.ProbeRegion": handle_probe_region,
    f"{NAMESPACE}.SurveySubRegions": handle_survey_subregions,
    f"{NAMESPACE}.ProbeOverpass": handle_probe_overpass,
    f"{NAMESPACE}.BuildStateReport": handle_build_state_report,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner.

    timeout_ms=0: the survey does blocking network I/O and the deep tier reads
    whole multi-GB files, so these rely on the runner's global execution timeout
    rather than a per-handler one.
    """
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_inventory_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
