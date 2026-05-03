"""Road extraction event facet handlers.

Handles road extraction events defined in osmroads.afl.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .road_extractor import (
    RoadFeatures,
    RoadStats,
    calculate_road_stats,
    filter_by_speed_limit,
    filter_roads_by_class,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Roads"


def _make_road_stats_handler(facet_name: str):
    """Create handler for RoadStatistics."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")
        cache = {"path": input_path, "size": payload.get("input_size", 0)}

        # Dynamic cache check (input_path comes from payload)
        hit = cached_result(qualified, cache, {"kind": "stats"}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path}")
        log.info("%s calculating stats for %s", facet_name, input_path)

        if not input_path:
            return {"stats": _empty_stats()}

        try:
            stats = calculate_road_stats(input_path)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.total_roads} roads, {stats.total_length_km:.1f} km",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, cache, {"kind": "stats"}, rv)
            return rv
        except Exception as e:
            log.error("Failed to calculate road stats: %s", e)
            return {"stats": _empty_stats()}

    return handler


def _make_filter_by_class_handler(facet_name: str):
    """Create handler for FilterRoadsByClass."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        road_class = payload.get("road_class", "all")
        step_log = payload.get("_step_log")
        cache = {"path": input_path, "size": payload.get("input_size", 0)}

        # Dynamic cache check (road_class comes from payload)
        hit = cached_result(qualified, cache, {"road_class": road_class}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for {road_class} roads")
        log.info("%s filtering %s for %s roads", facet_name, input_path, road_class)

        if not input_path:
            return {"result": _empty_result(road_class)}

        try:
            result = filter_roads_by_class(input_path, road_class)
            if step_log:
                step_log(
                    f"{facet_name}: filtered to {result.feature_count} {road_class} roads",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, {"road_class": road_class}, rv)
            return rv
        except Exception as e:
            log.error("Failed to filter roads: %s", e)
            return {"result": _empty_result(road_class)}

    return handler


def _make_filter_by_speed_handler(facet_name: str):
    """Create handler for FilterBySpeedLimit."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        min_speed = payload.get("min_speed", 0)
        max_speed = payload.get("max_speed", 999)
        step_log = payload.get("_step_log")
        cache = {"path": input_path, "size": payload.get("input_size", 0)}

        # Dynamic cache check (speed range comes from payload)
        hit = cached_result(
            qualified, cache, {"min_speed": min_speed, "max_speed": max_speed}, step_log
        )
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for speed {min_speed}-{max_speed}")
        log.info("%s filtering %s for speed %d-%d", facet_name, input_path, min_speed, max_speed)

        if not input_path:
            return {"result": _empty_result("filtered")}

        try:
            result = filter_by_speed_limit(input_path, min_speed, max_speed)
            if step_log:
                step_log(
                    f"{facet_name}: filtered to {result.feature_count} roads (speed {min_speed}-{max_speed})",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, {"min_speed": min_speed, "max_speed": max_speed}, rv)
            return rv
        except Exception as e:
            log.error("Failed to filter by speed limit: %s", e)
            return {"result": _empty_result("filtered")}

    return handler


def _result_to_dict(result: RoadFeatures) -> dict:
    """Convert RoadFeatures to dict."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "road_class": result.road_class,
        "total_length_km": result.total_length_km,
        "with_speed_limit": result.with_speed_limit,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: RoadStats) -> dict:
    """Convert RoadStats to dict."""
    return {
        "total_roads": stats.total_roads,
        "total_length_km": stats.total_length_km,
        "motorway_km": stats.motorway_km,
        "primary_km": stats.primary_km,
        "secondary_km": stats.secondary_km,
        "tertiary_km": stats.tertiary_km,
        "residential_km": stats.residential_km,
        "other_km": stats.other_km,
        "with_speed_limit": stats.with_speed_limit,
        "with_surface": stats.with_surface,
        "with_lanes": stats.with_lanes,
        "one_way_count": stats.one_way_count,
    }


def _empty_result(road_class: str) -> dict:
    """Return empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "road_class": road_class,
        "total_length_km": 0.0,
        "with_speed_limit": 0,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "total_roads": 0,
        "total_length_km": 0.0,
        "motorway_km": 0.0,
        "primary_km": 0.0,
        "secondary_km": 0.0,
        "tertiary_km": 0.0,
        "residential_km": 0.0,
        "other_km": 0.0,
        "with_speed_limit": 0,
        "with_surface": 0,
        "with_lanes": 0,
        "one_way_count": 0,
    }


ROAD_FACETS = [
    # Statistics and filtering
    ("RoadStatistics", _make_road_stats_handler),
    ("FilterRoadsByClass", _make_filter_by_class_handler),
    ("FilterBySpeedLimit", _make_filter_by_speed_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in ROAD_FACETS:
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


def register_road_handlers(poller) -> None:
    """Register all road event facet handlers."""
    for facet_name, handler_factory in ROAD_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered road handler: %s", qualified_name)
