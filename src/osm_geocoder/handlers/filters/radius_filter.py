"""Radius-based filtering for OSM boundary polygons.

Converts polygon boundaries to equivalent circular radius using:
    radius = sqrt(area / π)

Provides filtering by configurable units, comparison operators, and thresholds.
"""

from __future__ import annotations

import logging
import math
import posixpath
from dataclasses import dataclass
from datetime import UTC
from enum import Enum
from pathlib import Path
from typing import Any

from facetwork.runtime.storage import get_storage_backend

from ..shared._output import uri_stem

log = logging.getLogger(__name__)

_storage = get_storage_backend()

# Check for pyproj availability (optional, provides accurate geodesic area)
try:
    from pyproj import CRS, Transformer
    from shapely.ops import transform

    HAS_PYPROJ = True
except ImportError:
    HAS_PYPROJ = False

# Check for shapely availability
try:
    from shapely.geometry import shape

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False


class Unit(Enum):
    """Distance units for radius filtering."""

    METERS = "meters"
    KILOMETERS = "kilometers"
    MILES = "miles"

    @classmethod
    def from_string(cls, value: str) -> Unit:
        """Parse a unit string (case-insensitive)."""
        normalized = value.lower().strip()
        # Handle aliases
        aliases = {
            "m": cls.METERS,
            "meter": cls.METERS,
            "meters": cls.METERS,
            "km": cls.KILOMETERS,
            "kilometer": cls.KILOMETERS,
            "kilometers": cls.KILOMETERS,
            "mi": cls.MILES,
            "mile": cls.MILES,
            "miles": cls.MILES,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown unit: {value}")


class Operator(Enum):
    """Comparison operators for radius filtering."""

    GT = "gt"  # Greater than
    GTE = "gte"  # Greater than or equal
    LT = "lt"  # Less than
    LTE = "lte"  # Less than or equal
    EQ = "eq"  # Equal (with tolerance)
    NE = "ne"  # Not equal
    BETWEEN = "between"  # Inclusive range (requires min/max)

    @classmethod
    def from_string(cls, value: str) -> Operator:
        """Parse an operator string (case-insensitive)."""
        normalized = value.lower().strip()
        aliases = {
            "gt": cls.GT,
            ">": cls.GT,
            "gte": cls.GTE,
            ">=": cls.GTE,
            "lt": cls.LT,
            "<": cls.LT,
            "lte": cls.LTE,
            "<=": cls.LTE,
            "eq": cls.EQ,
            "=": cls.EQ,
            "==": cls.EQ,
            "ne": cls.NE,
            "!=": cls.NE,
            "<>": cls.NE,
            "between": cls.BETWEEN,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown operator: {value}")


@dataclass
class RadiusCriteria:
    """Filtering criteria for radius-based filtering."""

    threshold: float
    unit: Unit = Unit.KILOMETERS
    operator: Operator = Operator.GTE
    max_threshold: float | None = None  # Only used for BETWEEN operator
    tolerance: float = 0.01  # 1% tolerance for EQ/NE comparisons


# Conversion factors to meters
_TO_METERS = {
    Unit.METERS: 1.0,
    Unit.KILOMETERS: 1000.0,
    Unit.MILES: 1609.344,
}


def convert_to_meters(value: float, unit: Unit) -> float:
    """Convert a distance value to meters."""
    return value * _TO_METERS[unit]


def convert_from_meters(value: float, unit: Unit) -> float:
    """Convert a distance value from meters to the specified unit."""
    return value / _TO_METERS[unit]


def compute_geodesic_area(geometry: Any) -> float:
    """Compute the geodesic area of a geometry in square meters.

    Uses pyproj with an Albers Equal Area projection for accuracy.
    Falls back to spherical approximation when pyproj is unavailable.

    Args:
        geometry: A shapely geometry object

    Returns:
        Area in square meters
    """
    if not HAS_SHAPELY:
        raise RuntimeError("shapely is required for area computation")

    if HAS_PYPROJ:
        # Use Albers Equal Area projection centered on the geometry
        centroid = geometry.centroid
        lon, lat = centroid.x, centroid.y

        # Create a custom Albers Equal Area projection
        aea_crs = CRS.from_proj4(
            f"+proj=aea +lat_1={lat - 5} +lat_2={lat + 5} "
            f"+lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
        )

        # Transform from WGS84 to the equal area projection
        wgs84 = CRS.from_epsg(4326)
        transformer = Transformer.from_crs(wgs84, aea_crs, always_xy=True)

        projected = transform(transformer.transform, geometry)
        return abs(projected.area)

    # Fallback: spherical approximation using the geometry's bounds
    # This is less accurate but works without pyproj
    log.warning("pyproj not available, using spherical approximation for area")

    # Use Shoelace formula with spherical correction
    bounds = geometry.bounds  # (minx, miny, maxx, maxy)
    center_lat = (bounds[1] + bounds[3]) / 2

    # Approximate meters per degree at this latitude
    lat_rad = math.radians(center_lat)
    meters_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * lat_rad)
    meters_per_deg_lon = 111412.84 * math.cos(lat_rad)

    # Scale the planar area
    # This is a rough approximation - proper geodesic calculation is preferred
    planar_area = geometry.area  # In square degrees
    return planar_area * meters_per_deg_lat * meters_per_deg_lon


def compute_equivalent_radius(geometry: Any) -> float:
    """Compute the equivalent circular radius for a polygon.

    The equivalent radius is the radius of a circle with the same area:
        radius = sqrt(area / π)

    Args:
        geometry: A shapely geometry object

    Returns:
        Equivalent radius in meters
    """
    area = compute_geodesic_area(geometry)
    return math.sqrt(area / math.pi)


def matches_criteria(radius_meters: float, criteria: RadiusCriteria) -> bool:
    """Check if a radius matches the filtering criteria.

    Args:
        radius_meters: The radius to check (in meters)
        criteria: The filtering criteria

    Returns:
        True if the radius matches the criteria
    """
    threshold_meters = convert_to_meters(criteria.threshold, criteria.unit)

    if criteria.operator == Operator.GT:
        return radius_meters > threshold_meters

    if criteria.operator == Operator.GTE:
        return radius_meters >= threshold_meters

    if criteria.operator == Operator.LT:
        return radius_meters < threshold_meters

    if criteria.operator == Operator.LTE:
        return radius_meters <= threshold_meters

    if criteria.operator == Operator.EQ:
        tolerance = threshold_meters * criteria.tolerance
        return abs(radius_meters - threshold_meters) <= tolerance

    if criteria.operator == Operator.NE:
        tolerance = threshold_meters * criteria.tolerance
        return abs(radius_meters - threshold_meters) > tolerance

    if criteria.operator == Operator.BETWEEN:
        if criteria.max_threshold is None:
            raise ValueError("BETWEEN operator requires max_threshold")
        max_meters = convert_to_meters(criteria.max_threshold, criteria.unit)
        return threshold_meters <= radius_meters <= max_meters

    raise ValueError(f"Unknown operator: {criteria.operator}")


def parse_criteria(
    radius: float,
    unit: str = "kilometers",
    operator: str = "gte",
    max_radius: float | None = None,
    tolerance: float = 0.01,
) -> RadiusCriteria:
    """Parse string parameters into a RadiusCriteria object.

    Args:
        radius: The threshold radius value
        unit: Unit string (meters, kilometers, miles)
        operator: Operator string (gt, gte, lt, lte, eq, ne, between)
        max_radius: Maximum radius for BETWEEN operator
        tolerance: Tolerance for EQ/NE comparisons (default 1%)

    Returns:
        A RadiusCriteria object
    """
    return RadiusCriteria(
        threshold=radius,
        unit=Unit.from_string(unit),
        operator=Operator.from_string(operator),
        max_threshold=max_radius,
        tolerance=tolerance,
    )


@dataclass
class FilteredFeatures:
    """Result of a filtering operation."""

    output_path: str
    feature_count: int
    original_count: int
    boundary_type: str
    filter_applied: str
    format: str = "GeoJSON"
    extraction_date: str = ""


def filter_geojson(
    input_path: str | Path,
    criteria: RadiusCriteria,
    output_path: str | Path | None = None,
    boundary_type: str | None = None,
    heartbeat: callable | None = None,
    task_uuid: str = "",
) -> FilteredFeatures:
    """Filter GeoJSON features by equivalent radius.

    Args:
        input_path: Path to input GeoJSON file
        criteria: Radius filtering criteria
        output_path: Path to output GeoJSON file (default: adds _filtered suffix)
        boundary_type: Optional boundary type to filter by (from properties)
        heartbeat: Optional callback to signal progress during long operations

    Returns:
        FilteredFeatures with output path and counts
    """

    if not HAS_SHAPELY:
        raise RuntimeError("shapely is required for GeoJSON filtering")

    input_path = str(input_path)
    if output_path is None:
        stem = uri_stem(input_path)
        _dir = posixpath.dirname(input_path)
        output_path = f"{_dir}/{stem}_filtered.geojson"
    output_path = str(output_path)

    # Stream features to avoid loading multi-GB files into memory.
    # Write to a local temp file to avoid VirtioFS write stalls.
    import os
    import shutil
    import tempfile

    from facetwork.runtime.storage import localize

    from ..shared._output import ensure_dir
    from ..shared.geojson_writer import GeoJSONStreamWriter, iter_geojson_features

    local_path = localize(input_path)

    original_count = 0
    from facetwork.config import get_temp_dir

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)

    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_path, heartbeat):
                original_count += 1

                # Check boundary type if specified
                if boundary_type is not None:
                    props = feature.get("properties", {})
                    feature_type = props.get("boundary_type") or props.get("type") or ""
                    if feature_type.lower() != boundary_type.lower():
                        continue

                # Compute equivalent radius and check criteria
                geometry = feature.get("geometry")
                if geometry is None:
                    continue

                try:
                    geom = shape(geometry)
                    radius_meters = compute_equivalent_radius(geom)

                    if matches_criteria(radius_meters, criteria):
                        if "properties" not in feature:
                            feature["properties"] = {}
                        feature["properties"]["equivalent_radius_m"] = radius_meters
                        feature["properties"]["equivalent_radius_km"] = radius_meters / 1000

                        writer.write_feature(feature)
                except Exception as e:
                    log.warning("Failed to process feature: %s", e)
                    continue

        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    # Build filter description
    filter_desc = _describe_filter(criteria, boundary_type)

    from datetime import datetime

    return FilteredFeatures(
        output_path=str(output_path),
        feature_count=writer.feature_count,
        original_count=original_count,
        boundary_type=boundary_type or "all",
        filter_applied=filter_desc,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def _describe_filter(criteria: RadiusCriteria, boundary_type: str | None) -> str:
    """Build a human-readable filter description."""
    op_symbols = {
        Operator.GT: ">",
        Operator.GTE: ">=",
        Operator.LT: "<",
        Operator.LTE: "<=",
        Operator.EQ: "=",
        Operator.NE: "!=",
    }

    if criteria.operator == Operator.BETWEEN:
        filter_str = f"radius {criteria.threshold}-{criteria.max_threshold} {criteria.unit.value}"
    else:
        symbol = op_symbols.get(criteria.operator, criteria.operator.value)
        filter_str = f"radius {symbol} {criteria.threshold} {criteria.unit.value}"

    if boundary_type:
        filter_str = f"type={boundary_type}, {filter_str}"

    return filter_str
