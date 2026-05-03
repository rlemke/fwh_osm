"""OSMOSE local verifier event facet handlers.

Thin wrappers that delegate to osmose_verifier for the VerifyAll,
VerifyGeometry, VerifyTags, VerifyGeoJSON, and VerifySummary event facets
defined in osmosmose.afl under osm.ops.OSMOSE namespace.

Performs deep quality analysis directly on .osm.pbf and GeoJSON files with
no network dependency.
"""

import logging
import os
from dataclasses import asdict
from typing import Any

from .osmose_verifier import (
    VerifyResult,
    VerifySummaryData,
    compute_verify_summary,
    verify_geojson,
    verify_pbf,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.ops.OSMOSE"


def _result_dict(result: VerifyResult) -> dict[str, Any]:
    """Convert a VerifyResult dataclass to a handler return dict."""
    return asdict(result)


def _summary_dict(summary: VerifySummaryData) -> dict[str, Any]:
    """Convert a VerifySummaryData dataclass to a handler return dict."""
    return asdict(summary)


def handle_verify_all(payload: dict) -> dict:
    """Run full verification on a PBF cache file.

    Params:
        cache: OSMCache dict with 'path' key pointing to the .osm.pbf file
        output_dir: Base output directory (default AFL_OUTPUT_BASE/osm/osmose)
        check_geometry: Enable geometry checks (default true)
        check_tags: Enable tag checks (default true)
        check_references: Enable reference integrity checks (default true)
        check_coordinates: Enable coordinate range checks (default true)
        check_duplicates: Enable duplicate ID checks (default true)

    Returns:
        result: VerifyResult dict
        summary: VerifySummary dict
    """
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")
    if not pbf_path:
        log.warning("No PBF path in cache; returning empty result")
        return {
            "result": _result_dict(VerifyResult("", 0, 0, 0, 0)),
            "summary": _summary_dict(VerifySummaryData()),
        }

    if step_log:
        step_log(f"VerifyAll: running full verification on {pbf_path}")

    from facetwork.config import get_output_base

    output_dir = payload.get("output_dir", os.path.join(get_output_base(), "osm", "osmose"))

    result, summary = verify_pbf(
        pbf_path,
        output_dir,
        check_geometry=payload.get("check_geometry", True),
        check_tags=payload.get("check_tags", True),
        check_references=payload.get("check_references", True),
        check_coordinates=payload.get("check_coordinates", True),
        check_duplicates=payload.get("check_duplicates", True),
    )

    if step_log:
        step_log(
            f"VerifyAll: {result.issue_count} issues (geometry={summary.geometry_issues}, tags={summary.tag_issues})",
            level="success",
        )
    return {"result": _result_dict(result), "summary": _summary_dict(summary)}


def handle_verify_geometry(payload: dict) -> dict:
    """Run geometry-only verification on a PBF cache file.

    Params:
        cache: OSMCache dict with 'path' key

    Returns:
        result: VerifyResult dict
        summary: VerifySummary dict
    """
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")
    if not pbf_path:
        log.warning("No PBF path in cache; returning empty result")
        return {
            "result": _result_dict(VerifyResult("", 0, 0, 0, 0)),
            "summary": _summary_dict(VerifySummaryData()),
        }

    if step_log:
        step_log(f"VerifyGeometry: verifying geometry of {pbf_path}")

    result, summary = verify_pbf(
        pbf_path,
        check_geometry=True,
        check_tags=False,
        check_references=False,
        check_coordinates=False,
        check_duplicates=False,
    )

    if step_log:
        step_log(f"VerifyGeometry: {result.issue_count} geometry issues", level="success")
    return {"result": _result_dict(result), "summary": _summary_dict(summary)}


def handle_verify_tags(payload: dict) -> dict:
    """Run tag-only verification on a PBF cache file.

    Params:
        cache: OSMCache dict with 'path' key
        required_tags: Comma-separated list of required tag names (default "name")

    Returns:
        result: VerifyResult dict
        summary: VerifySummary dict
    """
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")
    if not pbf_path:
        log.warning("No PBF path in cache; returning empty result")
        return {
            "result": _result_dict(VerifyResult("", 0, 0, 0, 0)),
            "summary": _summary_dict(VerifySummaryData()),
        }

    if step_log:
        step_log(f"VerifyTags: verifying tags of {pbf_path}")

    required_tags_str = payload.get("required_tags", "name")
    required_tags = [t.strip() for t in required_tags_str.split(",") if t.strip()]

    result, summary = verify_pbf(
        pbf_path,
        check_geometry=False,
        check_tags=True,
        check_references=False,
        check_coordinates=False,
        check_duplicates=False,
        required_tags=required_tags,
    )

    if step_log:
        step_log(f"VerifyTags: {result.issue_count} tag issues", level="success")
    return {"result": _result_dict(result), "summary": _summary_dict(summary)}


def handle_verify_geojson(payload: dict) -> dict:
    """Verify a GeoJSON file for structure, geometry, and coordinate validity.

    Params:
        input_path: Path to the GeoJSON file

    Returns:
        result: VerifyResult dict
        summary: VerifySummary dict
    """
    input_path = payload.get("input_path", "")
    step_log = payload.get("_step_log")
    if not input_path:
        log.warning("No input_path provided; returning empty result")
        return {
            "result": _result_dict(VerifyResult("", 0, 0, 0, 0)),
            "summary": _summary_dict(VerifySummaryData()),
        }

    if step_log:
        step_log(f"VerifyGeoJSON: verifying {input_path}")

    result, summary = verify_geojson(input_path)

    if step_log:
        step_log(f"VerifyGeoJSON: {result.issue_count} issues", level="success")
    return {"result": _result_dict(result), "summary": _summary_dict(summary)}


def handle_verify_summary(payload: dict) -> dict:
    """Compute aggregate statistics from a verification GeoJSON output.

    Params:
        input_path: Path to verify-issues.geojson

    Returns:
        summary: VerifySummary dict
    """
    input_path = payload.get("input_path", "")
    step_log = payload.get("_step_log")
    if not input_path:
        log.warning("No input_path provided; returning empty summary")
        return {"summary": _summary_dict(VerifySummaryData())}

    if step_log:
        step_log(f"ComputeVerifySummary: computing summary for {input_path}")

    summary = compute_verify_summary(input_path)

    if step_log:
        step_log(f"ComputeVerifySummary: {summary.total_issues} total issues", level="success")
    return {"summary": _summary_dict(summary)}


# RegistryRunner dispatch adapter
_DISPATCH = {
    f"{NAMESPACE}.VerifyAll": handle_verify_all,
    f"{NAMESPACE}.VerifyGeometry": handle_verify_geometry,
    f"{NAMESPACE}.VerifyTags": handle_verify_tags,
    f"{NAMESPACE}.VerifyGeoJSON": handle_verify_geojson,
    f"{NAMESPACE}.ComputeVerifySummary": handle_verify_summary,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_osmose_handlers(poller) -> None:
    """Register OSMOSE local verifier handlers with the poller."""
    poller.register(f"{NAMESPACE}.VerifyAll", handle_verify_all)
    poller.register(f"{NAMESPACE}.VerifyGeometry", handle_verify_geometry)
    poller.register(f"{NAMESPACE}.VerifyTags", handle_verify_tags)
    poller.register(f"{NAMESPACE}.VerifyGeoJSON", handle_verify_geojson)
    poller.register(f"{NAMESPACE}.ComputeVerifySummary", handle_verify_summary)
    log.debug("Registered OSMOSE verifier handlers: %s.*", NAMESPACE)
