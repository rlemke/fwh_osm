"""Filter event facet handlers for OSM filtering.

Handles filtering events defined in osmfilters.afl under osm.Filters:
- Radius-based filtering (by equivalent circular radius)
- OSM type filtering (node, way, relation)
- OSM tag filtering (by key/value)
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .osm_type_filter import (
    HAS_OSMIUM as HAS_OSMIUM_TYPE,
)
from .osm_type_filter import (
    OSMFilteredFeatures,
    filter_geojson_by_osm_type,
    filter_pbf_by_type,
)
from .radius_filter import (
    HAS_SHAPELY,
    FilteredFeatures,
    filter_geojson,
    parse_criteria,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Filters"


def _file_size(path: str) -> int:
    """Return file size in bytes, or 0 if unavailable."""
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


# Check for boundary extractor availability (for ExtractAndFilterByRadius)
try:
    from .boundary_extractor import HAS_OSMIUM, extract_boundaries
except ImportError:
    HAS_OSMIUM = False

    def extract_boundaries(*args, **kwargs):
        raise RuntimeError("boundary_extractor not available")


def _make_radius_filter_handler(facet_name: str):
    """Create a handler for the FilterByRadius event facet.

    Filters GeoJSON features by equivalent radius using the specified
    threshold, unit, and comparison operator.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        radius = payload.get("radius", 0.0)
        unit = payload.get("unit", "kilometers")
        operator = payload.get("operator", "gte")
        step_log = payload.get("_step_log")

        # Dynamic cache check (params come from payload)
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"radius": radius, "unit": unit, "operator": operator}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} with radius {operator} {radius} {unit}")
        log.info(
            "%s filtering %s with radius %s %s %s",
            facet_name,
            input_path,
            operator,
            radius,
            unit,
        )

        if not HAS_SHAPELY or not input_path:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "boundary_type": "all",
                    "filter_applied": f"radius {operator} {radius} {unit}",
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        criteria = parse_criteria(radius=radius, unit=unit, operator=operator)
        result = filter_geojson(
            input_path,
            criteria,
            heartbeat=payload.get("_task_heartbeat"),
            task_uuid=payload.get("_task_uuid", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (radius {operator} {radius} {unit})",
                level="success",
            )
        rv = {"result": _result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_radius_range_handler(facet_name: str):
    """Create a handler for the FilterByRadiusRange event facet.

    Filters GeoJSON features by equivalent radius within an inclusive range.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        min_radius = payload.get("min_radius", 0.0)
        max_radius = payload.get("max_radius", 0.0)
        unit = payload.get("unit", "kilometers")
        step_log = payload.get("_step_log")

        # Dynamic cache check
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"min_radius": min_radius, "max_radius": max_radius, "unit": unit}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: filtering {input_path} with radius {min_radius}-{max_radius} {unit}"
            )
        log.info(
            "%s filtering %s with radius %s-%s %s",
            facet_name,
            input_path,
            min_radius,
            max_radius,
            unit,
        )

        if not HAS_SHAPELY or not input_path:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "boundary_type": "all",
                    "filter_applied": f"radius {min_radius}-{max_radius} {unit}",
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        criteria = parse_criteria(
            radius=min_radius,
            unit=unit,
            operator="between",
            max_radius=max_radius,
        )
        result = filter_geojson(
            input_path,
            criteria,
            heartbeat=payload.get("_task_heartbeat"),
            task_uuid=payload.get("_task_uuid", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (radius {min_radius}-{max_radius} {unit})",
                level="success",
            )
        rv = {"result": _result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_type_and_radius_handler(facet_name: str):
    """Create a handler for the FilterByTypeAndRadius event facet.

    Filters GeoJSON features by boundary type and equivalent radius.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        boundary_type = payload.get("boundary_type", "")
        radius = payload.get("radius", 0.0)
        unit = payload.get("unit", "kilometers")
        operator = payload.get("operator", "gte")
        step_log = payload.get("_step_log")

        # Dynamic cache check
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"boundary_type": boundary_type, "radius": radius, "unit": unit, "operator": operator}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: filtering {input_path} type={boundary_type} radius {operator} {radius} {unit}"
            )
        log.info(
            "%s filtering %s type=%s with radius %s %s %s",
            facet_name,
            input_path,
            boundary_type,
            operator,
            radius,
            unit,
        )

        if not HAS_SHAPELY or not input_path:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "boundary_type": boundary_type,
                    "filter_applied": f"type={boundary_type}, radius {operator} {radius} {unit}",
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        criteria = parse_criteria(radius=radius, unit=unit, operator=operator)
        result = filter_geojson(
            input_path,
            criteria,
            boundary_type=boundary_type,
            heartbeat=payload.get("_task_heartbeat"),
            task_uuid=payload.get("_task_uuid", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (type={boundary_type}, radius)",
                level="success",
            )
        rv = {"result": _result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_extract_and_filter_handler(facet_name: str):
    """Create a handler for the ExtractAndFilterByRadius event facet.

    Extracts boundaries from a PBF file and filters by radius in one step.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        pbf_path = cache.get("path", "")
        admin_levels = payload.get("admin_levels", [])
        natural_types = payload.get("natural_types", [])
        radius = payload.get("radius", 0.0)
        unit = payload.get("unit", "kilometers")
        operator = payload.get("operator", "gte")
        step_log = payload.get("_step_log")

        # Normalize admin_levels to list of ints
        if isinstance(admin_levels, str):
            admin_levels = [int(x.strip()) for x in admin_levels.split(",") if x.strip()]
        admin_levels = [int(x) for x in admin_levels] if admin_levels else None

        # Normalize natural_types to list of strings
        if isinstance(natural_types, str):
            natural_types = [x.strip() for x in natural_types.split(",") if x.strip()]
        natural_types = natural_types if natural_types else None

        # Dynamic cache check (admin_levels/natural_types/radius come from payload)
        cp = {
            "admin_levels": sorted(admin_levels) if admin_levels else [],
            "natural_types": sorted(natural_types) if natural_types else [],
            "radius": radius,
            "unit": unit,
            "operator": operator,
        }
        hit = cached_result(qualified, cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: extracting from {pbf_path} then filtering radius {operator} {radius} {unit}"
            )

        log.info(
            "%s extracting from %s (admin=%s, natural=%s) then filtering radius %s %s %s",
            facet_name,
            pbf_path,
            admin_levels,
            natural_types,
            operator,
            radius,
            unit,
        )

        if not HAS_OSMIUM or not HAS_SHAPELY or not pbf_path:
            boundary_type = _describe_boundary_type(admin_levels, natural_types)
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "boundary_type": boundary_type,
                    "filter_applied": f"radius {operator} {radius} {unit}",
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        # Step 1: Extract boundaries to GeoJSON
        extraction_result = extract_boundaries(
            pbf_path,
            admin_levels=admin_levels,
            natural_types=natural_types,
        )

        if not extraction_result.output_path or extraction_result.feature_count == 0:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "boundary_type": extraction_result.boundary_type,
                    "filter_applied": f"radius {operator} {radius} {unit}",
                    "format": "GeoJSON",
                    "extraction_date": extraction_result.extraction_date,
                }
            }

        # Step 2: Filter by radius
        criteria = parse_criteria(radius=radius, unit=unit, operator=operator)
        filter_result = filter_geojson(
            extraction_result.output_path,
            criteria,
            heartbeat=payload.get("_task_heartbeat"),
            task_uuid=payload.get("_task_uuid", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: extracted {extraction_result.feature_count}, filtered to {filter_result.feature_count}",
                level="success",
            )
        rv = {
            "result": {
                "output_path": filter_result.output_path,
                "feature_count": filter_result.feature_count,
                "original_count": extraction_result.feature_count,
                "boundary_type": extraction_result.boundary_type,
                "filter_applied": filter_result.filter_applied,
                "format": "GeoJSON",
                "extraction_date": filter_result.extraction_date,
            }
        }
        save_result_meta(qualified, cache, cp, rv)
        return rv

    return handler


def _result_to_dict(result: FilteredFeatures) -> dict:
    """Convert a FilteredFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "original_count": result.original_count,
        "boundary_type": result.boundary_type,
        "filter_applied": result.filter_applied,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _describe_boundary_type(
    admin_levels: list[int] | None,
    natural_types: list[str] | None,
) -> str:
    """Build a description of the boundary types being extracted."""
    parts = []
    if admin_levels:
        parts.append(f"admin:{','.join(str(x) for x in admin_levels)}")
    if natural_types:
        parts.append(f"natural:{','.join(natural_types)}")
    return "; ".join(parts) if parts else "none"


# --- OSM Type/Tag Filter Handlers ---


def _make_osm_type_filter_handler(facet_name: str):
    """Create a handler for the FilterByOSMType event facet.

    Filters PBF files by OSM element type (node, way, relation).
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        osm_type = payload.get("osm_type", "*")
        include_dependencies = payload.get("include_dependencies", False)
        step_log = payload.get("_step_log")

        # Dynamic cache check
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"osm_type": osm_type, "include_dependencies": include_dependencies}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} by OSM type={osm_type}")
        log.info(
            "%s filtering %s by OSM type=%s (deps=%s)",
            facet_name,
            input_path,
            osm_type,
            include_dependencies,
        )

        if not HAS_OSMIUM_TYPE or not input_path:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "osm_type": osm_type,
                    "filter_applied": f"type={osm_type}",
                    "dependencies_included": include_dependencies,
                    "dependency_count": 0,
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        result = filter_pbf_by_type(
            input_path,
            osm_type=osm_type,
            include_dependencies=include_dependencies,
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (type={osm_type})",
                level="success",
            )
        rv = {"result": _osm_result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_osm_tag_filter_handler(facet_name: str):
    """Create a handler for the FilterByOSMTag event facet.

    Filters PBF files by OSM tag key/value.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        tag_key = payload.get("tag_key", "")
        tag_value = payload.get("tag_value", "*")
        osm_type = payload.get("osm_type", "*")
        include_dependencies = payload.get("include_dependencies", False)
        step_log = payload.get("_step_log")

        # Dynamic cache check
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {
            "tag_key": tag_key,
            "tag_value": tag_value,
            "osm_type": osm_type,
            "include_dependencies": include_dependencies,
        }
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering {input_path} by tag {tag_key}={tag_value}")
        log.info(
            "%s filtering %s by tag %s=%s, type=%s (deps=%s)",
            facet_name,
            input_path,
            tag_key,
            tag_value,
            osm_type,
            include_dependencies,
        )

        if not HAS_OSMIUM_TYPE or not input_path or not tag_key:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "osm_type": osm_type,
                    "filter_applied": f"{tag_key}={tag_value}, type={osm_type}",
                    "dependencies_included": include_dependencies,
                    "dependency_count": 0,
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        result = filter_pbf_by_type(
            input_path,
            osm_type=osm_type,
            tag_key=tag_key,
            tag_value=tag_value if tag_value != "*" else None,
            include_dependencies=include_dependencies,
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (tag {tag_key}={tag_value})",
                level="success",
            )
        rv = {"result": _osm_result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_geojson_osm_type_filter_handler(facet_name: str):
    """Create a handler for the FilterGeoJSONByOSMType event facet.

    Filters GeoJSON files by OSM type stored in feature properties.
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        osm_type = payload.get("osm_type", "*")
        tag_key = payload.get("tag_key", "") or None
        tag_value = payload.get("tag_value", "*")
        step_log = payload.get("_step_log")

        # Dynamic cache check
        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"osm_type": osm_type, "tag_key": tag_key or "", "tag_value": tag_value}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: filtering GeoJSON {input_path} by OSM type={osm_type}")
        log.info(
            "%s filtering GeoJSON %s by OSM type=%s, tag=%s=%s",
            facet_name,
            input_path,
            osm_type,
            tag_key,
            tag_value,
        )

        if not input_path:
            return {
                "result": {
                    "output_path": "",
                    "feature_count": 0,
                    "original_count": 0,
                    "osm_type": osm_type,
                    "filter_applied": f"type={osm_type}",
                    "dependencies_included": False,
                    "dependency_count": 0,
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }

        result = filter_geojson_by_osm_type(
            input_path,
            osm_type=osm_type,
            tag_key=tag_key,
            tag_value=tag_value if tag_value != "*" else None,
            heartbeat=payload.get("_task_heartbeat"),
            task_uuid=payload.get("_task_uuid", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} matched (type={osm_type})",
                level="success",
            )
        rv = {"result": _osm_result_to_dict(result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _osm_result_to_dict(result: OSMFilteredFeatures) -> dict:
    """Convert an OSMFilteredFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "original_count": result.original_count,
        "osm_type": result.osm_type,
        "filter_applied": result.filter_applied,
        "dependencies_included": result.dependencies_included,
        "dependency_count": result.dependency_count,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


# Event facet definitions for handler registration
FILTER_FACETS = [
    # Radius-based filters
    ("FilterByRadius", _make_radius_filter_handler),
    ("FilterByRadiusRange", _make_radius_range_handler),
    ("FilterByTypeAndRadius", _make_type_and_radius_handler),
    ("ExtractAndFilterByRadius", _make_extract_and_filter_handler),
    # OSM type/tag filters
    ("FilterByOSMType", _make_osm_type_filter_handler),
    ("FilterByOSMTag", _make_osm_tag_filter_handler),
    ("FilterGeoJSONByOSMType", _make_geojson_osm_type_filter_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in FILTER_FACETS:
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


def register_filter_handlers(poller) -> None:
    """Register all filter event facet handlers with the poller."""
    for facet_name, handler_factory in FILTER_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered filter handler: %s", qualified_name)
