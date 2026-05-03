"""PBF source adapter — extracts OSM features from .osm.pbf files.

Delegates to existing category-specific extractors (route_extractor,
amenity_extractor, etc.) and normalizes their output into the standard
schema dictionaries expected by downstream analysis facets.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from datetime import UTC, datetime

from ..shared._output import uri_stem
from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

HAS_OSMIUM = importlib.util.find_spec("osmium") is not None

NAMESPACE = "osm.Source.PBF"

_LOCAL_OUTPUT = os.environ.get("AFL_LOCAL_OUTPUT_DIR", "/tmp")


def _file_size(path: str) -> int:
    if not path or path.startswith("hdfs://"):
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _cache_from_payload(payload: dict) -> dict:
    """Extract OSMCache dict from payload."""
    cache = payload.get("cache", {})
    if isinstance(cache, str):
        cache = {"path": cache, "size": 0}
    return cache


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _extract_routes(payload: dict) -> dict:
    """PBF route extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_route_result(payload)}

    from ..routes.route_extractor import extract_routes

    cache = _cache_from_payload(payload)
    route_type = payload.get("route_type", "all")
    network = payload.get("network", "*")
    include_infra = payload.get("include_infrastructure", True)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractRoutes"
    dyn = {"route_type": route_type, "network": network, "include_infrastructure": include_infra}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractRoutes: extracting {route_type} routes from {uri_stem(pbf_path)}")

    result = extract_routes(
        pbf_path,
        route_type=route_type,
        network=network,
        include_infrastructure=include_infra,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "route_type": result.route_type,
            "network_level": result.network_level,
            "include_infrastructure": result.include_infrastructure,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(f"PBF.ExtractRoutes: {result.feature_count} features extracted", level="success")
    return rv


def _empty_route_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "route_type": payload.get("route_type", "all"),
        "network_level": payload.get("network", "*"),
        "include_infrastructure": payload.get("include_infrastructure", True),
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Amenities
# ---------------------------------------------------------------------------


def _extract_amenities(payload: dict) -> dict:
    """PBF amenity extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_amenity_result(payload)}

    from ..amenities.amenity_extractor import extract_amenities

    cache = _cache_from_payload(payload)
    category = payload.get("category", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractAmenities"
    dyn = {"category": category}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractAmenities: extracting {category} from {uri_stem(pbf_path)}")

    result = extract_amenities(
        pbf_path,
        category=category,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "amenity_category": result.amenity_category,
            "amenity_types": result.amenity_types,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(
            f"PBF.ExtractAmenities: {result.feature_count} features extracted", level="success"
        )
    return rv


def _empty_amenity_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "amenity_category": payload.get("category", "all"),
        "amenity_types": "",
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Roads
# ---------------------------------------------------------------------------


def _extract_roads(payload: dict) -> dict:
    """PBF road extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_road_result(payload)}

    from ..roads.road_extractor import extract_roads

    cache = _cache_from_payload(payload)
    road_class = payload.get("road_class", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractRoads"
    dyn = {"road_class": road_class}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractRoads: extracting {road_class} from {uri_stem(pbf_path)}")

    result = extract_roads(
        pbf_path,
        road_class=road_class,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "road_class": result.road_class,
            "total_length_km": result.total_length_km,
            "with_speed_limit": result.with_speed_limit,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(f"PBF.ExtractRoads: {result.feature_count} features extracted", level="success")
    return rv


def _empty_road_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "road_class": payload.get("road_class", "all"),
        "total_length_km": 0.0,
        "with_speed_limit": 0,
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Parks
# ---------------------------------------------------------------------------


def _extract_parks(payload: dict) -> dict:
    """PBF park extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_park_result(payload)}

    from ..parks.park_extractor import extract_parks

    cache = _cache_from_payload(payload)
    park_type = payload.get("park_type", "all")
    protect_classes = payload.get("protect_classes", "*")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractParks"
    dyn = {"park_type": park_type, "protect_classes": protect_classes}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractParks: extracting {park_type} from {uri_stem(pbf_path)}")

    result = extract_parks(
        pbf_path,
        park_type=park_type,
        protect_classes=protect_classes,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "park_type": result.park_type,
            "protect_classes": result.protect_classes,
            "total_area_km2": result.total_area_km2,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(f"PBF.ExtractParks: {result.feature_count} features extracted", level="success")
    return rv


def _empty_park_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "park_type": payload.get("park_type", "all"),
        "protect_classes": payload.get("protect_classes", "*"),
        "total_area_km2": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Buildings
# ---------------------------------------------------------------------------


def _extract_buildings(payload: dict) -> dict:
    """PBF building extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_building_result(payload)}

    from ..buildings.building_extractor import extract_buildings

    cache = _cache_from_payload(payload)
    building_type = payload.get("building_type", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractBuildings"
    dyn = {"building_type": building_type}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractBuildings: extracting {building_type} from {uri_stem(pbf_path)}")

    result = extract_buildings(
        pbf_path,
        building_type=building_type,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "building_type": result.building_type,
            "total_area_km2": result.total_area_km2,
            "with_height_data": result.with_height_data,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(
            f"PBF.ExtractBuildings: {result.feature_count} features extracted", level="success"
        )
    return rv


def _empty_building_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "building_type": payload.get("building_type", "all"),
        "total_area_km2": 0.0,
        "with_height_data": 0,
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def _extract_boundaries(payload: dict) -> dict:
    """PBF boundary extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_boundary_result(payload)}

    from ..boundaries.boundary_extractor import extract_boundaries

    cache = _cache_from_payload(payload)
    boundary_type = payload.get("boundary_type", "admin")
    admin_level = payload.get("admin_level", 2)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractBoundaries"
    dyn = {"boundary_type": boundary_type, "admin_level": admin_level}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(
            f"PBF.ExtractBoundaries: extracting {boundary_type} (level {admin_level}) from {uri_stem(pbf_path)}"
        )

    result = extract_boundaries(
        pbf_path,
        boundary_type=boundary_type,
        admin_level=admin_level,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": result.output_path,
            "feature_count": result.feature_count,
            "boundary_type": result.boundary_type,
            "admin_levels": result.admin_levels,
            "format": result.format,
            "extraction_date": result.extraction_date,
        }
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(
            f"PBF.ExtractBoundaries: {result.feature_count} features extracted", level="success"
        )
    return rv


def _empty_boundary_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "boundary_type": payload.get("boundary_type", "admin"),
        "admin_levels": str(payload.get("admin_level", 2)),
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


def _extract_population(payload: dict) -> dict:
    """PBF population extraction via osmium."""
    if not HAS_OSMIUM:
        return {"result": _empty_population_result(payload)}

    from ..population.population_filter import extract_places_with_population

    cache = _cache_from_payload(payload)
    place_type = payload.get("place_type", "all")
    min_population = payload.get("min_population", 0)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractPopulation"
    dyn = {"place_type": place_type, "min_population": min_population}
    hit = cached_result(qualified, cache, dyn, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(
            f"PBF.ExtractPopulation: extracting {place_type} (min {min_population}) from {uri_stem(pbf_path)}"
        )

    result = extract_places_with_population(
        pbf_path,
        place_type=place_type,
        min_population=min_population,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
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
    }

    save_result_meta(qualified, cache, dyn, rv)
    if step_log:
        step_log(
            f"PBF.ExtractPopulation: {result.feature_count} features extracted", level="success"
        )
    return rv


def _empty_population_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "original_count": 0,
        "place_type": payload.get("place_type", "all"),
        "min_population": payload.get("min_population", 0),
        "max_population": 0,
        "filter_applied": "none",
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# POIs
# ---------------------------------------------------------------------------


def _extract_pois(payload: dict) -> dict:
    """PBF POI extraction via osmium."""
    if not HAS_OSMIUM:
        return {"pois": _empty_cache()}

    from ..poi.poi_handlers import extract_pois

    cache = _cache_from_payload(payload)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractPOIs"
    hit = cached_result(qualified, cache, {}, step_log)
    if hit is not None:
        return hit

    pbf_path = cache.get("path", "")
    if step_log:
        step_log(f"PBF.ExtractPOIs: extracting from {uri_stem(pbf_path)}")

    result = extract_pois(
        pbf_path,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {"pois": result}
    save_result_meta(qualified, cache, {}, rv)
    if step_log:
        step_log("PBF.ExtractPOIs: extraction complete", level="success")
    return rv


def _empty_cache() -> dict:
    return {"url": "", "path": "", "date": _now(), "size": 0, "wasInCache": False}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

PBF_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.ExtractRoutes": _extract_routes,
    f"{NAMESPACE}.ExtractAmenities": _extract_amenities,
    f"{NAMESPACE}.ExtractRoads": _extract_roads,
    f"{NAMESPACE}.ExtractParks": _extract_parks,
    f"{NAMESPACE}.ExtractBuildings": _extract_buildings,
    f"{NAMESPACE}.ExtractBoundaries": _extract_boundaries,
    f"{NAMESPACE}.ExtractPopulation": _extract_population,
    f"{NAMESPACE}.ExtractPOIs": _extract_pois,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = PBF_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown PBF source facet: {facet_name}")
    return handler(payload)
