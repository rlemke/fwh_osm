"""Route extraction event facet handlers.

Handles route extraction events defined in osmroutes.afl under osm.Routes.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .route_extractor import (
    RouteFeatures,
    RouteStats,
    calculate_route_stats,
    filter_routes_by_type,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routes"


def _make_filter_routes_handler(facet_name: str):
    """Create handler for FilterRoutesByType event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        route_type = payload.get("route_type", "bicycle")
        network = payload.get("network", "*")
        step_log = payload.get("_step_log")

        # Build synthetic cache dict from input_path for cache key
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        dyn_params = {"route_type": route_type, "network": network}
        hit = cached_result(qualified, input_cache, dyn_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for {route_type} routes")
        log.info(
            "%s filtering %s for %s routes (network=%s)",
            facet_name,
            input_path,
            route_type,
            network,
        )

        if not input_path:
            return {"result": _empty_result(route_type, network, False)}

        try:
            result = filter_routes_by_type(
                input_path,
                route_type=route_type,
                network=network,
                heartbeat=payload.get("_task_heartbeat"),
                task_uuid=payload.get("_task_uuid", ""),
            )
            if step_log:
                step_log(
                    f"{facet_name}: filtered to {result.feature_count} {route_type} routes",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, input_cache, dyn_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter routes: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter routes: {exc}", level="error")
            raise

    return handler


def _make_route_stats_handler(facet_name: str):
    """Create handler for RouteStatistics event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")

        # Build synthetic cache dict from input_path for cache key
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        dyn_params: dict = {}
        hit = cached_result(qualified, input_cache, dyn_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path}")
        log.info("%s calculating stats for %s", facet_name, input_path)

        if not input_path:
            return {"stats": _empty_stats()}

        try:
            stats = calculate_route_stats(
                input_path,
                heartbeat=payload.get("_task_heartbeat"),
                task_uuid=payload.get("_task_uuid", ""),
            )
            if step_log:
                step_log(
                    f"{facet_name}: {stats.route_count} routes, {stats.total_length_km:.1f} km",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, input_cache, dyn_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to calculate stats: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to calculate stats: {exc}", level="error")
            raise

    return handler


def _file_size(path: str) -> int:
    """Get file size, returning 0 if the file doesn't exist or is remote."""
    if not path or path.startswith("hdfs://"):
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _result_to_dict(result: RouteFeatures) -> dict:
    """Convert a RouteFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "route_type": result.route_type,
        "network_level": result.network_level,
        "include_infrastructure": result.include_infrastructure,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: RouteStats) -> dict:
    """Convert RouteStats to a dictionary."""
    return {
        "route_count": stats.route_count,
        "total_length_km": stats.total_length_km,
        "infrastructure_count": stats.infrastructure_count,
        "route_type": stats.route_type,
    }


def _empty_result(route_type: str, network: str, include_infra: bool) -> dict:
    """Return an empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "route_type": route_type,
        "network_level": network,
        "include_infrastructure": include_infra,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "route_count": 0,
        "total_length_km": 0.0,
        "infrastructure_count": 0,
        "route_type": "",
    }


# Event facet definitions for handler registration
ROUTE_FACETS = [
    ("FilterRoutesByType", _make_filter_routes_handler),
    ("RouteStatistics", _make_route_stats_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in ROUTE_FACETS:
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


def register_route_handlers(poller) -> None:
    """Register all route event facet handlers with the poller."""
    for facet_name, handler_factory in ROUTE_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered route handler: %s", qualified_name)
