"""Population filter event facet handlers.

Handles population filtering events defined in osmfilters_population.afl
under osm.Population namespace.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .population_filter import (
    Operator,
    PopulationFilteredFeatures,
    PopulationStats,
    calculate_population_stats,
    filter_geojson_by_population,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Population"


def _make_filter_by_population_handler(facet_name: str):
    """Create handler for FilterByPopulation event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        min_population = payload.get("min_population", 0)
        place_type = payload.get("place_type", "all")
        operator = payload.get("operator", "gte")
        step_log = payload.get("_step_log")

        # Dynamic cache check (params come from payload)
        cache_params = {
            "place_type": place_type,
            "min_population": min_population,
            "operator": operator,
        }
        input_cache = {"path": input_path, "size": 0}
        hit = cached_result(qualified, input_cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: filtering {input_path} for {place_type} with population {operator} {min_population}"
            )
        log.info(
            "%s filtering %s for %s with population %s %d",
            facet_name,
            input_path,
            place_type,
            operator,
            min_population,
        )

        if not input_path:
            return {"result": _empty_result(place_type, min_population, 0)}

        try:
            result = filter_geojson_by_population(
                input_path,
                min_population=min_population,
                place_type=place_type,
                operator=operator,
            )
            if step_log:
                step_log(
                    f"{facet_name}: {result.feature_count}/{result.original_count} matched ({place_type}, pop {operator} {min_population})",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, input_cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter by population: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter by population: {exc}", level="error")
            raise

    return handler


def _make_filter_by_population_range_handler(facet_name: str):
    """Create handler for FilterByPopulationRange event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        min_population = payload.get("min_population", 0)
        max_population = payload.get("max_population", 0)
        place_type = payload.get("place_type", "all")
        step_log = payload.get("_step_log")

        # Dynamic cache check (params come from payload)
        cache_params = {
            "place_type": place_type,
            "min_population": min_population,
            "max_population": max_population,
            "operator": "between",
        }
        input_cache = {"path": input_path, "size": 0}
        hit = cached_result(qualified, input_cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: filtering {input_path} for {place_type} with population {min_population}-{max_population}"
            )
        log.info(
            "%s filtering %s for %s with population %d-%d",
            facet_name,
            input_path,
            place_type,
            min_population,
            max_population,
        )

        if not input_path:
            return {"result": _empty_result(place_type, min_population, max_population)}

        try:
            result = filter_geojson_by_population(
                input_path,
                min_population=min_population,
                max_population=max_population,
                place_type=place_type,
                operator=Operator.BETWEEN,
            )
            if step_log:
                step_log(
                    f"{facet_name}: {result.feature_count}/{result.original_count} matched ({place_type}, pop {min_population}-{max_population})",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, input_cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to filter by population range: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to filter by population range: {exc}", level="error")
            raise

    return handler


def _make_population_stats_handler(facet_name: str):
    """Create handler for PopulationStatistics event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        place_type = payload.get("place_type", "all")
        step_log = payload.get("_step_log")

        # Dynamic cache check (params come from payload)
        cache_params = {"place_type": place_type}
        input_cache = {"path": input_path, "size": 0}
        hit = cached_result(qualified, input_cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: calculating stats for {input_path} ({place_type})")
        log.info("%s calculating stats for %s (%s)", facet_name, input_path, place_type)

        if not input_path:
            return {"stats": _empty_stats()}

        try:
            stats = calculate_population_stats(input_path, place_type=place_type)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.total_places} places, total pop {stats.total_population}",
                    level="success",
                )
            rv = {"stats": _stats_to_dict(stats)}
            save_result_meta(qualified, input_cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to calculate population stats: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to calculate population stats: {exc}", level="error")
            raise

    return handler


def _result_to_dict(result: PopulationFilteredFeatures) -> dict:
    """Convert a PopulationFilteredFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "original_count": result.original_count,
        "place_type": result.place_type,
        "min_population": result.min_population,
        "max_population": result.max_population,
        "filter_applied": result.filter_applied,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: PopulationStats) -> dict:
    """Convert PopulationStats to a dictionary."""
    return {
        "total_places": stats.total_places,
        "total_population": stats.total_population,
        "min_population": stats.min_population,
        "max_population": stats.max_population,
        "avg_population": stats.avg_population,
        "place_type": stats.place_type,
    }


def _empty_result(place_type: str, min_pop: int, max_pop: int) -> dict:
    """Return an empty result dict."""
    return {
        "output_path": "",
        "feature_count": 0,
        "original_count": 0,
        "place_type": place_type,
        "min_population": min_pop,
        "max_population": max_pop,
        "filter_applied": "",
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_stats() -> dict:
    """Return empty stats dict."""
    return {
        "total_places": 0,
        "total_population": 0,
        "min_population": 0,
        "max_population": 0,
        "avg_population": 0,
        "place_type": "",
    }


# Event facet definitions for handler registration
POPULATION_FACETS = [
    # Generic filters
    ("FilterByPopulation", _make_filter_by_population_handler),
    ("FilterByPopulationRange", _make_filter_by_population_range_handler),
    ("PopulationStatistics", _make_population_stats_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in POPULATION_FACETS:
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


def register_population_handlers(poller) -> None:
    """Register all population event facet handlers with the poller."""
    for facet_name, handler_factory in POPULATION_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered population handler: %s", qualified_name)
