"""AFL handler registration for CombinedScan and ExtractCategoryResult."""

import json
import logging
import os

from ..shared.output_cache import cached_result, save_result_meta
from .combined_handler import HAS_OSMIUM, combined_scan

log = logging.getLogger(__name__)

NAMESPACE = "osm.Combined"

SCAN_FACET = "CombinedScan"
SCAN_QUALIFIED = f"{NAMESPACE}.{SCAN_FACET}"

EXTRACT_FACET = "ExtractCategoryResult"
EXTRACT_QUALIFIED = f"{NAMESPACE}.{EXTRACT_FACET}"


def _handler(payload: dict) -> dict:
    """Handle a CombinedScan event."""
    cache = payload.get("cache", {})
    pbf_path = cache.get("path", "")
    categories = payload.get("categories", [])
    step_log = payload.get("_step_log")

    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",") if c.strip()]

    cache_params = {"categories": sorted(categories)}
    hit = cached_result(SCAN_QUALIFIED, cache, cache_params, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"CombinedScan: scanning {pbf_path} for {categories}")
    log.info("CombinedScan: %s categories=%s", pbf_path, categories)

    if not HAS_OSMIUM or not pbf_path:
        return _empty_result(categories)

    try:
        result = combined_scan(pbf_path, categories, step_log=step_log)

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
