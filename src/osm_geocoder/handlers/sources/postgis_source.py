"""PostGIS source adapter — extracts OSM features via SQL queries.

Queries osm_nodes and osm_ways tables (with tags JSONB and PostGIS geometry)
and writes results as GeoJSON files compatible with downstream analysis facets.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    psycopg2 = None

NAMESPACE = "osm.Source.PostGIS"

DEFAULT_POSTGIS_URL = os.environ.get(
    "AFL_POSTGIS_URL",
    "postgresql://afl_osm:afl_osm_2024@afl-postgres:5432/osm",
)

_LOCAL_OUTPUT = os.environ.get("AFL_LOCAL_OUTPUT_DIR", "/tmp")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_from_payload(payload: dict) -> tuple[str, str]:
    """Extract PostGIS URL and region from payload source dict."""
    source = payload.get("source", {})
    if isinstance(source, str):
        source = {"postgis_url": DEFAULT_POSTGIS_URL, "region": source}
    url = source.get("postgis_url", DEFAULT_POSTGIS_URL)
    region = source.get("region", "")
    return url, region


def _connect(url: str):
    """Create a read-only psycopg2 connection."""
    conn = psycopg2.connect(url, options="-c default_transaction_read_only=off")
    conn.autocommit = False
    return conn


def _query_to_geojson(
    url: str,
    sql: str,
    params: tuple,
    output_path: str,
    *,
    heartbeat=None,
    task_uuid: str = "",
) -> int:
    """Execute a SQL query and write results as a GeoJSON FeatureCollection.

    Returns the feature count.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(url)
    try:
        with conn.cursor(name="postgis_source", cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.itersize = 5000
            cur.execute(sql, params)

            count = 0
            with open(output_path, "w") as f:
                f.write('{"type":"FeatureCollection","features":[\n')
                for row in cur:
                    if heartbeat:
                        heartbeat()
                    geom = json.loads(row["geometry"]) if row["geometry"] else None
                    tags = row["tags"] if row["tags"] else {}
                    props = dict(tags)
                    props["osm_id"] = row["osm_id"]
                    if "region" in row.keys():
                        props["region"] = row["region"]

                    feature = {
                        "type": "Feature",
                        "geometry": geom,
                        "properties": props,
                    }
                    if count > 0:
                        f.write(",\n")
                    json.dump(feature, f)
                    count += 1

                f.write("\n]}\n")
    finally:
        conn.close()

    return count


def _output_path(category: str, subcategory: str, region: str) -> str:
    """Build a standard output path."""
    region_slug = region.lower().replace(" ", "-") if region else "unknown"
    return os.path.join(
        _LOCAL_OUTPUT, "postgis-extract", category, f"{region_slug}_{subcategory}.geojson"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_ROUTE_TYPE_TAGS = {
    "bicycle": ("route", ["bicycle", "mtb"]),
    "hiking": ("route", ["hiking", "foot", "walking"]),
    "train": ("route", ["train", "railway", "light_rail", "subway", "tram"]),
    "bus": ("route", ["bus", "trolleybus"]),
    "all": ("route", None),
}


def _extract_routes(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_route_result(payload)}

    url, region = _source_from_payload(payload)
    route_type = payload.get("route_type", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractRoutes"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"route_type": route_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractRoutes: querying {route_type} routes for {region}")

    tag_key, tag_values = _ROUTE_TYPE_TAGS.get(route_type, _ROUTE_TYPE_TAGS["all"])

    if tag_values:
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags->>%s = ANY(%s)
        """
        params = (region, tag_key, tag_values)
    else:
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags ? %s
        """
        params = (region, tag_key)

    out = _output_path("routes", route_type, region)
    count = _query_to_geojson(
        url,
        sql,
        params,
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "route_type": route_type,
            "network_level": payload.get("network", "*"),
            "include_infrastructure": payload.get("include_infrastructure", True),
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"PostGIS.ExtractRoutes: {count} features extracted", level="success")
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

_AMENITY_CATEGORIES = {
    "food": ["restaurant", "cafe", "bar", "fast_food", "pub", "food_court", "ice_cream"],
    "shopping": ["supermarket", "mall", "convenience", "department_store", "clothes", "shoes"],
    "services": ["bank", "atm", "post_office", "fuel", "charging_station", "parking"],
    "healthcare": ["hospital", "pharmacy", "doctors", "clinic", "dentist", "veterinary"],
    "education": ["school", "university", "library", "college", "kindergarten"],
    "entertainment": ["cinema", "theatre", "nightclub", "arts_centre", "casino"],
    "transport": ["bus_station", "taxi", "ferry_terminal", "bicycle_parking", "bicycle_rental"],
}


def _extract_amenities(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_amenity_result(payload)}

    url, region = _source_from_payload(payload)
    category = payload.get("category", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractAmenities"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"category": category}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractAmenities: querying {category} for {region}")

    if category != "all" and category in _AMENITY_CATEGORIES:
        amenity_values = _AMENITY_CATEGORIES[category]
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_nodes
            WHERE region = %s AND tags->>'amenity' = ANY(%s)
        """
        params = (region, amenity_values)
    else:
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_nodes
            WHERE region = %s AND tags ? 'amenity'
        """
        params = (region,)

    out = _output_path("amenities", category, region)
    count = _query_to_geojson(
        url,
        sql,
        params,
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "amenity_category": category,
            "amenity_types": ",".join(_AMENITY_CATEGORIES.get(category, [])),
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"PostGIS.ExtractAmenities: {count} features extracted", level="success")
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

_ROAD_CLASS_TAGS = {
    "motorway": ["motorway", "motorway_link"],
    "primary": ["primary", "primary_link"],
    "secondary": ["secondary", "secondary_link"],
    "tertiary": ["tertiary", "tertiary_link"],
    "residential": ["residential", "living_street"],
    "major": [
        "motorway",
        "motorway_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
    ],
}


def _extract_roads(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_road_result(payload)}

    url, region = _source_from_payload(payload)
    road_class = payload.get("road_class", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractRoads"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"road_class": road_class}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractRoads: querying {road_class} for {region}")

    if road_class != "all" and road_class in _ROAD_CLASS_TAGS:
        highway_values = _ROAD_CLASS_TAGS[road_class]
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags->>'highway' = ANY(%s)
        """
        params = (region, highway_values)
    else:
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags ? 'highway'
        """
        params = (region,)

    out = _output_path("roads", road_class, region)
    count = _query_to_geojson(
        url,
        sql,
        params,
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "road_class": road_class,
            "total_length_km": 0.0,  # would need ST_Length calculation
            "with_speed_limit": 0,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"PostGIS.ExtractRoads: {count} features extracted", level="success")
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
    if not HAS_PSYCOPG2:
        return {"result": _empty_park_result(payload)}

    url, region = _source_from_payload(payload)
    park_type = payload.get("park_type", "all")
    protect_classes = payload.get("protect_classes", "*")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractParks"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"park_type": park_type, "protect_classes": protect_classes}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractParks: querying {park_type} for {region}")

    conditions = ["region = %s"]
    params: list = [region]

    if park_type == "national":
        conditions.append("(tags->>'boundary' = 'national_park' OR tags->>'protect_class' = '2')")
    elif park_type == "state":
        conditions.append("tags->>'protect_class' = '5'")
    elif park_type == "nature_reserve":
        conditions.append("tags->>'leisure' = 'nature_reserve'")
    else:
        conditions.append(
            "(tags->>'boundary' IN ('national_park', 'protected_area') "
            "OR tags->>'leisure' = 'nature_reserve')"
        )

    if protect_classes != "*":
        conditions.append("tags->>'protect_class' = ANY(%s)")
        params.append(protect_classes.split(","))

    where = " AND ".join(conditions)
    sql = f"""
        SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
        FROM osm_ways
        WHERE {where}
    """

    out = _output_path("parks", park_type, region)
    count = _query_to_geojson(
        url,
        sql,
        tuple(params),
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "park_type": park_type,
            "protect_classes": protect_classes,
            "total_area_km2": 0.0,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"PostGIS.ExtractParks: {count} features extracted", level="success")
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

_BUILDING_TYPE_TAGS = {
    "residential": [
        "residential",
        "house",
        "apartments",
        "detached",
        "semidetached_house",
        "terrace",
    ],
    "commercial": ["commercial", "office", "retail"],
    "industrial": ["industrial", "warehouse", "manufacture"],
    "retail": ["retail", "supermarket", "kiosk"],
}


def _extract_buildings(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_building_result(payload)}

    url, region = _source_from_payload(payload)
    building_type = payload.get("building_type", "all")
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractBuildings"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"building_type": building_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractBuildings: querying {building_type} for {region}")

    if building_type != "all" and building_type in _BUILDING_TYPE_TAGS:
        values = _BUILDING_TYPE_TAGS[building_type]
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags->>'building' = ANY(%s)
        """
        params = (region, values)
    else:
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags ? 'building'
        """
        params = (region,)

    out = _output_path("buildings", building_type, region)
    count = _query_to_geojson(
        url,
        sql,
        params,
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
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
        step_log(f"PostGIS.ExtractBuildings: {count} features extracted", level="success")
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

_ADMIN_LEVEL_MAP = {
    "country": "2",
    "state": "4",
    "county": "6",
    "city": "8",
}


def _extract_boundaries(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_boundary_result(payload)}

    url, region = _source_from_payload(payload)
    boundary_type = payload.get("boundary_type", "admin")
    admin_level = str(payload.get("admin_level", 2))
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractBoundaries"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"boundary_type": boundary_type, "admin_level": admin_level}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(
            f"PostGIS.ExtractBoundaries: querying {boundary_type} (level {admin_level}) for {region}"
        )

    if boundary_type in ("lake", "forest", "park"):
        # Natural boundaries
        natural_map = {"lake": "water", "forest": "wood", "park": "park"}
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags->>'natural' = %s
        """
        params = (region, natural_map.get(boundary_type, boundary_type))
    else:
        # Admin boundaries
        level = _ADMIN_LEVEL_MAP.get(boundary_type, admin_level)
        sql = """
            SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
            FROM osm_ways
            WHERE region = %s AND tags->>'boundary' = 'administrative'
            AND tags->>'admin_level' = %s
        """
        params = (region, level)

    out = _output_path("boundaries", f"{boundary_type}_level{admin_level}", region)
    count = _query_to_geojson(
        url,
        sql,
        params,
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "boundary_type": boundary_type,
            "admin_levels": admin_level,
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }

    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"PostGIS.ExtractBoundaries: {count} features extracted", level="success")
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

_PLACE_TYPE_MAP = {
    "city": "city",
    "town": "town",
    "village": "village",
    "hamlet": "hamlet",
    "suburb": "suburb",
}


def _extract_population(payload: dict) -> dict:
    if not HAS_PSYCOPG2:
        return {"result": _empty_population_result(payload)}

    url, region = _source_from_payload(payload)
    place_type = payload.get("place_type", "all")
    min_population = payload.get("min_population", 0)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractPopulation"
    cache_key = {"postgis_url": url, "region": region}
    dyn = {"place_type": place_type, "min_population": min_population}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(
            f"PostGIS.ExtractPopulation: querying {place_type} (min {min_population}) for {region}"
        )

    conditions = ["region = %s", "tags ? 'population'"]
    params_list: list = [region]

    if place_type != "all" and place_type in _PLACE_TYPE_MAP:
        conditions.append("tags->>'place' = %s")
        params_list.append(_PLACE_TYPE_MAP[place_type])
    elif place_type != "all":
        conditions.append("tags->>'place' = %s")
        params_list.append(place_type)

    if min_population > 0:
        conditions.append("(tags->>'population')::bigint >= %s")
        params_list.append(min_population)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
        FROM osm_nodes
        WHERE {where}
    """

    out = _output_path("population", place_type, region)
    count = _query_to_geojson(
        url,
        sql,
        tuple(params_list),
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
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
        step_log(f"PostGIS.ExtractPopulation: {count} features extracted", level="success")
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
    if not HAS_PSYCOPG2:
        return {"pois": _empty_cache()}

    url, region = _source_from_payload(payload)
    step_log = payload.get("_step_log")

    qualified = f"{NAMESPACE}.ExtractPOIs"
    cache_key = {"postgis_url": url, "region": region}
    hit = cached_result(qualified, cache_key, {}, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"PostGIS.ExtractPOIs: querying POIs for {region}")

    sql = """
        SELECT osm_id, region, tags, ST_AsGeoJSON(geom) as geometry
        FROM osm_nodes
        WHERE region = %s AND (
            tags ? 'amenity' OR tags ? 'shop' OR tags ? 'tourism'
            OR tags ? 'leisure' OR tags ? 'historic' OR tags ? 'place'
        )
    """

    out = _output_path("pois", "all", region)
    count = _query_to_geojson(
        url,
        sql,
        (region,),
        out,
        heartbeat=payload.get("_task_heartbeat"),
        task_uuid=payload.get("_task_uuid", ""),
    )

    rv = {
        "pois": {
            "url": "",
            "path": out,
            "date": _now(),
            "size": os.path.getsize(out) if os.path.exists(out) else 0,
            "wasInCache": False,
        }
    }

    save_result_meta(qualified, cache_key, {}, rv)
    if step_log:
        step_log(f"PostGIS.ExtractPOIs: {count} features extracted", level="success")
    return rv


def _empty_cache() -> dict:
    return {"url": "", "path": "", "date": _now(), "size": 0, "wasInCache": False}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

POSTGIS_DISPATCH: dict[str, callable] = {
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
    handler = POSTGIS_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown PostGIS source facet: {facet_name}")
    return handler(payload)
