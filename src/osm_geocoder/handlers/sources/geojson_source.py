"""GeoJSON source adapter — loads and filters existing GeoJSON files.

Provides a unified source interface for re-processing previously extracted
GeoJSON data or third-party GeoJSON files, applying optional tag/property
filters to produce output compatible with downstream analysis facets.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from ..shared._output import uri_stem
from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

NAMESPACE = "osm.Source.GeoJSON"

_LOCAL_OUTPUT = os.environ.get("AFL_LOCAL_OUTPUT_DIR", "/tmp")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _file_size(path: str) -> int:
    if not path or path.startswith("hdfs://"):
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _iter_features(path: str):
    """Iterate over GeoJSON features from a file (streaming)."""
    with open(path) as f:
        data = json.load(f)
    features = data.get("features", []) if isinstance(data, dict) else []
    yield from features


def _filter_and_write(
    input_path: str,
    output_path: str,
    predicate,
    *,
    heartbeat=None,
) -> int:
    """Filter features from input and write matching ones to output.

    Returns feature count.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(output_path, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for feature in _iter_features(input_path):
            if heartbeat:
                heartbeat()
            if predicate(feature):
                if count > 0:
                    f.write(",\n")
                json.dump(feature, f)
                count += 1
        f.write("\n]}\n")

    return count


def _output_path(category: str, subcategory: str, input_path: str) -> str:
    """Build output path based on input filename."""
    stem = Path(input_path).stem
    return os.path.join(_LOCAL_OUTPUT, "geojson-source", category, f"{stem}_{subcategory}.geojson")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_ROUTE_TAG_VALUES = {
    "bicycle": {"bicycle", "mtb"},
    "hiking": {"hiking", "foot", "walking"},
    "train": {"train", "railway", "light_rail", "subway", "tram"},
    "bus": {"bus", "trolleybus"},
}


def _load_routes(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    route_type = payload.get("route_type", "all")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_route_result(payload)}

    qualified = f"{NAMESPACE}.LoadRoutes"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"route_type": route_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadRoutes: loading {route_type} routes from {uri_stem(input_path)}")

    if route_type == "all":

        def predicate(f):
            return True

    else:
        values = _ROUTE_TAG_VALUES.get(route_type, {route_type})

        def predicate(f):
            props = f.get("properties", {})
            return (
                props.get("route") in values
                or props.get("route_type") in values
                or route_type in props.get("route_types", [])
            )

    out = _output_path("routes", route_type, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "route_type": route_type,
            "network_level": payload.get("network", "*"),
            "include_infrastructure": False,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadRoutes: {count} features loaded", level="success")
    return rv


def _empty_route_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "route_type": payload.get("route_type", "all"),
        "network_level": payload.get("network", "*"),
        "include_infrastructure": False,
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Amenities
# ---------------------------------------------------------------------------

_AMENITY_CATEGORY_VALUES = {
    "food": {"restaurant", "cafe", "bar", "fast_food", "pub", "food_court", "ice_cream"},
    "shopping": {"supermarket", "mall", "convenience", "department_store", "clothes", "shoes"},
    "services": {"bank", "atm", "post_office", "fuel", "charging_station", "parking"},
    "healthcare": {"hospital", "pharmacy", "doctors", "clinic", "dentist", "veterinary"},
    "education": {"school", "university", "library", "college", "kindergarten"},
    "entertainment": {"cinema", "theatre", "nightclub", "arts_centre", "casino"},
    "transport": {"bus_station", "taxi", "ferry_terminal", "bicycle_parking", "bicycle_rental"},
}


def _load_amenities(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    category = payload.get("category", "all")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_amenity_result(payload)}

    qualified = f"{NAMESPACE}.LoadAmenities"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"category": category}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadAmenities: loading {category} from {uri_stem(input_path)}")

    if category == "all":

        def predicate(f):
            return True

    else:
        values = _AMENITY_CATEGORY_VALUES.get(category, {category})

        def predicate(f):
            return f.get("properties", {}).get("amenity") in values

    out = _output_path("amenities", category, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "amenity_category": category,
            "amenity_types": ",".join(sorted(_AMENITY_CATEGORY_VALUES.get(category, []))),
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadAmenities: {count} features loaded", level="success")
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

_ROAD_CLASS_VALUES = {
    "motorway": {"motorway", "motorway_link"},
    "primary": {"primary", "primary_link"},
    "secondary": {"secondary", "secondary_link"},
    "tertiary": {"tertiary", "tertiary_link"},
    "residential": {"residential", "living_street"},
    "major": {
        "motorway",
        "motorway_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
    },
}


def _load_roads(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    road_class = payload.get("road_class", "all")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_road_result(payload)}

    qualified = f"{NAMESPACE}.LoadRoads"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"road_class": road_class}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadRoads: loading {road_class} from {uri_stem(input_path)}")

    if road_class == "all":

        def predicate(f):
            return True

    else:
        values = _ROAD_CLASS_VALUES.get(road_class, {road_class})

        def predicate(f):
            return f.get("properties", {}).get("highway") in values

    out = _output_path("roads", road_class, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "road_class": road_class,
            "total_length_km": 0.0,
            "with_speed_limit": 0,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadRoads: {count} features loaded", level="success")
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


def _load_parks(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    park_type = payload.get("park_type", "all")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_park_result(payload)}

    qualified = f"{NAMESPACE}.LoadParks"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"park_type": park_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadParks: loading {park_type} from {uri_stem(input_path)}")

    if park_type == "all":

        def predicate(f):
            return True

    elif park_type == "national":

        def predicate(f):
            return (
                f.get("properties", {}).get("boundary") == "national_park"
                or f.get("properties", {}).get("protect_class") == "2"
            )

    elif park_type == "state":

        def predicate(f):
            return f.get("properties", {}).get("protect_class") == "5"

    elif park_type == "nature_reserve":

        def predicate(f):
            return f.get("properties", {}).get("leisure") == "nature_reserve"

    else:

        def predicate(f):
            return True

    out = _output_path("parks", park_type, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "park_type": park_type,
            "protect_classes": payload.get("protect_classes", "*"),
            "total_area_km2": 0.0,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadParks: {count} features loaded", level="success")
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


def _load_buildings(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    building_type = payload.get("building_type", "all")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_building_result(payload)}

    qualified = f"{NAMESPACE}.LoadBuildings"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"building_type": building_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadBuildings: loading {building_type} from {uri_stem(input_path)}")

    _building_type_values = {
        "residential": {"residential", "house", "apartments", "detached", "semidetached_house"},
        "commercial": {"commercial", "office", "retail"},
        "industrial": {"industrial", "warehouse", "manufacture"},
        "retail": {"retail", "supermarket", "kiosk"},
    }

    if building_type == "all":

        def predicate(f):
            return True

    else:
        values = _building_type_values.get(building_type, {building_type})

        def predicate(f):
            return f.get("properties", {}).get("building") in values

    out = _output_path("buildings", building_type, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "building_type": building_type,
            "total_area_km2": 0.0,
            "with_height_data": 0,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadBuildings: {count} features loaded", level="success")
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


def _load_boundaries(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    boundary_type = payload.get("boundary_type", "admin")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_boundary_result(payload)}

    qualified = f"{NAMESPACE}.LoadBoundaries"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"boundary_type": boundary_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadBoundaries: loading {boundary_type} from {uri_stem(input_path)}")

    # For GeoJSON loading, accept all features (filtering already done at extraction)
    def predicate(f):
        return True

    out = _output_path("boundaries", boundary_type, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "boundary_type": boundary_type,
            "admin_levels": "",
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadBoundaries: {count} features loaded", level="success")
    return rv


def _empty_boundary_result(payload: dict) -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "boundary_type": payload.get("boundary_type", "admin"),
        "admin_levels": "",
        "format": "GeoJSON",
        "extraction_date": _now(),
    }


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


def _load_population(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    place_type = payload.get("place_type", "all")
    min_population = payload.get("min_population", 0)
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"result": _empty_population_result(payload)}

    qualified = f"{NAMESPACE}.LoadPopulation"
    cache_key = {"path": input_path, "size": _file_size(input_path)}
    dyn = {"place_type": place_type, "min_population": min_population}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"GeoJSON.LoadPopulation: loading {place_type} from {uri_stem(input_path)}")

    def predicate(f):
        props = f.get("properties", {})
        if place_type != "all" and props.get("place") != place_type:
            return False
        pop = props.get("population", 0)
        try:
            pop = int(pop)
        except (ValueError, TypeError):
            pop = 0
        return pop >= min_population

    out = _output_path("population", place_type, input_path)
    count = _filter_and_write(
        input_path,
        out,
        predicate,
        heartbeat=payload.get("_task_heartbeat"),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "original_count": count,
            "place_type": place_type,
            "min_population": min_population,
            "max_population": 0,
            "filter_applied": f"population >= {min_population}" if min_population > 0 else "none",
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"GeoJSON.LoadPopulation: {count} features loaded", level="success")
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


def _load_pois(payload: dict) -> dict:
    input_path = payload.get("input_path", "")
    step_log = payload.get("_step_log")

    if not input_path or not os.path.exists(input_path):
        return {"pois": _empty_cache()}

    if step_log:
        step_log(f"GeoJSON.LoadPOIs: loading from {uri_stem(input_path)}")

    rv = {
        "pois": {
            "url": "",
            "path": input_path,
            "date": _now(),
            "size": _file_size(input_path),
            "wasInCache": True,
        }
    }

    if step_log:
        step_log("GeoJSON.LoadPOIs: loaded", level="success")
    return rv


def _empty_cache() -> dict:
    return {"url": "", "path": "", "date": _now(), "size": 0, "wasInCache": False}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

GEOJSON_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.LoadRoutes": _load_routes,
    f"{NAMESPACE}.LoadAmenities": _load_amenities,
    f"{NAMESPACE}.LoadRoads": _load_roads,
    f"{NAMESPACE}.LoadParks": _load_parks,
    f"{NAMESPACE}.LoadBuildings": _load_buildings,
    f"{NAMESPACE}.LoadBoundaries": _load_boundaries,
    f"{NAMESPACE}.LoadPopulation": _load_population,
    f"{NAMESPACE}.LoadPOIs": _load_pois,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = GEOJSON_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown GeoJSON source facet: {facet_name}")
    return handler(payload)
