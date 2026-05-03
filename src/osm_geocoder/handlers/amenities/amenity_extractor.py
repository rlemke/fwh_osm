"""Amenity extraction from OSM data.

Extracts amenities by category: food, shopping, services, healthcare, education, entertainment.
"""

import json
import logging
import re
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


class AmenityCategory(Enum):
    """Amenity categories."""

    FOOD = "food"
    SHOPPING = "shopping"
    SERVICES = "services"
    HEALTHCARE = "healthcare"
    EDUCATION = "education"
    ENTERTAINMENT = "entertainment"
    TRANSPORT = "transport"
    ALL = "all"

    @classmethod
    def from_string(cls, value: str) -> "AmenityCategory":
        normalized = value.lower().strip()
        aliases = {
            "food": cls.FOOD,
            "food_and_drink": cls.FOOD,
            "restaurant": cls.FOOD,
            "shopping": cls.SHOPPING,
            "shop": cls.SHOPPING,
            "retail": cls.SHOPPING,
            "services": cls.SERVICES,
            "service": cls.SERVICES,
            "healthcare": cls.HEALTHCARE,
            "health": cls.HEALTHCARE,
            "medical": cls.HEALTHCARE,
            "education": cls.EDUCATION,
            "school": cls.EDUCATION,
            "entertainment": cls.ENTERTAINMENT,
            "leisure": cls.ENTERTAINMENT,
            "transport": cls.TRANSPORT,
            "transportation": cls.TRANSPORT,
            "all": cls.ALL,
            "*": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown amenity category: {value}")


# Amenity tag to category mapping
AMENITY_CATEGORIES = {
    # Food & Drink
    "restaurant": AmenityCategory.FOOD,
    "cafe": AmenityCategory.FOOD,
    "bar": AmenityCategory.FOOD,
    "pub": AmenityCategory.FOOD,
    "fast_food": AmenityCategory.FOOD,
    "food_court": AmenityCategory.FOOD,
    "ice_cream": AmenityCategory.FOOD,
    "biergarten": AmenityCategory.FOOD,
    # Services
    "bank": AmenityCategory.SERVICES,
    "atm": AmenityCategory.SERVICES,
    "post_office": AmenityCategory.SERVICES,
    "fuel": AmenityCategory.SERVICES,
    "car_wash": AmenityCategory.SERVICES,
    "car_rental": AmenityCategory.SERVICES,
    "charging_station": AmenityCategory.SERVICES,
    "parking": AmenityCategory.SERVICES,
    # Healthcare
    "hospital": AmenityCategory.HEALTHCARE,
    "clinic": AmenityCategory.HEALTHCARE,
    "doctors": AmenityCategory.HEALTHCARE,
    "dentist": AmenityCategory.HEALTHCARE,
    "pharmacy": AmenityCategory.HEALTHCARE,
    "veterinary": AmenityCategory.HEALTHCARE,
    # Education
    "school": AmenityCategory.EDUCATION,
    "university": AmenityCategory.EDUCATION,
    "college": AmenityCategory.EDUCATION,
    "library": AmenityCategory.EDUCATION,
    "kindergarten": AmenityCategory.EDUCATION,
    # Entertainment
    "cinema": AmenityCategory.ENTERTAINMENT,
    "theatre": AmenityCategory.ENTERTAINMENT,
    "nightclub": AmenityCategory.ENTERTAINMENT,
    "casino": AmenityCategory.ENTERTAINMENT,
    "arts_centre": AmenityCategory.ENTERTAINMENT,
    # Transport
    "bus_station": AmenityCategory.TRANSPORT,
    "ferry_terminal": AmenityCategory.TRANSPORT,
    "taxi": AmenityCategory.TRANSPORT,
}

# Specific amenity types for typed handlers
FOOD_AMENITIES = {
    "restaurant",
    "cafe",
    "bar",
    "pub",
    "fast_food",
    "food_court",
    "ice_cream",
    "biergarten",
}
SHOPPING_TAGS = {
    "supermarket",
    "convenience",
    "mall",
    "department_store",
    "clothes",
    "shoes",
    "electronics",
}
HEALTHCARE_AMENITIES = {"hospital", "clinic", "doctors", "dentist", "pharmacy", "veterinary"}
EDUCATION_AMENITIES = {"school", "university", "college", "library", "kindergarten"}
ENTERTAINMENT_AMENITIES = {"cinema", "theatre", "nightclub", "casino", "arts_centre"}


@dataclass
class AmenityFeatures:
    """Result of an amenity extraction."""

    output_path: str
    feature_count: int
    amenity_category: str
    amenity_types: str
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class AmenityStats:
    """Statistics for extracted amenities."""

    total_amenities: int
    food: int
    shopping: int
    services: int
    healthcare: int
    education: int
    entertainment: int
    transport: int
    other: int
    with_name: int
    with_opening_hours: int


def classify_amenity(tags: dict[str, str]) -> str:
    """Classify an amenity based on its tags."""
    amenity = tags.get("amenity", "")
    if amenity in AMENITY_CATEGORIES:
        return AMENITY_CATEGORIES[amenity].value

    # Check shop tag
    if "shop" in tags:
        return AmenityCategory.SHOPPING.value

    # Check tourism tag
    tourism = tags.get("tourism", "")
    if tourism in ("hotel", "motel", "hostel", "guest_house"):
        return AmenityCategory.SERVICES.value
    if tourism in ("museum", "gallery", "zoo", "theme_park"):
        return AmenityCategory.ENTERTAINMENT.value

    return "other"


def calculate_amenity_stats(input_path: str | Path) -> AmenityStats:
    """Calculate statistics for extracted amenities."""
    input_path = str(input_path)
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    food = shopping = services = healthcare = education = entertainment = transport = other = 0
    with_name = with_hours = 0

    for feature in features:
        props = feature.get("properties", {})
        category = props.get("category", "other")

        if category == "food":
            food += 1
        elif category == "shopping":
            shopping += 1
        elif category == "services":
            services += 1
        elif category == "healthcare":
            healthcare += 1
        elif category == "education":
            education += 1
        elif category == "entertainment":
            entertainment += 1
        elif category == "transport":
            transport += 1
        else:
            other += 1

        if props.get("name"):
            with_name += 1
        if props.get("opening_hours"):
            with_hours += 1

    return AmenityStats(
        total_amenities=len(features),
        food=food,
        shopping=shopping,
        services=services,
        healthcare=healthcare,
        education=education,
        entertainment=entertainment,
        transport=transport,
        other=other,
        with_name=with_name,
        with_opening_hours=with_hours,
    )


def search_amenities(
    input_path: str | Path, name_pattern: str, output_path: str | Path | None = None
) -> AmenityFeatures:
    """Search amenities by name pattern."""
    input_path = str(input_path)

    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    pattern = re.compile(name_pattern, re.IGNORECASE)
    filtered = []

    for feature in geojson.get("features", []):
        name = feature.get("properties", {}).get("name", "")
        if pattern.search(name):
            filtered.append(feature)

    if output_path is None:
        import posixpath

        _dir = posixpath.dirname(input_path)
        output_path_str = f"{_dir}/{uri_stem(input_path)}_search.geojson"
    else:
        output_path_str = str(output_path)

    output_geojson = {"type": "FeatureCollection", "features": filtered}
    with open_output(output_path_str) as f:
        json.dump(output_geojson, f, indent=2)

    return AmenityFeatures(
        output_path=output_path_str,
        feature_count=len(filtered),
        amenity_category="search",
        amenity_types=name_pattern,
        extraction_date=datetime.now(UTC).isoformat(),
    )
