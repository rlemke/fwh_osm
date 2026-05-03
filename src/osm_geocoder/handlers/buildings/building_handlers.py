"""Building extraction event facet handlers.

Handles building extraction events defined in osmbuildings.afl.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .building_extractor import (
    BuildingStats,
    calculate_building_stats,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Buildings"


def _make_building_stats_handler(facet_name: str):
    """Create handler for BuildingStatistics."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")

        # Build cache dict from input_path for cache key computation
        input_cache = {"path": input_path, "size": 0}
        if input_path:
            try:
                backend = __import__(
                    "facetwork.runtime.storage", fromlist=["get_storage_backend"]
                ).get_storage_backend(input_path)
                input_cache["size"] = backend.getsize(input_path)
            except Exception:
                pass

        hit = cached_result(qualified, input_cache, {"stats": True}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path}")
        log.info("%s calculating stats for %s", facet_name, input_path)

        if not input_path:
            return {"stats": _empty_stats()}

        try:
            stats = calculate_building_stats(input_path)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.total_buildings} buildings, {stats.total_area_km2:.2f} km2"
                    f" (residential={stats.residential}, commercial={stats.commercial})",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, input_cache, {"stats": True}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to calculate building stats: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to calculate building stats: {exc}", level="error")
            raise

    return handler


def _make_filter_buildings_handler(facet_name: str):
    """Create handler for FilterBuildingsByType."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        building_type = payload.get("building_type", "all")
        step_log = payload.get("_step_log")

        # Build cache dict from input_path for cache key computation
        input_cache = {"path": input_path, "size": 0}
        if input_path:
            try:
                from facetwork.runtime.storage import get_storage_backend as _get_backend

                input_cache["size"] = _get_backend(input_path).getsize(input_path)
            except Exception:
                pass

        hit = cached_result(qualified, input_cache, {"building_type": building_type}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} for {building_type} buildings")
        log.info("%s filtering %s for %s buildings", facet_name, input_path, building_type)

        if not input_path:
            return {"result": _empty_result(building_type)}

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
                if building_type == "all" or props.get("building_type") == building_type:
                    filtered.append(feature)

            _dir = posixpath.dirname(input_path)
            output_path = f"{_dir}/{uri_stem(input_path)}_{building_type}.geojson"
            output_geojson = {"type": "FeatureCollection", "features": filtered}

            with _st.open(output_path, "w") as f:
                json.dump(output_geojson, f, indent=2)

            total_area = sum(f["properties"].get("area_m2", 0) for f in filtered)
            with_height = sum(
                1
                for f in filtered
                if f["properties"].get("height") or f["properties"].get("levels")
            )

            all_features = geojson.get("features", [])
            if step_log:
                step_log(
                    f"{facet_name}: {len(filtered)}/{len(all_features)} {building_type} buildings",
                    level="success",
                )
            rv = {
                "result": {
                    "output_path": str(output_path),
                    "feature_count": len(filtered),
                    "building_type": building_type,
                    "total_area_km2": round(total_area / 1_000_000, 4),
                    "with_height_data": with_height,
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }
            save_result_meta(qualified, input_cache, {"building_type": building_type}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter buildings: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter buildings: {exc}", level="error")
            raise

    return handler


def _stats_to_dict(stats: BuildingStats) -> dict:
    """Convert BuildingStats to dict."""
    return {
        "total_buildings": stats.total_buildings,
        "total_area_km2": stats.total_area_km2,
        "residential": stats.residential,
        "commercial": stats.commercial,
        "industrial": stats.industrial,
        "retail": stats.retail,
        "other": stats.other,
        "avg_levels": stats.avg_levels,
        "with_height": stats.with_height,
    }


def _empty_result(building_type: str) -> dict:
    """Return empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "building_type": building_type,
        "total_area_km2": 0.0,
        "with_height_data": 0,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "total_buildings": 0,
        "total_area_km2": 0.0,
        "residential": 0,
        "commercial": 0,
        "industrial": 0,
        "retail": 0,
        "other": 0,
        "avg_levels": 0.0,
        "with_height": 0,
    }


BUILDING_FACETS = [
    ("BuildingStatistics", _make_building_stats_handler),
    ("FilterBuildingsByType", _make_filter_buildings_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in BUILDING_FACETS:
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


def register_building_handlers(poller) -> None:
    """Register all building event facet handlers."""
    for facet_name, handler_factory in BUILDING_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered building handler: %s", qualified_name)
