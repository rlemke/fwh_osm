"""Validation event facet handlers for OSM cache quality checks.

Handles ValidateCache, ValidateGeometry, ValidateTags, ValidateBounds, and
ValidationSummary event facets defined in osmvalidation.afl under the
osm.ops.Validation namespace.

Delegates to the OSMOSE verifier for actual PBF analysis.
"""

import logging
import os
from datetime import UTC, datetime

from .osmose_verifier import VerifyResult, VerifySummaryData, compute_verify_summary, verify_pbf

log = logging.getLogger(__name__)

NAMESPACE = "osm.ops.Validation"


def _stats_dict(summary: VerifySummaryData) -> dict:
    """Build a ValidationStats dict from verify summary data."""
    return {
        "total_entries": summary.total_issues,
        "valid_entries": max(
            0, summary.total_issues - summary.geometry_issues - summary.tag_issues
        ),
        "invalid_entries": summary.geometry_issues + summary.tag_issues,
        "missing_tags": summary.tag_issues,
        "invalid_geometry": summary.geometry_issues,
        "out_of_bounds": 0,
        "duplicate_entries": summary.reference_issues,
        "validation_date": datetime.now(UTC).isoformat(),
    }


def _result_dict(result: VerifyResult) -> dict:
    """Build a ValidationResult dict from verify result."""
    return {
        "output_path": result.output_path,
        "feature_count": result.issue_count,
        "format": "GeoJSON",
        "validation_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    return {
        "total_entries": 0,
        "valid_entries": 0,
        "invalid_entries": 0,
        "missing_tags": 0,
        "invalid_geometry": 0,
        "out_of_bounds": 0,
        "duplicate_entries": 0,
        "validation_date": datetime.now(UTC).isoformat(),
    }


def _empty_result() -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "format": "GeoJSON",
        "validation_date": datetime.now(UTC).isoformat(),
    }


def handle_validate_cache(payload: dict) -> dict:
    """Full validation of an OSM cache file."""
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    from facetwork.config import get_output_base

    output_dir = payload.get("output_dir", os.path.join(get_output_base(), "osm", "validation"))
    step_log = payload.get("_step_log")

    if not pbf_path:
        log.warning("No PBF path in cache; returning empty validation")
        return {"stats": _empty_stats(), "result": _empty_result()}

    if step_log:
        step_log(f"ValidateCache: validating {pbf_path}")
    log.info("ValidateCache: validating %s", pbf_path)
    result, summary = verify_pbf(pbf_path, output_dir)
    if step_log:
        step_log(
            f"ValidateCache: {summary.total_issues} issues (geometry={summary.geometry_issues}, tags={summary.tag_issues})",
            level="success",
        )
    return {"stats": _stats_dict(summary), "result": _result_dict(result)}


def handle_validate_geometry(payload: dict) -> dict:
    """Geometry-only validation."""
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"ValidateGeometry: validating geometry of {pbf_path}")

    if not pbf_path:
        return {"stats": _empty_stats(), "result": _empty_result()}

    result, summary = verify_pbf(
        pbf_path,
        check_geometry=True,
        check_tags=False,
        check_references=False,
        check_coordinates=False,
        check_duplicates=False,
    )
    if step_log:
        step_log(f"ValidateGeometry: {summary.geometry_issues} geometry issues", level="success")
    return {"stats": _stats_dict(summary), "result": _result_dict(result)}


def handle_validate_tags(payload: dict) -> dict:
    """Tag-only validation."""
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"ValidateTags: validating tags of {pbf_path}")

    if not pbf_path:
        return {"stats": _empty_stats(), "result": _empty_result()}

    result, summary = verify_pbf(
        pbf_path,
        check_geometry=False,
        check_tags=True,
        check_references=False,
        check_coordinates=False,
        check_duplicates=False,
    )
    if step_log:
        step_log(f"ValidateTags: {summary.tag_issues} tag issues", level="success")
    return {"stats": _stats_dict(summary), "result": _result_dict(result)}


def handle_validate_bounds(payload: dict) -> dict:
    """Coordinate bounds validation."""
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "") if isinstance(cache, dict) else ""
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"ValidateBounds: validating coordinate bounds of {pbf_path}")

    if not pbf_path:
        return {"stats": _empty_stats(), "result": _empty_result()}

    result, summary = verify_pbf(
        pbf_path,
        check_geometry=False,
        check_tags=False,
        check_references=False,
        check_coordinates=True,
        check_duplicates=False,
    )
    if step_log:
        step_log(f"ValidateBounds: {summary.total_issues} coordinate issues", level="success")
    return {"stats": _stats_dict(summary), "result": _result_dict(result)}


def handle_validation_summary(payload: dict) -> dict:
    """Compute summary from a validation result file."""
    input_path = payload.get("input_path", "")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"ValidationSummary: computing summary for {input_path}")

    if not input_path:
        return {"stats": _empty_stats()}

    summary = compute_verify_summary(input_path)
    if step_log:
        step_log(f"ValidationSummary: {summary.total_issues} total issues", level="success")
    return {"stats": _stats_dict(summary)}


# RegistryRunner dispatch adapter
_DISPATCH = {
    f"{NAMESPACE}.ValidateCache": handle_validate_cache,
    f"{NAMESPACE}.ValidateGeometry": handle_validate_geometry,
    f"{NAMESPACE}.ValidateTags": handle_validate_tags,
    f"{NAMESPACE}.ValidateBounds": handle_validate_bounds,
    f"{NAMESPACE}.ValidationSummary": handle_validation_summary,
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


def register_validation_handlers(poller) -> None:
    """Register all validation event facet handlers with the poller."""
    poller.register(f"{NAMESPACE}.ValidateCache", handle_validate_cache)
    poller.register(f"{NAMESPACE}.ValidateGeometry", handle_validate_geometry)
    poller.register(f"{NAMESPACE}.ValidateTags", handle_validate_tags)
    poller.register(f"{NAMESPACE}.ValidateBounds", handle_validate_bounds)
    poller.register(f"{NAMESPACE}.ValidationSummary", handle_validation_summary)
    log.debug("Registered validation handlers: %s.*", NAMESPACE)
