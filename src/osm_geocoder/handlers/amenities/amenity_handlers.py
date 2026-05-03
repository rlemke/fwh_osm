"""Amenity extraction event facet handlers.

Handles amenity extraction events defined in osmamenities.afl.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .amenity_extractor import (
    AmenityFeatures,
    AmenityStats,
    calculate_amenity_stats,
    search_amenities,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Amenities"


def _make_amenity_stats_handler(facet_name: str):
    """Create handler for AmenityStatistics."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path}")
        log.info("%s calculating stats for %s", facet_name, input_path)

        if not input_path:
            return {"stats": _empty_stats()}

        cache = {"path": input_path, "size": _file_size(input_path)}
        hit = cached_result(qualified, cache, {}, step_log)
        if hit is not None:
            return hit

        try:
            stats = calculate_amenity_stats(input_path)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.total_amenities} amenities"
                    f" (food={stats.food}, shopping={stats.shopping}, healthcare={stats.healthcare})",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, cache, {}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to calculate amenity stats: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to calculate amenity stats: {exc}", level="error")
            raise

    return handler


def _make_search_amenities_handler(facet_name: str):
    """Create handler for SearchAmenities."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        name_pattern = payload.get("name_pattern", ".*")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: searching {input_path} for pattern '{name_pattern}'")
        log.info("%s searching %s for pattern '%s'", facet_name, input_path, name_pattern)

        if not input_path:
            return {"result": _empty_result("search")}

        cache = {"path": input_path, "size": _file_size(input_path)}
        hit = cached_result(qualified, cache, {"name_pattern": name_pattern}, step_log)
        if hit is not None:
            return hit

        try:
            result = search_amenities(input_path, name_pattern)
            if step_log:
                step_log(
                    f"{facet_name}: found {result.feature_count} matching '{name_pattern}'",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, {"name_pattern": name_pattern}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to search amenities: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to search amenities: {exc}", level="error")
            raise

    return handler


def _make_filter_by_category_handler(facet_name: str):
    """Create handler for FilterByCategory."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        category = payload.get("category", "all")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for category {category}")
        log.info("%s filtering %s for category %s", facet_name, input_path, category)

        if not input_path:
            return {"result": _empty_result(category)}

        cache = {"path": input_path, "size": _file_size(input_path)}
        hit = cached_result(qualified, cache, {"category": category}, step_log)
        if hit is not None:
            return hit

        try:
            import json
            import posixpath

            from facetwork.runtime.storage import get_storage_backend

            from ..shared._output import uri_stem

            input_path = str(input_path)
            _st = get_storage_backend(input_path)
            with _st.open(input_path, "r") as f:
                geojson = json.load(f)

            filtered = []
            for feature in geojson.get("features", []):
                props = feature.get("properties", {})
                if category == "all" or props.get("category") == category:
                    filtered.append(feature)

            _dir = posixpath.dirname(input_path)
            output_path = f"{_dir}/{uri_stem(input_path)}_{category}.geojson"
            output_geojson = {"type": "FeatureCollection", "features": filtered}

            with _st.open(output_path, "w") as f:
                json.dump(output_geojson, f, indent=2)

            all_features = geojson.get("features", [])
            if step_log:
                step_log(
                    f"{facet_name}: {len(filtered)}/{len(all_features)} {category} amenities",
                    level="success",
                )
            rv = {
                "result": {
                    "output_path": str(output_path),
                    "feature_count": len(filtered),
                    "amenity_category": category,
                    "amenity_types": category,
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }
            save_result_meta(qualified, cache, {"category": category}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter amenities: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter amenities: {exc}", level="error")
            raise

    return handler


def _file_size(path: str) -> int:
    """Return file size or 0 if unavailable."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _result_to_dict(result: AmenityFeatures) -> dict:
    """Convert AmenityFeatures to dict."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "amenity_category": result.amenity_category,
        "amenity_types": result.amenity_types,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: AmenityStats) -> dict:
    """Convert AmenityStats to dict."""
    return {
        "total_amenities": stats.total_amenities,
        "food": stats.food,
        "shopping": stats.shopping,
        "services": stats.services,
        "healthcare": stats.healthcare,
        "education": stats.education,
        "entertainment": stats.entertainment,
        "transport": stats.transport,
        "other": stats.other,
        "with_name": stats.with_name,
        "with_opening_hours": stats.with_opening_hours,
    }


def _empty_result(category: str) -> dict:
    """Return empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "amenity_category": category,
        "amenity_types": category,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "total_amenities": 0,
        "food": 0,
        "shopping": 0,
        "services": 0,
        "healthcare": 0,
        "education": 0,
        "entertainment": 0,
        "transport": 0,
        "other": 0,
        "with_name": 0,
        "with_opening_hours": 0,
    }


AMENITY_FACETS = [
    # Statistics and filtering
    ("AmenityStatistics", _make_amenity_stats_handler),
    ("SearchAmenities", _make_search_amenities_handler),
    ("FilterByCategory", _make_filter_by_category_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in AMENITY_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = handler_factory(facet_name)


_build_dispatch()


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


def register_amenity_handlers(poller) -> None:
    """Register all amenity event facet handlers."""
    for facet_name, handler_factory in AMENITY_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered amenity handler: %s", qualified_name)
