"""National and state parks extraction from OSM data.

Extracts park boundaries and protected areas with classification:
- National parks (boundary=national_park, protect_class=2)
- State/regional parks (protect_class=5)
- Nature reserves (leisure=nature_reserve)
- All protected areas (boundary=protected_area)
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from facetwork.runtime.storage import get_storage_backend

from ..shared._output import open_output, uri_stem

_storage = get_storage_backend()

log = logging.getLogger(__name__)

import importlib.util

HAS_OSMIUM = importlib.util.find_spec("osmium") is not None

# Check for shapely availability
try:
    from shapely.geometry import shape
    from shapely.ops import transform

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

# Check for pyproj availability
try:
    import pyproj

    HAS_PYPROJ = True
except ImportError:
    HAS_PYPROJ = False


class ParkType(Enum):
    """Supported park types for extraction."""

    NATIONAL = "national"
    STATE = "state"
    NATURE_RESERVE = "nature_reserve"
    PROTECTED_AREA = "protected_area"
    ALL = "all"

    @classmethod
    def from_string(cls, value: str) -> "ParkType":
        """Parse a park type string (case-insensitive)."""
        normalized = value.lower().strip()
        aliases = {
            "national": cls.NATIONAL,
            "national_park": cls.NATIONAL,
            "national_parks": cls.NATIONAL,
            "state": cls.STATE,
            "state_park": cls.STATE,
            "state_parks": cls.STATE,
            "regional": cls.STATE,
            "regional_park": cls.STATE,
            "nature_reserve": cls.NATURE_RESERVE,
            "nature_reserves": cls.NATURE_RESERVE,
            "reserve": cls.NATURE_RESERVE,
            "protected_area": cls.PROTECTED_AREA,
            "protected_areas": cls.PROTECTED_AREA,
            "protected": cls.PROTECTED_AREA,
            "all": cls.ALL,
            "*": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown park type: {value}")


# IUCN Protection class mappings
# See https://wiki.openstreetmap.org/wiki/Key:protect_class
PROTECT_CLASS_NATIONAL = {"2"}  # National parks (IUCN Category II)
PROTECT_CLASS_STATE = {"5"}  # Protected landscapes (often state/regional)
PROTECT_CLASS_STRICT = {"1a", "1b"}  # Strict nature reserves
PROTECT_CLASS_ALL = {"1a", "1b", "2", "3", "4", "5", "6"}


@dataclass
class ParkFeatures:
    """Result of a park extraction operation."""

    output_path: str
    feature_count: int
    park_type: str
    protect_classes: str
    total_area_km2: float
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class ParkStats:
    """Statistics for extracted parks."""

    total_parks: int
    total_area_km2: float
    national_parks: int
    state_parks: int
    nature_reserves: int
    other_protected: int
    park_type: str


def parse_protect_classes(value: str) -> set[str]:
    """Parse protect_classes parameter.

    Args:
        value: Comma-separated list like "1a,1b,2" or "*" for all

    Returns:
        Set of protect class values
    """
    if value == "*" or value.lower() == "all":
        return PROTECT_CLASS_ALL.copy()

    classes = set()
    for part in value.split(","):
        cleaned = part.strip().lower()
        if cleaned:
            classes.add(cleaned)
    return classes


def classify_park(tags: dict[str, str]) -> str:
    """Classify a park based on its OSM tags.

    Returns one of: "national", "state", "nature_reserve", "protected_area", "park"
    """
    boundary = tags.get("boundary", "")
    leisure = tags.get("leisure", "")
    protect_class = tags.get("protect_class", "")
    designation = tags.get("designation", "").lower()

    # National park indicators
    if boundary == "national_park":
        return "national"
    if protect_class == "2":
        return "national"
    if "national_park" in designation or "national park" in designation:
        return "national"

    # State/regional park indicators
    if protect_class == "5":
        return "state"
    if "state_park" in designation or "state park" in designation:
        return "state"
    if "regional_park" in designation or "regional park" in designation:
        return "state"
    if "provincial_park" in designation or "provincial park" in designation:
        return "state"

    # Nature reserve indicators
    if leisure == "nature_reserve":
        return "nature_reserve"
    if protect_class in {"1a", "1b"}:
        return "nature_reserve"
    if "nature_reserve" in designation or "nature reserve" in designation:
        return "nature_reserve"

    # Generic protected area
    if boundary == "protected_area":
        return "protected_area"

    # Generic park
    if leisure == "park":
        return "park"

    return "protected_area"


def matches_park_type(
    tags: dict[str, str], park_type: ParkType, protect_classes: set[str] | None = None
) -> bool:
    """Check if tags match the specified park type.

    Args:
        tags: OSM tags dictionary
        park_type: Type of park to match
        protect_classes: Set of protect_class values to accept

    Returns:
        True if tags match the criteria
    """
    boundary = tags.get("boundary", "")
    leisure = tags.get("leisure", "")
    protect_class = tags.get("protect_class", "")

    # First check if this is any kind of park/protected area
    is_park = (
        boundary in ("national_park", "protected_area")
        or leisure in ("park", "nature_reserve")
        or protect_class != ""
    )

    if not is_park:
        return False

    # If protect_classes filter is specified, check it
    if protect_classes and protect_class and protect_class not in protect_classes:
        return False

    if park_type == ParkType.ALL:
        return True

    classification = classify_park(tags)

    if park_type == ParkType.NATIONAL:
        return classification == "national"
    elif park_type == ParkType.STATE:
        return classification == "state"
    elif park_type == ParkType.NATURE_RESERVE:
        return classification == "nature_reserve"
    elif park_type == ParkType.PROTECTED_AREA:
        return classification in ("protected_area", "national", "state", "nature_reserve")

    return False


def calculate_area_km2(geometry: dict) -> float:
    """Calculate the area of a geometry in square kilometers.

    Uses geodesic calculation with pyproj if available, falls back to
    approximate spherical calculation.
    """
    if not geometry:
        return 0.0

    if not HAS_SHAPELY:
        return 0.0

    try:
        geom = shape(geometry)

        if not geom.is_valid:
            geom = geom.buffer(0)

        if HAS_PYPROJ:
            # Use equal-area projection for accurate measurement
            # Albers Equal Area centered on geometry centroid
            centroid = geom.centroid
            proj_string = (
                f"+proj=aea +lat_1={centroid.y - 5} +lat_2={centroid.y + 5} "
                f"+lat_0={centroid.y} +lon_0={centroid.x} +datum=WGS84 +units=m"
            )
            project = pyproj.Transformer.from_crs(
                "EPSG:4326", proj_string, always_xy=True
            ).transform
            projected = transform(project, geom)
            area_m2 = projected.area
        else:
            # Approximate using spherical earth
            # This is less accurate but works without pyproj
            bounds = geom.bounds  # (minx, miny, maxx, maxy)
            mid_lat = (bounds[1] + bounds[3]) / 2
            # Approximate meters per degree at this latitude
            m_per_deg_lat = 111132.92
            m_per_deg_lon = 111132.92 * math.cos(math.radians(mid_lat))

            # Very rough approximation - scale geometry to approximate meters
            # This only works for simple cases
            area_deg2 = geom.area
            area_m2 = area_deg2 * m_per_deg_lat * m_per_deg_lon

        return area_m2 / 1_000_000  # Convert to km²

    except Exception as e:
        log.debug("Could not calculate area: %s", e)
        return 0.0


def filter_parks_by_type(
    input_path: str | Path,
    park_type: str | ParkType,
    protect_classes: str = "*",
    output_path: str | Path | None = None,
) -> ParkFeatures:
    """Filter existing GeoJSON by park type.

    Args:
        input_path: Path to input GeoJSON file
        park_type: Type of park to filter for
        protect_classes: Comma-separated protect classes or "*" for all
        output_path: Path to output GeoJSON file

    Returns:
        ParkFeatures with output path and statistics
    """
    input_path = str(input_path)

    # Parse park type
    if isinstance(park_type, str):
        park_type = ParkType.from_string(park_type)

    # Parse protect classes
    protect_class_set = parse_protect_classes(protect_classes)

    # Generate output path if not provided
    if output_path is None:
        stem = uri_stem(input_path)
        import posixpath

        _dir = posixpath.dirname(input_path)
        output_path = f"{_dir}/{stem}_{park_type.value}.geojson"
    output_path = str(output_path)

    # Load input GeoJSON
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    # Filter features
    filtered = []
    total_area = 0.0

    for feature in features:
        props = feature.get("properties", {})

        # Features from the combined scan already have a `park_type`
        # classification — match against it directly.  Fall back to
        # raw OSM tag matching only when `park_type` is absent.
        classified = props.get("park_type", "")
        if classified:
            if park_type == ParkType.ALL:
                match = True
            elif park_type == ParkType.PROTECTED_AREA:
                match = classified in ("protected_area", "national", "state", "nature_reserve")
            else:
                match = classified == park_type.value
            if match:
                pc = props.get("protect_class", "")
                if (
                    protect_classes != "*"
                    and protect_class_set
                    and pc
                    and pc not in protect_class_set
                ):
                    continue
                filtered.append(feature)
                total_area += props.get("area_km2", 0.0)
            continue

        # Raw OSM tags — used when filtering PBF-extracted data directly
        tags = {
            "boundary": props.get("boundary", ""),
            "leisure": props.get("leisure", ""),
            "protect_class": props.get("protect_class", ""),
            "designation": props.get("designation", ""),
        }

        if matches_park_type(
            tags, park_type, protect_class_set if protect_classes != "*" else None
        ):
            filtered.append(feature)
            total_area += props.get("area_km2", 0.0)

    # Build output GeoJSON
    output_geojson = {
        "type": "FeatureCollection",
        "features": filtered,
    }

    # Write output
    with open_output(str(output_path)) as f:
        json.dump(output_geojson, f, indent=2)

    return ParkFeatures(
        output_path=str(output_path),
        feature_count=len(filtered),
        park_type=park_type.value,
        protect_classes=protect_classes,
        total_area_km2=round(total_area, 2),
        extraction_date=datetime.now(UTC).isoformat(),
    )


def calculate_park_stats(input_path: str | Path) -> ParkStats:
    """Calculate statistics for extracted parks.

    Args:
        input_path: Path to GeoJSON file with parks

    Returns:
        ParkStats with counts and areas by type
    """
    input_path = str(input_path)

    # Load GeoJSON
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    total_area = 0.0
    national_count = 0
    state_count = 0
    reserve_count = 0
    other_count = 0
    park_type = ""

    for feature in features:
        props = feature.get("properties", {})
        classification = props.get("park_type", "")
        area = props.get("area_km2", 0.0)

        if not park_type:
            park_type = classification

        total_area += area

        if classification == "national":
            national_count += 1
        elif classification == "state":
            state_count += 1
        elif classification == "nature_reserve":
            reserve_count += 1
        else:
            other_count += 1

    return ParkStats(
        total_parks=len(features),
        total_area_km2=round(total_area, 2),
        national_parks=national_count,
        state_parks=state_count,
        nature_reserves=reserve_count,
        other_protected=other_count,
        park_type=park_type if park_type else "mixed",
    )
