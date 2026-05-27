"""Road network extraction from OSM data.

Extracts road network with attributes:
- Classification (motorway, primary, secondary, residential)
- Speed limits, surface type, lane count
- One-way status, access restrictions
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from facetwork.runtime.storage import get_storage_backend

from ..shared._output import (
    ensure_dir,
    finalize_output_file,
    open_output,
    resolve_output_dir,
    uri_stem,
)

_storage = get_storage_backend()

log = logging.getLogger(__name__)

import importlib.util

HAS_OSMIUM = importlib.util.find_spec("osmium") is not None


class RoadClass(Enum):
    """Road classifications."""

    MOTORWAY = "motorway"
    TRUNK = "trunk"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    RESIDENTIAL = "residential"
    SERVICE = "service"
    UNCLASSIFIED = "unclassified"
    TRACK = "track"
    PATH = "path"
    ALL = "all"

    @classmethod
    def from_string(cls, value: str) -> "RoadClass":
        normalized = value.lower().strip()
        aliases = {
            "motorway": cls.MOTORWAY,
            "highway": cls.MOTORWAY,
            "trunk": cls.TRUNK,
            "primary": cls.PRIMARY,
            "secondary": cls.SECONDARY,
            "tertiary": cls.TERTIARY,
            "residential": cls.RESIDENTIAL,
            "service": cls.SERVICE,
            "unclassified": cls.UNCLASSIFIED,
            "track": cls.TRACK,
            "path": cls.PATH,
            "all": cls.ALL,
            "*": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown road class: {value}")


# OSM highway values by road class
ROAD_CLASS_MAP = {
    "motorway": RoadClass.MOTORWAY,
    "motorway_link": RoadClass.MOTORWAY,
    "trunk": RoadClass.TRUNK,
    "trunk_link": RoadClass.TRUNK,
    "primary": RoadClass.PRIMARY,
    "primary_link": RoadClass.PRIMARY,
    "secondary": RoadClass.SECONDARY,
    "secondary_link": RoadClass.SECONDARY,
    "tertiary": RoadClass.TERTIARY,
    "tertiary_link": RoadClass.TERTIARY,
    "residential": RoadClass.RESIDENTIAL,
    "living_street": RoadClass.RESIDENTIAL,
    "service": RoadClass.SERVICE,
    "unclassified": RoadClass.UNCLASSIFIED,
    "track": RoadClass.TRACK,
    "path": RoadClass.PATH,
    "footway": RoadClass.PATH,
    "cycleway": RoadClass.PATH,
    "bridleway": RoadClass.PATH,
}

MAJOR_ROAD_CLASSES = {RoadClass.MOTORWAY, RoadClass.TRUNK, RoadClass.PRIMARY, RoadClass.SECONDARY}

PAVED_SURFACES = {
    "asphalt",
    "concrete",
    "paved",
    "concrete:plates",
    "concrete:lanes",
    "paving_stones",
    "sett",
    "cobblestone",
}
UNPAVED_SURFACES = {
    "unpaved",
    "gravel",
    "dirt",
    "sand",
    "grass",
    "ground",
    "earth",
    "mud",
    "compacted",
    "fine_gravel",
}


@dataclass
class RoadFeatures:
    """Result of a road extraction operation."""

    output_path: str
    feature_count: int
    road_class: str
    total_length_km: float
    with_speed_limit: int
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class RoadStats:
    """Statistics for extracted roads."""

    total_roads: int
    total_length_km: float
    motorway_km: float
    primary_km: float
    secondary_km: float
    tertiary_km: float
    residential_km: float
    other_km: float
    with_speed_limit: int
    with_surface: int
    with_lanes: int
    one_way_count: int


def classify_road(tags: dict[str, str]) -> str:
    """Classify a road based on its highway tag."""
    highway = tags.get("highway", "")
    if highway in ROAD_CLASS_MAP:
        return ROAD_CLASS_MAP[highway].value
    return "other"


def parse_speed_limit(value: str | None) -> int | None:
    """Parse speed limit from OSM maxspeed tag."""
    if not value:
        return None
    try:
        # Handle mph conversion
        if "mph" in value.lower():
            cleaned = value.lower().replace("mph", "").strip()
            return int(float(cleaned) * 1.60934)
        # Remove km/h suffix if present
        cleaned = value.replace("km/h", "").replace("kmh", "").strip()
        return int(float(cleaned))
    except ValueError:
        return None


def parse_lanes(value: str | None) -> int | None:
    """Parse lane count from OSM lanes tag."""
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def calculate_line_length(geometry: dict) -> float:
    """Calculate line length in kilometers."""
    if not geometry or geometry.get("type") not in ("LineString", "MultiLineString"):
        return 0.0

    try:
        coords = geometry.get("coordinates", [])
        if geometry["type"] == "MultiLineString":
            total = 0.0
            for line_coords in coords:
                total += _haversine_length(line_coords)
            return total
        return _haversine_length(coords)
    except Exception:
        return 0.0


def _haversine_length(coords: list) -> float:
    """Calculate length of a coordinate list using Haversine formula."""
    if len(coords) < 2:
        return 0.0

    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i][:2]
        lon2, lat2 = coords[i + 1][:2]

        # Haversine formula
        R = 6371  # Earth radius in km
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)

        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        total += R * c

    return total


def extract_roads(
    pbf_path: str | Path,
    road_class: str = "all",
    output_path: str | Path | None = None,
    heartbeat=None,
    task_uuid: str = "",
) -> RoadFeatures:
    """Extract highway ways from a PBF as GeoJSON LineStrings.

    Iterates ways carrying a ``highway`` tag, builds geometry via osmium's
    ``WKBFactory``, classifies each via :func:`classify_road`, and — unless
    ``road_class == "all"`` — keeps only the requested class (e.g.
    ``"motorway"``). Every OSM tag is preserved as a feature property —
    including ``ref`` (route numbers like ``I 5``) and ``name`` — plus a derived
    ``road_class``. Streams to a temp file then moves into place (multi-GB safe).
    """
    import os
    import tempfile

    import osmium
    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize
    from shapely import wkb as shapely_wkb
    from shapely.geometry import mapping as shapely_mapping

    from ..shared.geojson_writer import GeoJSONStreamWriter

    pbf_path = str(pbf_path)
    if output_path is None:
        out_dir = resolve_output_dir("osm-roads")
        output_path_str = f"{out_dir}/{uri_stem(pbf_path)}_roads_{road_class}.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    local_pbf = localize(pbf_path)
    wkbfab = osmium.geom.WKBFactory()

    class _RoadHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.original = 0
            self.kept = 0
            self.length_km = 0.0
            self.with_speed = 0

        def way(self, w) -> None:
            tags = {t.k: t.v for t in w.tags}
            if "highway" not in tags:
                return
            self.original += 1
            rc = classify_road(tags)
            if road_class != "all" and rc != road_class:
                return
            try:
                geom = shapely_mapping(
                    shapely_wkb.loads(wkbfab.create_linestring(w), hex=True)
                )
            except Exception:
                return  # incomplete geometry (missing nodes) — skip
            props = dict(tags)
            props["osm_id"] = w.id
            props["road_class"] = rc
            # Preserve the OSM node-id sequence (aligned 1:1 with the geometry
            # coordinates — the WKB linestring is built from these same nodes).
            # osm.Network.BuildNetwork uses it to build a graph on SHARED node ids
            # (exact topology, no coordinate-snapping): ways referencing the same
            # node id connect, including across per-region extract seams, since
            # OSM node ids are global. See node_id_graph() in network_ops.py.
            props["node_ids"] = [n.ref for n in w.nodes]
            self.writer.write_feature(
                {"type": "Feature", "properties": props, "geometry": geom}
            )
            self.kept += 1
            self.length_km += calculate_line_length(geom)
            if "maxspeed" in tags:
                self.with_speed += 1
            if heartbeat and self.kept % 5000 == 0:
                heartbeat()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            handler = _RoadHandler(writer)
            handler.apply_file(local_pbf, locations=True, idx="flex_mem")
        finalize_output_file(tmp_path, output_path_str)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return RoadFeatures(
        output_path=output_path_str,
        feature_count=handler.kept,
        road_class=road_class,
        total_length_km=round(handler.length_km, 2),
        with_speed_limit=handler.with_speed,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def calculate_road_stats(input_path: str | Path) -> RoadStats:
    """Calculate statistics for extracted roads."""
    input_path = str(input_path)
    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    total_length = 0.0
    motorway_km = primary_km = secondary_km = tertiary_km = residential_km = other_km = 0.0
    with_speed = with_surface = with_lanes = one_way = 0

    for feature in features:
        props = feature.get("properties", {})
        length = props.get("length_km", 0)
        total_length += length

        road_class = props.get("road_class", "other")
        if road_class == "motorway":
            motorway_km += length
        elif road_class == "primary":
            primary_km += length
        elif road_class == "secondary":
            secondary_km += length
        elif road_class == "tertiary":
            tertiary_km += length
        elif road_class == "residential":
            residential_km += length
        else:
            other_km += length

        if props.get("maxspeed"):
            with_speed += 1
        if props.get("surface"):
            with_surface += 1
        if props.get("lanes"):
            with_lanes += 1
        if props.get("oneway"):
            one_way += 1

    return RoadStats(
        total_roads=len(features),
        total_length_km=round(total_length, 2),
        motorway_km=round(motorway_km, 2),
        primary_km=round(primary_km, 2),
        secondary_km=round(secondary_km, 2),
        tertiary_km=round(tertiary_km, 2),
        residential_km=round(residential_km, 2),
        other_km=round(other_km, 2),
        with_speed_limit=with_speed,
        with_surface=with_surface,
        with_lanes=with_lanes,
        one_way_count=one_way,
    )


def filter_roads_by_class(
    input_path: str | Path,
    road_class: str,
    output_path: str | Path | None = None,
) -> RoadFeatures:
    """Filter roads by classification."""
    input_path = str(input_path)

    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    filtered = [
        f
        for f in geojson.get("features", [])
        if f.get("properties", {}).get("road_class") == road_class
    ]

    if output_path is None:
        import posixpath

        _dir = posixpath.dirname(input_path)
        output_path_str = f"{_dir}/{uri_stem(input_path)}_{road_class}.geojson"
    else:
        output_path_str = str(output_path)

    output_geojson = {"type": "FeatureCollection", "features": filtered}
    with open_output(output_path_str) as f:
        json.dump(output_geojson, f, indent=2)

    total_length = sum(f["properties"].get("length_km", 0) for f in filtered)
    with_speed = sum(1 for f in filtered if f["properties"].get("maxspeed"))

    return RoadFeatures(
        output_path=output_path_str,
        feature_count=len(filtered),
        road_class=road_class,
        total_length_km=round(total_length, 2),
        with_speed_limit=with_speed,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def filter_by_speed_limit(
    input_path: str | Path,
    min_speed: int,
    max_speed: int,
    output_path: str | Path | None = None,
) -> RoadFeatures:
    """Filter roads by speed limit range."""
    input_path = str(input_path)

    with get_storage_backend(input_path).open(input_path, "r") as f:
        geojson = json.load(f)

    filtered = []
    for f in geojson.get("features", []):
        speed = f.get("properties", {}).get("maxspeed")
        if speed is not None and min_speed <= speed <= max_speed:
            filtered.append(f)

    if output_path is None:
        import posixpath

        _dir = posixpath.dirname(input_path)
        output_path_str = f"{_dir}/{uri_stem(input_path)}_speed_{min_speed}_{max_speed}.geojson"
    else:
        output_path_str = str(output_path)

    output_geojson = {"type": "FeatureCollection", "features": filtered}
    with open_output(output_path_str) as f:
        json.dump(output_geojson, f, indent=2)

    total_length = sum(f["properties"].get("length_km", 0) for f in filtered)

    return RoadFeatures(
        output_path=output_path_str,
        feature_count=len(filtered),
        road_class="filtered",
        total_length_km=round(total_length, 2),
        with_speed_limit=len(filtered),
        extraction_date=datetime.now(UTC).isoformat(),
    )
