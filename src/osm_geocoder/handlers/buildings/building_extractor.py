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


def extract_buildings(
    pbf_path: str | Path,
    building_type: str = "all",
    output_path: str | Path | None = None,
    heartbeat=None,
    task_uuid: str = "",
) -> BuildingFeatures:
    """Extract buildings from a PBF as GeoJSON Polygons.

    Assembles closed ways and multipolygon relations into areas, keeps those
    carrying a ``building`` tag, and — unless ``building_type`` is ``"all"`` —
    only those whose :func:`classify_building` class matches (e.g.
    ``"residential"``, ``"commercial"``). Every OSM tag is preserved plus a
    derived ``building_type``; total footprint area (km²) is summed and features
    carrying ``height`` or ``building:levels`` are counted.

    Note: a full-region building extract is large and slow (millions of areas);
    prefer a ``building_type`` filter, and see the roadmap on category caches.
    """
    import os
    import shutil
    import tempfile
    from datetime import UTC, datetime

    import osmium
    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize
    from shapely import wkb as shapely_wkb
    from shapely.geometry import mapping as shapely_mapping

    from ..shared._output import ensure_dir, resolve_output_dir, uri_stem
    from ..shared.geojson_writer import GeoJSONStreamWriter

    pbf_path = str(pbf_path)
    if output_path is None:
        out_dir = resolve_output_dir("osm-buildings")
        output_path_str = f"{out_dir}/{uri_stem(pbf_path)}_buildings_{building_type}.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    local_pbf = localize(pbf_path)
    wkbfab = osmium.geom.WKBFactory()

    class _BuildingHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.kept = 0
            self.area_m2 = 0.0
            self.with_height = 0

        def area(self, a) -> None:
            tags = {t.k: t.v for t in a.tags}
            if "building" not in tags:
                return
            bt = classify_building(tags)
            if building_type != "all" and bt != building_type:
                return
            try:
                geom = shapely_mapping(
                    shapely_wkb.loads(wkbfab.create_multipolygon(a), hex=True)
                )
            except Exception:
                return  # incomplete geometry — skip
            props = dict(tags)
            props["osm_id"] = a.orig_id()
            props["building_type"] = bt
            self.writer.write_feature(
                {"type": "Feature", "properties": props, "geometry": geom}
            )
            self.kept += 1
            self.area_m2 += calculate_building_area(geom)
            if "height" in tags or "building:levels" in tags:
                self.with_height += 1
            if heartbeat and self.kept % 5000 == 0:
                heartbeat()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            handler = _BuildingHandler(writer)
            handler.apply_file(local_pbf, locations=True, idx="flex_mem")
        ensure_dir(output_path_str)
        shutil.move(tmp_path, output_path_str)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return BuildingFeatures(
        output_path=output_path_str,
        feature_count=handler.kept,
        building_type=building_type,
        total_area_km2=round(handler.area_m2 / 1_000_000, 4),
        with_height_data=handler.with_height,
        extraction_date=datetime.now(UTC).isoformat(),
    )


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
