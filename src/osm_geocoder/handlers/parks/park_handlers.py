"""Park extraction event facet handlers.

Handles park extraction events defined in osmparks.afl under osm.Parks.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .park_extractor import (
    ParkFeatures,
    ParkStats,
    calculate_park_stats,
    filter_parks_by_type,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Parks"


def _make_filter_parks_handler(facet_name: str):
    """Create handler for FilterParksByType event facet.

    Uses dynamic cache_params since park_type and protect_classes come from payload.
    Input is a GeoJSON file (input_path) rather than a PBF cache.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        park_type = payload.get("park_type", "all")
        protect_classes = payload.get("protect_classes", "*")
        step_log = payload.get("_step_log")

        # Dynamic cache check — use input_path as cache identity
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"park_type": park_type, "protect_classes": protect_classes}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for {park_type} parks")
        log.info(
            "%s filtering %s for %s parks (classes=%s)",
            facet_name,
            input_path,
            park_type,
            protect_classes,
        )

        if not input_path:
            return {"result": _empty_result(park_type, protect_classes)}

        try:
            result = filter_parks_by_type(
                input_path,
                park_type=park_type,
                protect_classes=protect_classes,
            )
            if step_log:
                step_log(
                    f"{facet_name}: filtered to {result.feature_count} {park_type} parks",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, input_cache, cp, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter parks: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter parks: {exc}", level="error")
            raise

    return handler


def _make_park_stats_handler(facet_name: str):
    """Create handler for ParkStatistics event facet.

    Input is a GeoJSON file (input_path) rather than a PBF cache.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")

        # Dynamic cache check — use input_path as cache identity
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp: dict[str, str] = {}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path}")
        log.info("%s calculating stats for %s", facet_name, input_path)

        if not input_path:
            return {"stats": _empty_stats()}

        try:
            stats = calculate_park_stats(input_path)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.total_parks} parks, {stats.total_area_km2:.1f} km2"
                    f" (national={stats.national_parks}, state={stats.state_parks},"
                    f" reserves={stats.nature_reserves})",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, input_cache, cp, rv)
            return rv
        except Exception as exc:
            log.error("Failed to calculate park stats: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to calculate park stats: {exc}", level="error")
            raise

    return handler


def _file_size(path: str) -> int:
    """Return file size in bytes, or 0 if the file doesn't exist."""
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _result_to_dict(result: ParkFeatures) -> dict:
    """Convert a ParkFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "park_type": result.park_type,
        "protect_classes": result.protect_classes,
        "total_area_km2": result.total_area_km2,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: ParkStats) -> dict:
    """Convert ParkStats to a dictionary."""
    return {
        "total_parks": stats.total_parks,
        "total_area_km2": stats.total_area_km2,
        "national_parks": stats.national_parks,
        "state_parks": stats.state_parks,
        "nature_reserves": stats.nature_reserves,
        "other_protected": stats.other_protected,
        "park_type": stats.park_type,
    }


def _empty_result(park_type: str, protect_classes: str) -> dict:
    """Return an empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "park_type": park_type,
        "protect_classes": protect_classes,
        "total_area_km2": 0.0,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "total_parks": 0,
        "total_area_km2": 0.0,
        "national_parks": 0,
        "state_parks": 0,
        "nature_reserves": 0,
        "other_protected": 0,
        "park_type": "",
    }


# Event facet definitions for handler registration
PARK_FACETS = [
    ("FilterParksByType", _make_filter_parks_handler),
    ("ParkStatistics", _make_park_stats_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in PARK_FACETS:
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


def register_park_handlers(poller) -> None:
    """Register all park event facet handlers with the poller."""
    for facet_name, handler_factory in PARK_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered park handler: %s", qualified_name)
