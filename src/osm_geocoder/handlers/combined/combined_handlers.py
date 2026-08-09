"""AFL handler registration for CombinedScan and ExtractCategoryResult."""

import json
import logging
import os

from ..shared._output import resolve_output_dir
from ..shared.output_cache import cached_result, save_result_meta
from .combined_handler import HAS_OSMIUM, combined_scan

log = logging.getLogger(__name__)

_REMOTE_SCHEMES = ("s3://", "hdfs://")


def _scan_outputs_usable(rv: dict, step_log=None) -> bool:
    """Guard a cached scan manifest against a storage-backend change.

    A manifest records per-category ``output_path``s nested in its ``results``
    JSON (which ``cached_result``'s top-level existence check never sees). If
    those were written under a different backend than the one in effect now —
    e.g. local paths cached before ``FW_STORAGE=s3`` — handing them downstream
    fails: a local path is unreadable as an S3 key, and a deleted artifact is a
    dead path. Returns False (→ re-scan) when any cached output is on a different
    backend than where a fresh scan would write, or no longer exists there."""
    from facetwork.runtime.storage import get_storage_backend

    cur_remote = resolve_output_dir("osm-combined").startswith(_REMOTE_SCHEMES)
    try:
        results = json.loads(rv.get("results", "{}"))
    except (TypeError, ValueError):
        return False
    for cat in results.values():
        op = (cat or {}).get("output_path") if isinstance(cat, dict) else None
        if not op:
            continue
        if op.startswith(_REMOTE_SCHEMES) != cur_remote:
            if step_log:
                step_log(
                    f"CombinedScan: cached output on a different storage backend "
                    f"({op}) — re-scanning", level="warning")
            return False
        try:
            if not get_storage_backend(op).exists(op):
                if step_log:
                    step_log(f"CombinedScan: cached output missing ({op}) — re-scanning",
                             level="warning")
                return False
        except Exception:
            return False  # path not resolvable in the current backend → re-scan
    return True


NAMESPACE = "osm.Combined"

SCAN_FACET = "CombinedScan"
SCAN_QUALIFIED = f"{NAMESPACE}.{SCAN_FACET}"

EXTRACT_FACET = "ExtractCategoryResult"
EXTRACT_QUALIFIED = f"{NAMESPACE}.{EXTRACT_FACET}"


def ensure_scan(
    cache: dict,
    categories: list[str],
    step_log=None,
    heartbeat=None,
    cancel_check=None,
) -> dict:
    """Run (or reuse a cached) single-pass CombinedScan for ``categories``.

    Returns the CombinedScan return dict (``results`` JSON string + totals).
    The scan manifest is cached per ``(region, sorted(categories))`` via the
    output-cache sidecar, so a repeat call on the same region is an instant
    lookup — a single osmium pass extracts many categories for ~the cost of one.

    Shared by the ``osm.Combined.CombinedScan`` facet and the
    ``osm.Source.PBF.ExtractCategory`` facade so both hit the same cache.
    """
    pbf_path = cache.get("path", "")
    cache_params = {"categories": sorted(categories)}
    hit = cached_result(SCAN_QUALIFIED, cache, cache_params, step_log)
    if hit is not None and _scan_outputs_usable(hit, step_log):
        return hit

    if step_log:
        step_log(f"CombinedScan: scanning {pbf_path} for {categories}")
    log.info("CombinedScan: %s categories=%s", pbf_path, categories)

    if not HAS_OSMIUM or not pbf_path:
        return _empty_result(categories)

    try:
        result = combined_scan(
            pbf_path,
            categories,
            step_log=step_log,
            heartbeat=heartbeat,
            cancel_check=cancel_check,
        )

        # Serialize per-category results to JSON string for AFL
        results_dict = {}
        for cat, pr in result.results.items():
            results_dict[cat] = {
                "output_path": pr.output_path,
                "feature_count": pr.feature_count,
                "metadata": pr.metadata,
                "error": pr.error,
            }

        if step_log:
            step_log(
                f"CombinedScan: {result.total_features} features "
                f"from {len(categories)} categories in {result.scan_duration_seconds}s",
                level="success",
            )

        rv = {
            "results": json.dumps(results_dict),
            "total_features": result.total_features,
            "scan_duration": result.scan_duration_seconds,
            "category_count": len(categories),
        }
        save_result_meta(SCAN_QUALIFIED, cache, cache_params, rv)
        return rv

    except Exception as e:
        log.error("CombinedScan failed: %s", e)
        if step_log:
            step_log(f"CombinedScan: failed: {e}", level="error")
        return _empty_result(categories)


def _handler(payload: dict) -> dict:
    """Handle a CombinedScan event."""
    cache = payload.get("cache", {})
    categories = payload.get("categories", [])
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]
    return ensure_scan(
        cache,
        categories,
        payload.get("_step_log"),
        payload.get("_task_heartbeat"),
        payload.get("_cancellation_check"),
    )


def _extract_handler(payload: dict) -> dict:
    """Handle an ExtractCategoryResult event.

    Parses the JSON results string from CombinedScan and returns the
    output_path and feature_count for the requested category.
    """
    results_json = payload.get("results", "{}")
    category = payload.get("category", "")
    step_log = payload.get("_step_log")

    try:
        results = json.loads(results_json) if isinstance(results_json, str) else results_json
    except (json.JSONDecodeError, TypeError):
        log.error("ExtractCategoryResult: invalid JSON results")
        if step_log:
            step_log("ExtractCategoryResult: invalid JSON results", level="error")
        return {"output_path": "", "feature_count": 0}

    cat_data = results.get(category, {})
    output_path = cat_data.get("output_path", "")
    feature_count = cat_data.get("feature_count", 0)

    if step_log:
        step_log(
            f"ExtractCategoryResult: {category} → {feature_count} features at {output_path}",
            level="success" if output_path else "warning",
        )

    return {"output_path": output_path, "feature_count": feature_count}


def _empty_result(categories: list[str]) -> dict:
    return {
        "results": "{}",
        "total_features": 0,
        "scan_duration": 0.0,
        "category_count": len(categories),
    }


def register_combined_handlers(poller) -> None:
    """Register with AgentPoller."""
    if not HAS_OSMIUM:
        return
    poller.register(SCAN_QUALIFIED, _handler)
    poller.register(EXTRACT_QUALIFIED, _extract_handler)
    log.debug("Registered combined handlers: %s, %s", SCAN_QUALIFIED, EXTRACT_QUALIFIED)


# RegistryRunner dispatch adapter
_DISPATCH = {
    SCAN_QUALIFIED: _handler,
    EXTRACT_QUALIFIED: _extract_handler,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload.get("_facet_name", SCAN_QUALIFIED)
    handler_fn = _DISPATCH.get(facet_name)
    if handler_fn is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler_fn(payload)


def register_handlers(runner) -> None:
    """Register with RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )
