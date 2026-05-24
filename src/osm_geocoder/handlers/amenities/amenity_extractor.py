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


def extract_amenities(
    pbf_path: str | Path,
    category: str = "all",
    output_path: str | Path | None = None,
    heartbeat=None,
    task_uuid: str = "",
) -> AmenityFeatures:
    """Extract amenity / shop / tourism features from a PBF as GeoJSON Points.

    Walks nodes and ways carrying an ``amenity`` or ``shop`` tag (or a
    recognised ``tourism`` value), classifies each via :func:`classify_amenity`,
    and — unless ``category`` is ``"all"``/``"*"`` — keeps only the requested
    category (e.g. ``"food"``). Nodes become Points; closed ways become their
    centroid Point, so the output is a uniform clickable point layer. Every OSM
    tag is preserved as a feature property (``name``, ``cuisine``, ``brand`` …)
    plus a derived ``category``. Streams to a temp file then moves into place
    (multi-GB safe).
    """
    import os
    import shutil
    import tempfile

    import osmium
    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize
    from shapely import wkb as shapely_wkb

    from ..shared._output import ensure_dir, resolve_output_dir
    from ..shared.geojson_writer import GeoJSONStreamWriter

    pbf_path = str(pbf_path)
    cat = AmenityCategory.from_string(category)
    target = None if cat == AmenityCategory.ALL else cat.value

    if output_path is None:
        out_dir = resolve_output_dir("osm-amenities")
        output_path_str = f"{out_dir}/{uri_stem(pbf_path)}_amenities_{cat.value}.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    local_pbf = localize(pbf_path)
    wkbfab = osmium.geom.WKBFactory()

    def _is_amenity(tags: dict[str, str]) -> bool:
        return (
            "amenity" in tags
            or "shop" in tags
            or tags.get("tourism", "")
            in ("hotel", "motel", "hostel", "guest_house", "museum", "gallery", "zoo", "theme_park")
        )

    class _AmenityHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.kept = 0
            self.types: set[str] = set()

        def _emit(self, tags: dict[str, str], geom: dict, osm_id: int) -> None:
            cls = classify_amenity(tags)
            if target is not None and cls != target:
                return
            props = dict(tags)
            props["osm_id"] = osm_id
            props["category"] = cls
            self.writer.write_feature(
                {"type": "Feature", "properties": props, "geometry": geom}
            )
            self.kept += 1
            atype = tags.get("amenity") or tags.get("shop") or tags.get("tourism")
            if atype:
                self.types.add(atype)
            if heartbeat and self.kept % 5000 == 0:
                heartbeat()

        def node(self, n) -> None:
            if not n.location.valid():
                return
            tags = {t.k: t.v for t in n.tags}
            if not _is_amenity(tags):
                return
            self._emit(
                tags, {"type": "Point", "coordinates": [n.location.lon, n.location.lat]}, n.id
            )

        def way(self, w) -> None:
            tags = {t.k: t.v for t in w.tags}
            if not _is_amenity(tags):
                return
            try:
                centroid = shapely_wkb.loads(wkbfab.create_linestring(w), hex=True).centroid
            except Exception:
                return  # incomplete geometry (missing nodes) — skip
            self._emit(tags, {"type": "Point", "coordinates": [centroid.x, centroid.y]}, w.id)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            handler = _AmenityHandler(writer)
            handler.apply_file(local_pbf, locations=True, idx="flex_mem")
        ensure_dir(output_path_str)
        shutil.move(tmp_path, output_path_str)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return AmenityFeatures(
        output_path=output_path_str,
        feature_count=handler.kept,
        amenity_category=cat.value,
        amenity_types=",".join(sorted(handler.types)[:25]),
        extraction_date=datetime.now(UTC).isoformat(),
    )


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
