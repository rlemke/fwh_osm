"""Overture Maps source adapter — extracts features from remote GeoParquet.

Provides a unified source interface over `Overture Maps <https://overturemaps.org>`_
open data (cloud-hosted GeoParquet on S3/Azure), mapping Overture's GeoJSON-shaped
records into the SAME unified ``osm.*`` feature schemas as the PBF / PostGIS /
GeoJSON adapters. Overture can therefore be swapped in for any of those sources and
every downstream analysis facet works unchanged.

Design — thin, swappable reader vs. handler logic
-------------------------------------------------
The actual remote read is isolated behind a single function:

    _read_overture_records(source: dict, theme: str, type_filter, bbox) -> Iterable[dict]

Each yielded record is a plain dict ``{"geometry": <GeoJSON geom>, "properties":
{...}}`` (an Overture row already projected to GeoJSON shape). Everything else in
this module — schema mapping, category/type filtering, staging the GeoJSON output,
result-dict construction, caching, dispatch registration — is reader-agnostic and
fully exercised offline.

Real reads require pyarrow (or duckdb) and network access; that dependency lives in
the ``[overture]`` extra. When it is missing the reader raises a clear
``OvertureDependencyError`` rather than silently returning empty — callers must not
mistake "no deps installed" for "no features found". Tests monkeypatch
``_read_overture_records`` to feed synthetic Overture-shaped records, so the schema
mapping is verified without any optional dependency or network.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

NAMESPACE = "osm.Source.Overture"

_LOCAL_OUTPUT = os.environ.get("FW_LOCAL_OUTPUT_DIR", "/tmp")

# Default Overture release; overridable per-source.
DEFAULT_RELEASE = os.environ.get("FW_OVERTURE_RELEASE", "2024-09-18.0")

# Base location of Overture's GeoParquet (AWS S3 open-data bucket). The real
# reader interpolates {release}/theme={theme}/type={type}/*.parquet under this.
OVERTURE_S3_BASE = os.environ.get(
    "FW_OVERTURE_S3_BASE", "s3://overturemaps-us-west-2/release"
)


class OvertureDependencyError(RuntimeError):
    """Raised when a real Overture read is attempted without pyarrow/duckdb.

    Distinct exception type so the handler never confuses "optional dependency
    absent" with "query returned no features".
    """


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_from_payload(payload: dict) -> dict:
    """Normalize the OvertureSource payload into a dict with defaults filled in."""
    source = payload.get("source", {})
    if isinstance(source, str):
        source = {"region": source}
    return {
        "theme": source.get("theme", ""),
        "release": source.get("release") or DEFAULT_RELEASE,
        "min_lon": source.get("min_lon", -180.0),
        "min_lat": source.get("min_lat", -90.0),
        "max_lon": source.get("max_lon", 180.0),
        "max_lat": source.get("max_lat", 90.0),
        "region": source.get("region", ""),
    }


def _bbox(source: dict) -> tuple[float, float, float, float]:
    return (
        source["min_lon"],
        source["min_lat"],
        source["max_lon"],
        source["max_lat"],
    )


def _output_path(category: str, subcategory: str, region: str) -> str:
    region_slug = region.lower().replace(" ", "-") if region else "unknown"
    return os.path.join(
        _LOCAL_OUTPUT, "overture-source", category, f"{region_slug}_{subcategory}.geojson"
    )


# ---------------------------------------------------------------------------
# Swappable reader — the ONLY part that touches pyarrow/duckdb + the network.
# ---------------------------------------------------------------------------

def _has_reader_dep() -> bool:
    try:
        import pyarrow.dataset  # noqa: F401

        return True
    except ImportError:
        pass
    try:
        import duckdb  # noqa: F401

        return True
    except ImportError:
        return False


def _read_overture_records(
    source: dict,
    theme: str,
    type_filter: str | None,
    bbox: tuple[float, float, float, float],
) -> Iterable[dict]:
    """Read Overture GeoParquet rows for a theme/type within ``bbox``.

    Yields dicts shaped like ``{"geometry": <GeoJSON geom>, "properties": {...}}``.

    This is the single swappable seam: the real implementation streams remote
    GeoParquet via pyarrow/duckdb; tests monkeypatch it with synthetic records.
    Raises :class:`OvertureDependencyError` if neither optional backend is
    installed — never returns empty silently.
    """
    if not _has_reader_dep():
        raise OvertureDependencyError(
            "Reading Overture Maps GeoParquet requires the 'overture' extra "
            "(pyarrow or duckdb). Install with: pip install 'osm-geocoder[overture]'. "
            "Refusing to return an empty result so this is not mistaken for "
            "'no features found'."
        )

    # Real read path (heavy; exercised only when deps + network are present).
    return _read_overture_records_pyarrow(source, theme, type_filter, bbox)


def _read_overture_records_pyarrow(
    source: dict,
    theme: str,
    type_filter: str | None,
    bbox: tuple[float, float, float, float],
) -> Iterable[dict]:  # pragma: no cover - requires pyarrow + network
    """Stream Overture GeoParquet via pyarrow, projecting each row to GeoJSON shape.

    Kept thin and isolated: the file path layout and WKB->GeoJSON conversion are
    the Overture-specific concerns. Everything that maps these rows into the
    unified schema lives in the offline-tested handler functions below.
    """
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    import shapely.wkb

    release = source["release"]
    type_part = f"/type={type_filter}" if type_filter else ""
    uri = f"{OVERTURE_S3_BASE}/{release}/theme={theme}{type_part}/"

    dataset = ds.dataset(uri, format="parquet")
    min_lon, min_lat, max_lon, max_lat = bbox

    # Overture stores a struct `bbox` column (xmin/ymin/xmax/ymax) for pruning.
    bbox_filter = (
        (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "ymin") <= max_lat)
        & (pc.field("bbox", "ymax") >= min_lat)
    )

    for batch in dataset.to_batches(filter=bbox_filter):
        rows = batch.to_pylist()
        for row in rows:
            geom_wkb = row.get("geometry")
            if not geom_wkb:
                continue
            try:
                geom = shapely.wkb.loads(bytes(geom_wkb))
            except Exception:  # noqa: BLE001 - skip undecodable geometry
                continue
            yield {
                "geometry": json.loads(shapely.to_geojson(geom)),
                "properties": _overture_row_properties(row),
            }


def _overture_row_properties(row: dict) -> dict:  # pragma: no cover - real path helper
    """Flatten an Overture row's relevant fields into a flat properties dict."""
    props: dict = {}
    if row.get("id"):
        props["overture_id"] = row["id"]
    # Names are nested under names.primary in Overture.
    names = row.get("names") or {}
    if isinstance(names, dict) and names.get("primary"):
        props["name"] = names["primary"]
    for key in ("class", "subtype", "subclass"):
        if row.get(key):
            props[key] = row[key]
    # Categories (places theme) -> amenity-like value.
    categories = row.get("categories") or {}
    if isinstance(categories, dict) and categories.get("primary"):
        props["category"] = categories["primary"]
    return props


# ---------------------------------------------------------------------------
# Shared staging: write mapped GeoJSON features to a FeatureCollection on disk.
# ---------------------------------------------------------------------------

def _stage_features(
    records: Iterable[dict],
    output_path: str,
    map_props: Callable[[dict], dict | None],
    *,
    heartbeat=None,
) -> int:
    """Map Overture records to unified GeoJSON features and stage to ``output_path``.

    ``map_props`` receives each record's ``properties`` dict and returns the
    unified property dict (e.g. with ``amenity``/``building``/``highway`` keys),
    or ``None`` to drop the feature. Returns the number of features written.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with open(output_path, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for rec in records:
            if heartbeat:
                heartbeat()
            mapped = map_props(rec.get("properties", {}))
            if mapped is None:
                continue
            feature = {
                "type": "Feature",
                "geometry": rec.get("geometry"),
                "properties": mapped,
            }
            if count > 0:
                f.write(",\n")
            json.dump(feature, f)
            count += 1
        f.write("\n]}\n")
    return count


# ---------------------------------------------------------------------------
# Amenities  (Overture `places` theme -> unified `amenity` property)
# ---------------------------------------------------------------------------

# Overture place categories -> the coarse osm amenity categories used by the
# PBF/PostGIS/GeoJSON adapters. The unified schema only carries the resulting
# `amenity` value in properties; downstream facets filter on `category`.
_AMENITY_CATEGORIES = {
    "food": ["restaurant", "cafe", "bar", "fast_food", "pub", "food_court", "ice_cream"],
    "shopping": ["supermarket", "mall", "convenience", "department_store", "clothes", "shoes"],
    "services": ["bank", "atm", "post_office", "fuel", "charging_station", "parking"],
    "healthcare": ["hospital", "pharmacy", "doctors", "clinic", "dentist", "veterinary"],
    "education": ["school", "university", "library", "college", "kindergarten"],
    "entertainment": ["cinema", "theatre", "nightclub", "arts_centre", "casino"],
    "transport": ["bus_station", "taxi", "ferry_terminal", "bicycle_parking", "bicycle_rental"],
}


def _amenity_value(props: dict) -> str:
    """Resolve an OSM-style `amenity` value from an Overture place record."""
    # Overture's `category` (its taxonomy) maps loosely onto OSM amenity values;
    # we keep the leaf token. Fall back to an explicit `amenity` if present.
    if props.get("amenity"):
        return props["amenity"]
    cat = props.get("category") or props.get("class") or ""
    return cat.split(".")[-1] if cat else ""


def _extract_amenities(payload: dict) -> dict:
    source = _source_from_payload(payload)
    category = payload.get("category", "all")
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractAmenities"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"category": category}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractAmenities: reading {category} places for {region}")

    allowed = set(_AMENITY_CATEGORIES.get(category, [])) if category != "all" else None

    def map_props(props: dict) -> dict | None:
        amenity = _amenity_value(props)
        if not amenity:
            return None
        if allowed is not None and amenity not in allowed:
            return None
        out = dict(props)
        out["amenity"] = amenity
        return out

    records = _read_overture_records(source, "places", "place", _bbox(source))
    out = _output_path("amenities", category, region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractAmenities: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Buildings  (Overture `buildings` theme -> unified `building` property)
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


def _building_value(props: dict) -> str:
    """Resolve an OSM-style `building` value from an Overture building record."""
    if props.get("building"):
        return props["building"]
    # Overture buildings carry `class`/`subtype` (e.g. residential, commercial).
    val = props.get("subtype") or props.get("class") or "yes"
    return val


def _extract_buildings(payload: dict) -> dict:
    source = _source_from_payload(payload)
    building_type = payload.get("building_type", "all")
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractBuildings"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"building_type": building_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractBuildings: reading {building_type} for {region}")

    allowed = (
        set(_BUILDING_TYPE_TAGS.get(building_type, [])) if building_type != "all" else None
    )

    def map_props(props: dict) -> dict | None:
        building = _building_value(props)
        if allowed is not None and building not in allowed:
            return None
        out = dict(props)
        out["building"] = building
        return out

    records = _read_overture_records(source, "buildings", "building", _bbox(source))
    out = _output_path("buildings", building_type, region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractBuildings: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Roads  (Overture `transportation` theme / `segment` type -> `highway`)
# ---------------------------------------------------------------------------

_ROAD_CLASS_TAGS = {
    "motorway": ["motorway", "motorway_link"],
    "primary": ["primary", "primary_link"],
    "secondary": ["secondary", "secondary_link"],
    "tertiary": ["tertiary", "tertiary_link"],
    "residential": ["residential", "living_street"],
    "major": ["motorway", "motorway_link", "primary", "primary_link", "secondary",
              "secondary_link"],
}

# Overture transportation `class` -> OSM highway value.
_OVERTURE_ROAD_CLASS_MAP = {
    "motorway": "motorway",
    "trunk": "trunk",
    "primary": "primary",
    "secondary": "secondary",
    "tertiary": "tertiary",
    "residential": "residential",
    "living_street": "living_street",
    "footway": "footway",
    "cycleway": "cycleway",
}


def _highway_value(props: dict) -> str:
    if props.get("highway"):
        return props["highway"]
    cls = props.get("class") or ""
    return _OVERTURE_ROAD_CLASS_MAP.get(cls, cls)


def _extract_roads(payload: dict) -> dict:
    source = _source_from_payload(payload)
    road_class = payload.get("road_class", "all")
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractRoads"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"road_class": road_class}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractRoads: reading {road_class} for {region}")

    allowed = set(_ROAD_CLASS_TAGS.get(road_class, [])) if road_class != "all" else None

    def map_props(props: dict) -> dict | None:
        highway = _highway_value(props)
        if not highway:
            return None
        if allowed is not None and highway not in allowed:
            return None
        out = dict(props)
        out["highway"] = highway
        return out

    records = _read_overture_records(source, "transportation", "segment", _bbox(source))
    out = _output_path("roads", road_class, region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractRoads: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Parks  (Overture `base` theme / `land_use` + `land` types -> leisure/boundary)
# ---------------------------------------------------------------------------

def _park_props(props: dict, park_type: str) -> dict | None:
    subtype = props.get("subtype") or props.get("class") or ""
    out = dict(props)
    if park_type == "national":
        if subtype not in ("national_park", "protected"):
            return None
        out["boundary"] = "national_park"
    elif park_type == "nature_reserve":
        if subtype not in ("nature_reserve", "protected"):
            return None
        out["leisure"] = "nature_reserve"
    else:
        if subtype not in ("park", "national_park", "nature_reserve", "protected",
                           "recreation_ground", "forest"):
            return None
        out["leisure"] = "park"
    return out


def _extract_parks(payload: dict) -> dict:
    source = _source_from_payload(payload)
    park_type = payload.get("park_type", "all")
    protect_classes = payload.get("protect_classes", "*")
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractParks"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"park_type": park_type, "protect_classes": protect_classes}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractParks: reading {park_type} for {region}")

    records = _read_overture_records(source, "base", "land_use", _bbox(source))
    out = _output_path("parks", park_type, region)
    count = _stage_features(
        records, out, lambda p: _park_props(p, park_type),
        heartbeat=payload.get("_task_heartbeat"),
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
        step_log(f"Overture.ExtractParks: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Boundaries  (Overture `divisions` theme / `division_boundary` -> admin_level)
# ---------------------------------------------------------------------------

_ADMIN_LEVEL_MAP = {"country": "2", "state": "4", "county": "6", "city": "8"}
# Overture division subtypes -> OSM admin_level.
_OVERTURE_DIVISION_LEVEL = {
    "country": "2",
    "region": "4",
    "county": "6",
    "locality": "8",
    "localadmin": "7",
}


def _extract_boundaries(payload: dict) -> dict:
    source = _source_from_payload(payload)
    boundary_type = payload.get("boundary_type", "admin")
    admin_level = str(payload.get("admin_level", 2))
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractBoundaries"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"boundary_type": boundary_type, "admin_level": admin_level}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(
            f"Overture.ExtractBoundaries: reading {boundary_type} (level {admin_level}) for {region}"
        )

    target_level = _ADMIN_LEVEL_MAP.get(boundary_type, admin_level)

    def map_props(props: dict) -> dict | None:
        subtype = props.get("subtype") or props.get("class") or ""
        level = _OVERTURE_DIVISION_LEVEL.get(subtype)
        if level is None:
            return None
        if boundary_type not in ("lake", "forest", "park") and level != target_level:
            return None
        out = dict(props)
        out["boundary"] = "administrative"
        out["admin_level"] = level
        return out

    records = _read_overture_records(source, "divisions", "division_boundary", _bbox(source))
    out = _output_path("boundaries", f"{boundary_type}_level{admin_level}", region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractBoundaries: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Population  (Overture `divisions` theme / `division` -> place + population)
# ---------------------------------------------------------------------------

_PLACE_TYPE_MAP = {
    "city": "locality",
    "town": "locality",
    "village": "locality",
    "hamlet": "locality",
    "suburb": "neighborhood",
}


def _extract_population(payload: dict) -> dict:
    source = _source_from_payload(payload)
    place_type = payload.get("place_type", "all")
    min_population = payload.get("min_population", 0)
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractPopulation"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"place_type": place_type, "min_population": min_population}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(
            f"Overture.ExtractPopulation: reading {place_type} (min {min_population}) for {region}"
        )

    def map_props(props: dict) -> dict | None:
        pop = props.get("population", 0)
        try:
            pop = int(pop)
        except (ValueError, TypeError):
            pop = 0
        if pop < min_population:
            return None
        place = props.get("place") or props.get("subtype") or ""
        if place_type != "all":
            want = _PLACE_TYPE_MAP.get(place_type, place_type)
            if place != want and place != place_type:
                return None
        out = dict(props)
        out["place"] = place_type if place_type != "all" else (place or "locality")
        out["population"] = pop
        return out

    records = _read_overture_records(source, "divisions", "division", _bbox(source))
    out = _output_path("population", place_type, region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractPopulation: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Routes  (Overture has no native "route relations"; map transportation segments)
# ---------------------------------------------------------------------------

_ROUTE_TYPE_CLASS = {
    "bicycle": {"cycleway"},
    "hiking": {"footway", "path", "pedestrian"},
    "train": set(),  # not in transportation segments
    "bus": set(),
    "all": None,
}


def _extract_routes(payload: dict) -> dict:
    source = _source_from_payload(payload)
    route_type = payload.get("route_type", "all")
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractRoutes"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    dyn = {"route_type": route_type}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractRoutes: reading {route_type} routes for {region}")

    allowed = _ROUTE_TYPE_CLASS.get(route_type, None)

    def map_props(props: dict) -> dict | None:
        cls = props.get("class") or props.get("route") or ""
        if allowed is not None and cls not in allowed:
            return None
        out = dict(props)
        out["route"] = route_type if route_type != "all" else cls
        return out

    records = _read_overture_records(source, "transportation", "segment", _bbox(source))
    out = _output_path("routes", route_type, region)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log(f"Overture.ExtractRoutes: {count} features extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# POIs  (Overture `places` theme -> OSMCache pointing at the staged GeoJSON)
# ---------------------------------------------------------------------------

def _extract_pois(payload: dict) -> dict:
    source = _source_from_payload(payload)
    step_log = payload.get("_step_log")
    region = source["region"]

    qualified = f"{NAMESPACE}.ExtractPOIs"
    cache_key = {"release": source["release"], "bbox": list(_bbox(source)), "region": region}
    hit = cached_result(qualified, cache_key, {}, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(f"Overture.ExtractPOIs: reading POIs for {region}")

    def map_props(props: dict) -> dict:
        return dict(props)

    records = _read_overture_records(source, "places", "place", _bbox(source))
    out = _output_path("pois", "all", region)
    _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

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
        step_log("Overture.ExtractPOIs: extracted", level="success")
    return rv


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

OVERTURE_DISPATCH: dict[str, Callable] = {
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
    handler = OVERTURE_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown Overture source facet: {facet_name}")
    return handler(payload)
