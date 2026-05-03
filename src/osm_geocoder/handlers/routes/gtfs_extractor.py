"""GTFS transit feed extraction and analysis.

Parses GTFS static feeds (ZIP/CSV) to extract stops, routes, service
frequency, and compute coverage/accessibility metrics against OSM data.

Uses only Python stdlib (csv, zipfile, json, math) — no external
geo-dependencies required.
"""

import csv
import hashlib
import json
import logging
import math
import os
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum

from facetwork.runtime.storage import get_storage_backend

_storage = get_storage_backend()

log = logging.getLogger(__name__)

# Optional requests for HTTP downloads
try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Environment-based cache directory
from facetwork.config import get_output_base

_LOCAL_OUTPUT = get_output_base()
GTFS_CACHE_DIR = os.environ.get(
    "AFL_GTFS_CACHE_DIR", os.path.join(_LOCAL_OUTPUT, "osm", "gtfs-cache")
)

# Safety cap for grid-based analyses
MAX_GRID_CELLS = 10_000


# ── GTFS route type enum ────────────────────────────────────────────────


class GTFSRouteType(IntEnum):
    """GTFS route_type values (basic + extended)."""

    TRAM = 0
    SUBWAY = 1
    RAIL = 2
    BUS = 3
    FERRY = 4
    CABLE_TRAM = 5
    AERIAL_LIFT = 6
    FUNICULAR = 7
    TROLLEYBUS = 11
    MONORAIL = 12

    @classmethod
    def from_string(cls, value: str) -> "GTFSRouteType | None":
        """Parse a route_type string, returning None for unknown values."""
        try:
            return cls(int(value))
        except (ValueError, KeyError):
            return None

    def label(self) -> str:
        """Human-readable label for the route type."""
        labels = {
            0: "Tram",
            1: "Subway",
            2: "Rail",
            3: "Bus",
            4: "Ferry",
            5: "Cable Tram",
            6: "Aerial Lift",
            7: "Funicular",
            11: "Trolleybus",
            12: "Monorail",
        }
        return labels.get(self.value, f"Type {self.value}")


# ── Result dataclasses ──────────────────────────────────────────────────


@dataclass
class StopResult:
    """Result of stop extraction."""

    output_path: str
    stop_count: int
    bbox_min_lat: float = 0.0
    bbox_min_lon: float = 0.0
    bbox_max_lat: float = 0.0
    bbox_max_lon: float = 0.0
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class GTFSRouteFeatures:
    """Result of route extraction."""

    output_path: str
    route_count: int
    has_shapes: bool = False
    route_types: str = ""
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class FrequencyResult:
    """Result of service frequency computation."""

    output_path: str
    stop_count: int
    avg_trips_per_day: float = 0.0
    max_trips_per_day: int = 0
    service_date: str = ""
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class TransitStats:
    """Aggregate transit statistics."""

    agency_name: str
    stop_count: int
    route_count: int
    trip_count: int
    has_shapes: bool = False
    route_type_counts: str = ""
    extraction_date: str = ""


@dataclass
class NearestStopResult:
    """Result of nearest-stop lookup."""

    output_path: str
    feature_count: int
    matched_count: int = 0
    avg_distance_m: float = 0.0
    max_distance_m: float = 0.0
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class AccessibilityResult:
    """Result of stop accessibility analysis."""

    output_path: str
    total_features: int
    within_400m: int = 0
    within_800m: int = 0
    beyond_800m: int = 0
    pct_within_400m: float = 0.0
    pct_within_800m: float = 0.0
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class CoverageResult:
    """Result of coverage gap analysis."""

    output_path: str
    total_cells: int
    covered_cells: int = 0
    gap_cells: int = 0
    coverage_pct: float = 0.0
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class DensityResult:
    """Result of route density analysis."""

    output_path: str
    total_cells: int
    max_routes_per_cell: int = 0
    avg_routes_per_cell: float = 0.0
    format: str = "GeoJSON"
    extraction_date: str = ""


# ── Helpers ─────────────────────────────────────────────────────────────


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in meters between two points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_csv(feed_path: str, filename: str) -> list[dict[str, str]]:
    """Read a CSV file from an extracted GTFS feed directory.

    Handles UTF-8 BOM and missing files gracefully.
    """
    csv_path = os.path.join(feed_path, filename)
    try:
        with _storage.open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except FileNotFoundError:
        log.debug("GTFS file not found: %s", csv_path)
        return []
    except Exception as e:
        log.warning("Error reading %s: %s", csv_path, e)
        return []


def _feature_centroid(feature: dict) -> tuple[float, float] | None:
    """Compute a simple centroid for a GeoJSON feature.

    Returns (lat, lon) or None if geometry is missing/unsupported.
    """
    geom = feature.get("geometry")
    if not geom:
        return None

    gtype = geom.get("type", "")
    coords = geom.get("coordinates")
    if not coords:
        return None

    if gtype == "Point":
        # GeoJSON coords are [lon, lat]
        return (coords[1], coords[0])
    elif gtype == "LineString":
        lats = [c[1] for c in coords]
        lons = [c[0] for c in coords]
        return (sum(lats) / len(lats), sum(lons) / len(lons))
    elif gtype == "Polygon":
        ring = coords[0]  # outer ring
        lats = [c[1] for c in ring]
        lons = [c[0] for c in ring]
        return (sum(lats) / len(lats), sum(lons) / len(lons))
    elif gtype == "MultiPolygon":
        all_lats: list[float] = []
        all_lons: list[float] = []
        for polygon in coords:
            ring = polygon[0]
            all_lats.extend(c[1] for c in ring)
            all_lons.extend(c[0] for c in ring)
        if all_lats:
            return (sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons))
    return None


def _load_geojson_features(path: str) -> list[dict]:
    """Load features from a GeoJSON file."""
    try:
        with _storage.open(path, "r") as f:
            data = json.load(f)
        return data.get("features", [])
    except Exception as e:
        log.warning("Error loading GeoJSON %s: %s", path, e)
        return []


def _write_geojson(path: str, features: list[dict]) -> None:
    """Write a GeoJSON FeatureCollection to a file."""
    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }
    with _storage.open(path, "w") as f:
        json.dump(geojson, f, indent=2)


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat()


def _find_feed_root(extract_dir: str) -> str:
    """Find the root directory containing GTFS files.

    Some GTFS ZIPs have files in a subdirectory. Scan for stops.txt.
    """
    # Check top level first
    if os.path.isfile(os.path.join(extract_dir, "stops.txt")):
        return extract_dir

    # Check one level of subdirectories
    try:
        for entry in os.listdir(extract_dir):
            subdir = os.path.join(extract_dir, entry)
            if os.path.isdir(subdir) and os.path.isfile(os.path.join(subdir, "stops.txt")):
                return subdir
    except OSError:
        pass

    return extract_dir


# ── Core extraction functions ───────────────────────────────────────────


def download_gtfs_feed(url: str, output_dir: str = "") -> dict:
    """Download and extract a GTFS feed ZIP.

    Caches by URL hash to avoid re-downloading. Returns a dict matching
    the GTFSFeed schema.

    Args:
        url: URL of the GTFS ZIP file
        output_dir: Base directory for extraction (defaults to AFL_GTFS_CACHE_DIR)

    Returns:
        Dict with url, path, date, size, wasInCache, agency_name, has_shapes
    """
    if not output_dir:
        output_dir = GTFS_CACHE_DIR

    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_dir = os.path.join(output_dir, url_hash)
    feed_dir = os.path.join(cache_dir, "feed")
    meta_path = os.path.join(cache_dir, "meta.json")

    # Check cache
    if os.path.isfile(meta_path) and os.path.isdir(feed_dir):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            meta["wasInCache"] = True
            log.info("Using cached GTFS feed for %s at %s", url, feed_dir)
            return meta
        except Exception:
            pass

    # Download
    os.makedirs(feed_dir, exist_ok=True)
    zip_path = os.path.join(cache_dir, "feed.zip")

    if HAS_REQUESTS:
        log.info("Downloading GTFS feed from %s", url)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            f.write(resp.content)
        feed_size = len(resp.content)
    elif url.startswith("file://") or os.path.isfile(url):
        # Support local file paths for testing
        local_path = url.replace("file://", "") if url.startswith("file://") else url
        with open(local_path, "rb") as src:
            data = src.read()
        with open(zip_path, "wb") as f:
            f.write(data)
        feed_size = len(data)
    else:
        log.error("requests library not available and URL is not a local file: %s", url)
        return {
            "url": url,
            "path": "",
            "date": _now_iso(),
            "size": 0,
            "wasInCache": False,
            "agency_name": "",
            "has_shapes": False,
        }

    # Extract ZIP
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(feed_dir)
    except zipfile.BadZipFile as e:
        log.error("Invalid GTFS ZIP from %s: %s", url, e)
        return {
            "url": url,
            "path": "",
            "date": _now_iso(),
            "size": 0,
            "wasInCache": False,
            "agency_name": "",
            "has_shapes": False,
        }

    # Find the actual feed root (handles nested dirs)
    feed_root = _find_feed_root(feed_dir)

    # Read agency name
    agency_name = ""
    agency_rows = _read_csv(feed_root, "agency.txt")
    if agency_rows:
        agency_name = agency_rows[0].get("agency_name", "")

    # Check for shapes
    has_shapes = os.path.isfile(os.path.join(feed_root, "shapes.txt"))

    meta = {
        "url": url,
        "path": feed_root,
        "date": _now_iso(),
        "size": feed_size,
        "wasInCache": False,
        "agency_name": agency_name,
        "has_shapes": has_shapes,
    }

    # Cache metadata
    try:
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)
    except OSError as e:
        log.warning("Could not write cache metadata: %s", e)

    return meta


def extract_stops(feed_path: str, output_path: str = "") -> StopResult:
    """Extract stop locations from stops.txt to GeoJSON points.

    Filters to location_type=0 (or empty) for actual stop/platform locations.

    Args:
        feed_path: Path to extracted GTFS feed directory
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        StopResult with output path and bounding box
    """
    if not output_path:
        output_path = os.path.join(feed_path, "stops.geojson")

    rows = _read_csv(feed_path, "stops.txt")
    features: list[dict] = []
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")

    for row in rows:
        # Filter to stops only (location_type 0 or blank)
        loc_type = row.get("location_type", "0").strip()
        if loc_type not in ("", "0"):
            continue

        try:
            lat = float(row.get("stop_lat", "0"))
            lon = float(row.get("stop_lon", "0"))
        except (ValueError, TypeError):
            continue

        if lat == 0.0 and lon == 0.0:
            continue

        min_lat = min(min_lat, lat)
        min_lon = min(min_lon, lon)
        max_lat = max(max_lat, lat)
        max_lon = max(max_lon, lon)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "stop_id": row.get("stop_id", ""),
                    "stop_name": row.get("stop_name", ""),
                    "stop_code": row.get("stop_code", ""),
                    "zone_id": row.get("zone_id", ""),
                    "wheelchair_boarding": row.get("wheelchair_boarding", ""),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        )

    _write_geojson(output_path, features)

    if not features:
        min_lat = min_lon = max_lat = max_lon = 0.0

    return StopResult(
        output_path=output_path,
        stop_count=len(features),
        bbox_min_lat=round(min_lat, 6),
        bbox_min_lon=round(min_lon, 6),
        bbox_max_lat=round(max_lat, 6),
        bbox_max_lon=round(max_lon, 6),
        extraction_date=_now_iso(),
    )


def extract_routes(feed_path: str, output_path: str = "") -> GTFSRouteFeatures:
    """Extract route geometries from shapes.txt (or stop sequences) to GeoJSON.

    When shapes.txt is available, builds linestrings from shape points.
    Otherwise, degrades to point geometries at stop locations.

    Args:
        feed_path: Path to extracted GTFS feed directory
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        GTFSRouteFeatures with output path and route info
    """
    if not output_path:
        output_path = os.path.join(feed_path, "routes.geojson")

    routes_rows = _read_csv(feed_path, "routes.txt")
    trips_rows = _read_csv(feed_path, "trips.txt")
    shapes_rows = _read_csv(feed_path, "shapes.txt")

    # Build route lookup: route_id -> route info
    route_info: dict[str, dict] = {}
    for row in routes_rows:
        rid = row.get("route_id", "")
        route_info[rid] = {
            "route_id": rid,
            "route_short_name": row.get("route_short_name", ""),
            "route_long_name": row.get("route_long_name", ""),
            "route_type": row.get("route_type", "3"),
            "route_color": row.get("route_color", ""),
            "agency_id": row.get("agency_id", ""),
        }

    # Map trip_id -> (route_id, shape_id)
    trip_to_route: dict[str, str] = {}
    route_to_shape: dict[str, str] = {}
    for row in trips_rows:
        tid = row.get("trip_id", "")
        rid = row.get("route_id", "")
        sid = row.get("shape_id", "")
        trip_to_route[tid] = rid
        if sid and rid not in route_to_shape:
            route_to_shape[rid] = sid

    has_shapes = bool(shapes_rows)
    features: list[dict] = []
    route_types_seen: set[str] = set()

    if has_shapes:
        # Build shape geometries: shape_id -> sorted list of (seq, lon, lat)
        shape_points: dict[str, list[tuple[int, float, float]]] = {}
        for row in shapes_rows:
            sid = row.get("shape_id", "")
            try:
                seq = int(row.get("shape_pt_sequence", "0"))
                lat = float(row.get("shape_pt_lat", "0"))
                lon = float(row.get("shape_pt_lon", "0"))
            except (ValueError, TypeError):
                continue
            shape_points.setdefault(sid, []).append((seq, lon, lat))

        # Sort each shape by sequence
        for sid in shape_points:
            shape_points[sid].sort(key=lambda p: p[0])

        # One feature per route using its shape
        for rid, info in route_info.items():
            sid = route_to_shape.get(rid, "")
            if sid not in shape_points:
                continue

            coords = [[p[1], p[2]] for p in shape_points[sid]]
            if len(coords) < 2:
                continue

            rtype = info.get("route_type", "3")
            route_types_seen.add(rtype)
            rt = GTFSRouteType.from_string(rtype)

            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        **info,
                        "route_type_label": rt.label() if rt else f"Type {rtype}",
                        "shape_id": sid,
                        "point_count": len(coords),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                }
            )
    else:
        # Fallback: use stop sequence from stop_times.txt
        stop_times_rows = _read_csv(feed_path, "stop_times.txt")
        stops_rows = _read_csv(feed_path, "stops.txt")

        # stop_id -> (lat, lon)
        stop_coords: dict[str, tuple[float, float]] = {}
        for row in stops_rows:
            sid = row.get("stop_id", "")
            try:
                lat = float(row.get("stop_lat", "0"))
                lon = float(row.get("stop_lon", "0"))
                stop_coords[sid] = (lat, lon)
            except (ValueError, TypeError):
                continue

        # trip_id -> sorted list of (seq, stop_id)
        trip_stops: dict[str, list[tuple[int, str]]] = {}
        for row in stop_times_rows:
            tid = row.get("trip_id", "")
            try:
                seq = int(row.get("stop_sequence", "0"))
            except (ValueError, TypeError):
                continue
            stop_id = row.get("stop_id", "")
            trip_stops.setdefault(tid, []).append((seq, stop_id))

        # Pick one trip per route
        route_trips: dict[str, str] = {}
        for tid, rid in trip_to_route.items():
            if rid not in route_trips and tid in trip_stops:
                route_trips[rid] = tid

        for rid, info in route_info.items():
            tid = route_trips.get(rid, "")
            if not tid or tid not in trip_stops:
                continue

            stops = sorted(trip_stops[tid], key=lambda s: s[0])
            coords = []
            for _, stop_id in stops:
                if stop_id in stop_coords:
                    lat, lon = stop_coords[stop_id]
                    coords.append([lon, lat])

            if len(coords) < 2:
                continue

            rtype = info.get("route_type", "3")
            route_types_seen.add(rtype)
            rt = GTFSRouteType.from_string(rtype)

            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        **info,
                        "route_type_label": rt.label() if rt else f"Type {rtype}",
                        "shape_id": "",
                        "point_count": len(coords),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                }
            )

    _write_geojson(output_path, features)

    # Format route types string
    rt_labels = []
    for rt_str in sorted(route_types_seen):
        rt = GTFSRouteType.from_string(rt_str)
        rt_labels.append(rt.label() if rt else f"Type {rt_str}")

    return GTFSRouteFeatures(
        output_path=output_path,
        route_count=len(features),
        has_shapes=has_shapes,
        route_types=", ".join(rt_labels),
        extraction_date=_now_iso(),
    )


def compute_service_frequency(feed_path: str, output_path: str = "") -> FrequencyResult:
    """Compute trips-per-stop-per-day from stop_times.txt + calendar.txt.

    Streams stop_times.txt for memory efficiency on large feeds.

    Args:
        feed_path: Path to extracted GTFS feed directory
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        FrequencyResult with frequency metrics
    """
    if not output_path:
        output_path = os.path.join(feed_path, "frequency.geojson")

    # Read calendar to find active service IDs
    calendar_rows = _read_csv(feed_path, "calendar.txt")
    active_services: set[str] = set()
    service_date = ""

    if calendar_rows:
        # Use the first service as representative
        for row in calendar_rows:
            sid = row.get("service_id", "")
            # Check if any day is active
            days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if any(row.get(d, "0") == "1" for d in days):
                active_services.add(sid)
        if calendar_rows:
            service_date = calendar_rows[0].get("start_date", "")

    # Map trip_id -> service_id from trips.txt
    trips_rows = _read_csv(feed_path, "trips.txt")
    trip_service: dict[str, str] = {}
    for row in trips_rows:
        trip_service[row.get("trip_id", "")] = row.get("service_id", "")

    # Count trips per stop (streaming stop_times.txt)
    stop_trip_count: dict[str, int] = {}
    csv_path = os.path.join(feed_path, "stop_times.txt")
    try:
        with _storage.open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            seen_trips_per_stop: dict[str, set[str]] = {}
            for row in reader:
                trip_id = row.get("trip_id", "")
                stop_id = row.get("stop_id", "")

                # If we have calendar data, filter to active services
                if active_services:
                    svc = trip_service.get(trip_id, "")
                    if svc and svc not in active_services:
                        continue

                seen = seen_trips_per_stop.setdefault(stop_id, set())
                if trip_id not in seen:
                    seen.add(trip_id)
                    stop_trip_count[stop_id] = stop_trip_count.get(stop_id, 0) + 1
    except FileNotFoundError:
        log.warning("stop_times.txt not found in %s", feed_path)
    except Exception as e:
        log.warning("Error reading stop_times.txt: %s", e)

    # Load stop locations for GeoJSON output
    stops_rows = _read_csv(feed_path, "stops.txt")
    stop_locs: dict[str, tuple[str, float, float]] = {}
    for row in stops_rows:
        loc_type = row.get("location_type", "0").strip()
        if loc_type not in ("", "0"):
            continue
        try:
            lat = float(row.get("stop_lat", "0"))
            lon = float(row.get("stop_lon", "0"))
        except (ValueError, TypeError):
            continue
        stop_locs[row.get("stop_id", "")] = (
            row.get("stop_name", ""),
            lat,
            lon,
        )

    # Build frequency GeoJSON
    features: list[dict] = []
    total_trips = 0
    max_trips = 0

    for stop_id, count in stop_trip_count.items():
        if stop_id not in stop_locs:
            continue
        name, lat, lon = stop_locs[stop_id]
        total_trips += count
        max_trips = max(max_trips, count)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "stop_id": stop_id,
                    "stop_name": name,
                    "trips_per_day": count,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        )

    _write_geojson(output_path, features)

    avg_trips = total_trips / len(features) if features else 0.0

    return FrequencyResult(
        output_path=output_path,
        stop_count=len(features),
        avg_trips_per_day=round(avg_trips, 1),
        max_trips_per_day=max_trips,
        service_date=service_date,
        extraction_date=_now_iso(),
    )


def compute_transit_statistics(feed_path: str) -> TransitStats:
    """Compute aggregate transit statistics from a GTFS feed.

    Args:
        feed_path: Path to extracted GTFS feed directory

    Returns:
        TransitStats with counts by route type
    """
    agency_rows = _read_csv(feed_path, "agency.txt")
    stops_rows = _read_csv(feed_path, "stops.txt")
    routes_rows = _read_csv(feed_path, "routes.txt")
    trips_rows = _read_csv(feed_path, "trips.txt")

    agency_name = ""
    if agency_rows:
        agency_name = agency_rows[0].get("agency_name", "")

    # Count stops (location_type=0 only)
    stop_count = sum(1 for row in stops_rows if row.get("location_type", "0").strip() in ("", "0"))

    # Count routes by type
    type_counts: dict[str, int] = {}
    for row in routes_rows:
        rtype = row.get("route_type", "3")
        rt = GTFSRouteType.from_string(rtype)
        label = rt.label() if rt else f"Type {rtype}"
        type_counts[label] = type_counts.get(label, 0) + 1

    has_shapes = os.path.isfile(os.path.join(feed_path, "shapes.txt"))

    # Format route type counts
    type_str = ", ".join(f"{k}: {v}" for k, v in sorted(type_counts.items()))

    return TransitStats(
        agency_name=agency_name,
        stop_count=stop_count,
        route_count=len(routes_rows),
        trip_count=len(trips_rows),
        has_shapes=has_shapes,
        route_type_counts=type_str,
        extraction_date=_now_iso(),
    )


# ── OSM integration functions ───────────────────────────────────────────


def find_nearest_stops(
    osm_path: str,
    stops_path: str,
    max_dist: float = 2000.0,
    output_path: str = "",
) -> NearestStopResult:
    """Find nearest transit stop for each OSM feature.

    Uses brute-force haversine distance. Each OSM feature gets annotated
    with its nearest stop info.

    Args:
        osm_path: Path to OSM GeoJSON file
        stops_path: Path to stops GeoJSON file
        max_dist: Maximum distance in meters
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        NearestStopResult with match statistics
    """
    if not output_path:
        output_path = os.path.join(os.path.dirname(stops_path), "nearest_stops.geojson")

    osm_features = _load_geojson_features(osm_path)
    stop_features = _load_geojson_features(stops_path)

    # Pre-extract stop centroids
    stop_points: list[tuple[float, float, dict]] = []
    for sf in stop_features:
        c = _feature_centroid(sf)
        if c:
            stop_points.append((c[0], c[1], sf.get("properties", {})))

    features: list[dict] = []
    matched = 0
    distances: list[float] = []
    max_found = 0.0

    for feat in osm_features:
        fc = _feature_centroid(feat)
        if not fc:
            continue

        # Find nearest stop
        best_dist = float("inf")
        best_stop: dict = {}
        for slat, slon, sprops in stop_points:
            d = _haversine_m(fc[0], fc[1], slat, slon)
            if d < best_dist:
                best_dist = d
                best_stop = sprops

        props = dict(feat.get("properties", {}))
        if best_dist <= max_dist:
            matched += 1
            distances.append(best_dist)
            max_found = max(max_found, best_dist)
            props["nearest_stop_id"] = best_stop.get("stop_id", "")
            props["nearest_stop_name"] = best_stop.get("stop_name", "")
            props["nearest_stop_distance_m"] = round(best_dist, 1)
        else:
            props["nearest_stop_id"] = ""
            props["nearest_stop_name"] = ""
            props["nearest_stop_distance_m"] = -1

        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": feat.get("geometry"),
            }
        )

    _write_geojson(output_path, features)

    avg_dist = sum(distances) / len(distances) if distances else 0.0

    return NearestStopResult(
        output_path=output_path,
        feature_count=len(features),
        matched_count=matched,
        avg_distance_m=round(avg_dist, 1),
        max_distance_m=round(max_found, 1),
        extraction_date=_now_iso(),
    )


def compute_stop_accessibility(
    osm_path: str,
    stops_path: str,
    threshold: float = 400.0,
    output_path: str = "",
) -> AccessibilityResult:
    """Classify OSM features into walk-distance bands from transit stops.

    Bands: within 400 m, within 800 m, beyond 800 m.

    Args:
        osm_path: Path to OSM GeoJSON file
        stops_path: Path to stops GeoJSON file
        threshold: Primary walk threshold in meters (default 400)
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        AccessibilityResult with band counts and percentages
    """
    if not output_path:
        output_path = os.path.join(os.path.dirname(stops_path), "accessibility.geojson")

    osm_features = _load_geojson_features(osm_path)
    stop_features = _load_geojson_features(stops_path)

    # Pre-extract stop centroids
    stop_points: list[tuple[float, float]] = []
    for sf in stop_features:
        c = _feature_centroid(sf)
        if c:
            stop_points.append(c)

    features: list[dict] = []
    within_400 = 0
    within_800 = 0
    beyond_800 = 0
    secondary_threshold = threshold * 2  # 800m when threshold=400

    for feat in osm_features:
        fc = _feature_centroid(feat)
        if not fc:
            continue

        # Find minimum distance to any stop
        min_dist = float("inf")
        for slat, slon in stop_points:
            d = _haversine_m(fc[0], fc[1], slat, slon)
            if d < min_dist:
                min_dist = d
                if d <= threshold:
                    break  # Can't get closer than the primary band

        if min_dist <= threshold:
            band = "within_400m"
            within_400 += 1
        elif min_dist <= secondary_threshold:
            band = "within_800m"
            within_800 += 1
        else:
            band = "beyond_800m"
            beyond_800 += 1

        props = dict(feat.get("properties", {}))
        props["transit_band"] = band
        props["nearest_stop_m"] = round(min_dist, 1) if min_dist < float("inf") else -1

        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": feat.get("geometry"),
            }
        )

    _write_geojson(output_path, features)

    total = len(features)
    pct_400 = (within_400 / total * 100) if total else 0.0
    pct_800 = ((within_400 + within_800) / total * 100) if total else 0.0

    return AccessibilityResult(
        output_path=output_path,
        total_features=total,
        within_400m=within_400,
        within_800m=within_800,
        beyond_800m=beyond_800,
        pct_within_400m=round(pct_400, 1),
        pct_within_800m=round(pct_800, 1),
        extraction_date=_now_iso(),
    )


# ── Coverage analysis functions ─────────────────────────────────────────


def _compute_bbox(features: list[dict]) -> tuple[float, float, float, float]:
    """Compute bounding box from GeoJSON features.

    Returns (min_lat, min_lon, max_lat, max_lon).
    """
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")

    for feat in features:
        c = _feature_centroid(feat)
        if c:
            min_lat = min(min_lat, c[0])
            min_lon = min(min_lon, c[1])
            max_lat = max(max_lat, c[0])
            max_lon = max(max_lon, c[1])

    if min_lat == float("inf"):
        return (0, 0, 0, 0)
    return (min_lat, min_lon, max_lat, max_lon)


def _cell_size_deg(cell_size_m: float, mid_lat: float) -> tuple[float, float]:
    """Convert cell size in meters to approximate degrees.

    Returns (lat_deg, lon_deg).
    """
    lat_deg = cell_size_m / 111_132.0
    lon_deg = cell_size_m / (111_132.0 * math.cos(math.radians(mid_lat)))
    return (lat_deg, lon_deg)


def compute_coverage_gaps(
    stops_path: str,
    osm_path: str,
    cell_size: float = 500.0,
    output_path: str = "",
) -> CoverageResult:
    """Detect transit coverage gaps using a grid overlay.

    A gap cell is one that contains OSM features but no transit stops.

    Args:
        stops_path: Path to stops GeoJSON file
        osm_path: Path to OSM GeoJSON file
        cell_size: Grid cell size in meters
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        CoverageResult with gap statistics
    """
    if not output_path:
        output_path = os.path.join(os.path.dirname(stops_path), "coverage_gaps.geojson")

    stop_features = _load_geojson_features(stops_path)
    osm_features = _load_geojson_features(osm_path)

    # Compute combined bounding box
    all_features = stop_features + osm_features
    if not all_features:
        _write_geojson(output_path, [])
        return CoverageResult(
            output_path=output_path,
            total_cells=0,
            extraction_date=_now_iso(),
        )

    min_lat, min_lon, max_lat, max_lon = _compute_bbox(all_features)
    mid_lat = (min_lat + max_lat) / 2
    lat_step, lon_step = _cell_size_deg(cell_size, mid_lat)

    # Safety cap on grid size
    n_rows = max(1, int(math.ceil((max_lat - min_lat) / lat_step)))
    n_cols = max(1, int(math.ceil((max_lon - min_lon) / lon_step)))
    if n_rows * n_cols > MAX_GRID_CELLS:
        scale = math.sqrt((n_rows * n_cols) / MAX_GRID_CELLS)
        lat_step *= scale
        lon_step *= scale
        n_rows = max(1, int(math.ceil((max_lat - min_lat) / lat_step)))
        n_cols = max(1, int(math.ceil((max_lon - min_lon) / lon_step)))

    # Assign stops to grid cells
    stop_cells: set[tuple[int, int]] = set()
    for feat in stop_features:
        c = _feature_centroid(feat)
        if c:
            row = int((c[0] - min_lat) / lat_step)
            col = int((c[1] - min_lon) / lon_step)
            stop_cells.add((row, col))

    # Assign OSM features to grid cells
    osm_cells: set[tuple[int, int]] = set()
    for feat in osm_features:
        c = _feature_centroid(feat)
        if c:
            row = int((c[0] - min_lat) / lat_step)
            col = int((c[1] - min_lon) / lon_step)
            osm_cells.add((row, col))

    # Find gap cells (have OSM features but no stops)
    gap_cells = osm_cells - stop_cells
    covered_cells = osm_cells & stop_cells
    total_cells = len(osm_cells)

    # Build GeoJSON grid features for gap cells
    features: list[dict] = []
    for row, col in gap_cells:
        cell_min_lat = min_lat + row * lat_step
        cell_min_lon = min_lon + col * lon_step
        cell_max_lat = cell_min_lat + lat_step
        cell_max_lon = cell_min_lon + lon_step

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "grid_row": row,
                    "grid_col": col,
                    "type": "gap",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [cell_min_lon, cell_min_lat],
                            [cell_max_lon, cell_min_lat],
                            [cell_max_lon, cell_max_lat],
                            [cell_min_lon, cell_max_lat],
                            [cell_min_lon, cell_min_lat],
                        ]
                    ],
                },
            }
        )

    _write_geojson(output_path, features)

    coverage_pct = (len(covered_cells) / total_cells * 100) if total_cells else 0.0

    return CoverageResult(
        output_path=output_path,
        total_cells=total_cells,
        covered_cells=len(covered_cells),
        gap_cells=len(gap_cells),
        coverage_pct=round(coverage_pct, 1),
        extraction_date=_now_iso(),
    )


def compute_route_density(
    routes_path: str,
    cell_size: float = 500.0,
    output_path: str = "",
) -> DensityResult:
    """Compute route density per grid cell.

    Counts the number of route linestrings passing through each cell.

    Args:
        routes_path: Path to routes GeoJSON file
        cell_size: Grid cell size in meters
        output_path: Path for output GeoJSON (auto-generated if empty)

    Returns:
        DensityResult with density statistics
    """
    if not output_path:
        output_path = os.path.join(os.path.dirname(routes_path), "route_density.geojson")

    route_features = _load_geojson_features(routes_path)
    if not route_features:
        _write_geojson(output_path, [])
        return DensityResult(
            output_path=output_path,
            total_cells=0,
            extraction_date=_now_iso(),
        )

    min_lat, min_lon, max_lat, max_lon = _compute_bbox(route_features)
    mid_lat = (min_lat + max_lat) / 2
    lat_step, lon_step = _cell_size_deg(cell_size, mid_lat)

    # Safety cap
    n_rows = max(1, int(math.ceil((max_lat - min_lat) / lat_step)))
    n_cols = max(1, int(math.ceil((max_lon - min_lon) / lon_step)))
    if n_rows * n_cols > MAX_GRID_CELLS:
        scale = math.sqrt((n_rows * n_cols) / MAX_GRID_CELLS)
        lat_step *= scale
        lon_step *= scale
        n_rows = max(1, int(math.ceil((max_lat - min_lat) / lat_step)))
        n_cols = max(1, int(math.ceil((max_lon - min_lon) / lon_step)))

    # Count routes per cell — sample points along each route linestring
    cell_routes: dict[tuple[int, int], set[str]] = {}
    for feat in route_features:
        geom = feat.get("geometry", {})
        rid = feat.get("properties", {}).get("route_id", "")
        coords = geom.get("coordinates", [])
        if geom.get("type") != "LineString" or not coords:
            continue

        visited: set[tuple[int, int]] = set()
        for coord in coords:
            lon, lat = coord[0], coord[1]
            row = int((lat - min_lat) / lat_step)
            col = int((lon - min_lon) / lon_step)
            cell = (row, col)
            if cell not in visited:
                visited.add(cell)
                cell_routes.setdefault(cell, set()).add(rid)

    # Build GeoJSON
    features: list[dict] = []
    max_count = 0
    total_count = 0

    for (row, col), route_ids in cell_routes.items():
        count = len(route_ids)
        max_count = max(max_count, count)
        total_count += count

        cell_min_lat = min_lat + row * lat_step
        cell_min_lon = min_lon + col * lon_step
        cell_max_lat = cell_min_lat + lat_step
        cell_max_lon = cell_min_lon + lon_step

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "grid_row": row,
                    "grid_col": col,
                    "route_count": count,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [cell_min_lon, cell_min_lat],
                            [cell_max_lon, cell_min_lat],
                            [cell_max_lon, cell_max_lat],
                            [cell_min_lon, cell_max_lat],
                            [cell_min_lon, cell_min_lat],
                        ]
                    ],
                },
            }
        )

    _write_geojson(output_path, features)

    total_cells = len(cell_routes)
    avg_count = total_count / total_cells if total_cells else 0.0

    return DensityResult(
        output_path=output_path,
        total_cells=total_cells,
        max_routes_per_cell=max_count,
        avg_routes_per_cell=round(avg_count, 1),
        extraction_date=_now_iso(),
    )


# ── Report generation ───────────────────────────────────────────────────


def generate_transit_report(
    feed_path: str,
    osm_path: str = "",
    output_dir: str = "",
) -> dict:
    """Run all analyses and produce a consolidated transit report.

    Args:
        feed_path: Path to extracted GTFS feed directory
        osm_path: Path to OSM GeoJSON file (optional, enables integration analyses)
        output_dir: Directory for output files (defaults to feed_path)

    Returns:
        Dict matching the TransitReport schema
    """
    if not output_dir:
        output_dir = feed_path

    os.makedirs(output_dir, exist_ok=True)

    # Core analyses
    stats = compute_transit_statistics(feed_path)

    stops_result = extract_stops(
        feed_path,
        output_path=os.path.join(output_dir, "stops.geojson"),
    )

    routes_result = extract_routes(
        feed_path,
        output_path=os.path.join(output_dir, "routes.geojson"),
    )

    freq_result = compute_service_frequency(
        feed_path,
        output_path=os.path.join(output_dir, "frequency.geojson"),
    )

    # OSM integration analyses (only if osm_path provided)
    nearest_path = ""
    accessibility_path = ""
    coverage_path = ""
    density_path = ""
    has_osm = bool(osm_path)

    if has_osm and stops_result.stop_count > 0:
        try:
            nearest = find_nearest_stops(
                osm_path,
                stops_result.output_path,
                output_path=os.path.join(output_dir, "nearest_stops.geojson"),
            )
            nearest_path = nearest.output_path
        except Exception as e:
            log.warning("Nearest stops analysis failed: %s", e)

        try:
            access = compute_stop_accessibility(
                osm_path,
                stops_result.output_path,
                output_path=os.path.join(output_dir, "accessibility.geojson"),
            )
            accessibility_path = access.output_path
        except Exception as e:
            log.warning("Accessibility analysis failed: %s", e)

        try:
            coverage = compute_coverage_gaps(
                stops_result.output_path,
                osm_path,
                output_path=os.path.join(output_dir, "coverage_gaps.geojson"),
            )
            coverage_path = coverage.output_path
        except Exception as e:
            log.warning("Coverage gap analysis failed: %s", e)

    if routes_result.route_count > 0:
        try:
            density = compute_route_density(
                routes_result.output_path,
                output_path=os.path.join(output_dir, "route_density.geojson"),
            )
            density_path = density.output_path
        except Exception as e:
            log.warning("Route density analysis failed: %s", e)

    return {
        "feed_agency": stats.agency_name,
        "stop_count": stats.stop_count,
        "route_count": stats.route_count,
        "trip_count": stats.trip_count,
        "has_shapes": stats.has_shapes,
        "stops_path": stops_result.output_path,
        "routes_path": routes_result.output_path,
        "frequency_path": freq_result.output_path,
        "osm_integration": has_osm,
        "nearest_stops_path": nearest_path,
        "accessibility_path": accessibility_path,
        "coverage_path": coverage_path,
        "density_path": density_path,
        "extraction_date": _now_iso(),
    }
