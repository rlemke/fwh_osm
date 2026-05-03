"""Building extraction from OSM data.

Extracts building footprints with classification:
- Residential, commercial, industrial, retail
- Building height/levels for 3D visualization
"""

import json
import logging
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from facetwork.runtime.storage import get_storage_backend

_storage = get_storage_backend()

log = logging.getLogger(__name__)

import importlib.util

HAS_OSMIUM = importlib.util.find_spec("osmium") is not None

# Check for shapely availability
try:
    import shapely  # noqa: F401

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False


class BuildingType(Enum):
    """Building type classifications."""

    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"
    RETAIL = "retail"
    OFFICE = "office"
    PUBLIC = "public"
    RELIGIOUS = "religious"
    ALL = "all"

    @classmethod
    def from_string(cls, value: str) -> "BuildingType":
        """Parse a building type string."""
        normalized = value.lower().strip()
        aliases = {
            "residential": cls.RESIDENTIAL,
            "house": cls.RESIDENTIAL,
            "apartments": cls.RESIDENTIAL,
            "commercial": cls.COMMERCIAL,
            "industrial": cls.INDUSTRIAL,
            "warehouse": cls.INDUSTRIAL,
            "retail": cls.RETAIL,
            "shop": cls.RETAIL,
            "supermarket": cls.RETAIL,
            "office": cls.OFFICE,
            "public": cls.PUBLIC,
            "civic": cls.PUBLIC,
            "government": cls.PUBLIC,
            "religious": cls.RELIGIOUS,
            "church": cls.RELIGIOUS,
            "mosque": cls.RELIGIOUS,
            "all": cls.ALL,
            "*": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown building type: {value}")


# OSM building tag to type mapping
BUILDING_TYPE_MAP = {
    # Residential
    "house": BuildingType.RESIDENTIAL,
    "residential": BuildingType.RESIDENTIAL,
    "apartments": BuildingType.RESIDENTIAL,
    "detached": BuildingType.RESIDENTIAL,
    "semidetached_house": BuildingType.RESIDENTIAL,
    "terrace": BuildingType.RESIDENTIAL,
    "dormitory": BuildingType.RESIDENTIAL,
    # Commercial
    "commercial": BuildingType.COMMERCIAL,
    "hotel": BuildingType.COMMERCIAL,
    "office": BuildingType.OFFICE,
    # Industrial
    "industrial": BuildingType.INDUSTRIAL,
    "warehouse": BuildingType.INDUSTRIAL,
    "factory": BuildingType.INDUSTRIAL,
    "manufacture": BuildingType.INDUSTRIAL,
    # Retail
    "retail": BuildingType.RETAIL,
    "supermarket": BuildingType.RETAIL,
    "kiosk": BuildingType.RETAIL,
    # Public
    "public": BuildingType.PUBLIC,
    "civic": BuildingType.PUBLIC,
    "government": BuildingType.PUBLIC,
    "hospital": BuildingType.PUBLIC,
    "school": BuildingType.PUBLIC,
    "university": BuildingType.PUBLIC,
    "kindergarten": BuildingType.PUBLIC,
    # Religious
    "church": BuildingType.RELIGIOUS,
    "chapel": BuildingType.RELIGIOUS,
    "cathedral": BuildingType.RELIGIOUS,
    "mosque": BuildingType.RELIGIOUS,
    "temple": BuildingType.RELIGIOUS,
    "synagogue": BuildingType.RELIGIOUS,
}


@dataclass
class BuildingFeatures:
    """Result of a building extraction operation."""

    output_path: str
    feature_count: int
    building_type: str
    total_area_km2: float
    with_height_data: int
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class BuildingStats:
    """Statistics for extracted buildings."""

    total_buildings: int
    total_area_km2: float
    residential: int
    commercial: int
    industrial: int
    retail: int
    other: int
    avg_levels: float
    with_height: int


def classify_building(tags: dict[str, str]) -> str:
    """Classify a building based on its OSM tags."""
    building_tag = tags.get("building", "yes")

    if building_tag in BUILDING_TYPE_MAP:
        return BUILDING_TYPE_MAP[building_tag].value

    # Check amenity tag for public buildings
    amenity = tags.get("amenity", "")
    if amenity in ("hospital", "school", "university", "library", "townhall"):
        return BuildingType.PUBLIC.value

    # Check shop tag for retail
    if "shop" in tags:
        return BuildingType.RETAIL.value

    # Check office tag
    if "office" in tags:
        return BuildingType.OFFICE.value

    return "other"


def parse_height(value: str | None) -> float | None:
    """Parse building height from OSM tag."""
    if not value:
        return None
    try:
        # Remove units if present
        cleaned = value.replace("m", "").replace("ft", "").strip()
        return float(cleaned)
    except ValueError:
        return None


def parse_levels(value: str | None) -> int | None:
    """Parse building levels from OSM tag."""
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def calculate_building_area(geometry: dict) -> float:
    """Calculate building area in square meters."""
    if not geometry or not HAS_SHAPELY:
        return 0.0

    try:
        from shapely.geometry import shape

        geom = shape(geometry)

        # Approximate conversion (degrees to meters at mid-latitudes)
        bounds = geom.bounds
        mid_lat = (bounds[1] + bounds[3]) / 2
        m_per_deg = 111320 * math.cos(math.radians(mid_lat))

        return geom.area * m_per_deg * m_per_deg
    except Exception:
        return 0.0


def calculate_building_stats(input_path: str | Path) -> BuildingStats:
    """Calculate statistics for extracted buildings."""
    input_path = str(input_path)

    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    total_area = 0.0
    residential = commercial = industrial = retail = other = 0
    levels_sum = 0
    levels_count = 0
    with_height = 0

    for feature in features:
        props = feature.get("properties", {})
        total_area += props.get("area_m2", 0)

        building_type = props.get("building_type", "other")
        if building_type == "residential":
            residential += 1
        elif building_type == "commercial":
            commercial += 1
        elif building_type == "industrial":
            industrial += 1
        elif building_type == "retail":
            retail += 1
        else:
            other += 1

        levels = props.get("levels")
        if levels:
            levels_sum += levels
            levels_count += 1

        if props.get("height") or props.get("levels"):
            with_height += 1

    return BuildingStats(
        total_buildings=len(features),
        total_area_km2=round(total_area / 1_000_000, 4),
        residential=residential,
        commercial=commercial,
        industrial=industrial,
        retail=retail,
        other=other,
        avg_levels=round(levels_sum / levels_count, 1) if levels_count > 0 else 0.0,
        with_height=with_height,
    )
