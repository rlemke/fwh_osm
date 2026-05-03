"""Structural Betweenness Sampling (SBS) for zoom-level road importance.

Builds anchor sets from cities, samples OD pairs, routes via GraphHopper HTTP API
with parallel requests, and accumulates betweenness votes on logical edges.
"""

import json
import logging
import math
import os
import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from .zoom_graph import RoadGraph, _haversine_m

GRAPHHOPPER_API_URL = os.environ.get("GRAPHHOPPER_API_URL", "http://localhost:8989")

# Population thresholds per zoom level for anchor selection (spec §5.1)
ANCHOR_POP_THRESHOLDS: dict[int, int] = {
    2: 500_000,
    3: 200_000,
    4: 80_000,
    5: 30_000,
    6: 10_000,
    7: 5_000,
}

# Target anchor counts per zoom (approximate upper bounds)
ANCHOR_TARGETS: dict[int, int] = {
    2: 50,
    3: 200,
    4: 1_000,
    5: 5_000,
    6: 20_000,
    7: 20_000,
}

# Default OD pair counts per zoom (spec §5.2)
DEFAULT_K_PAIRS: dict[int, int] = {
    2: 5_000,
    3: 20_000,
    4: 50_000,
    5: 150_000,
    6: 400_000,
    7: 400_000,
}

# Minimum straight-line distance for OD pairs in km (spec §5.2)
MIN_PAIR_DISTANCE_KM: dict[int, float] = {
    2: 300.0,
    3: 150.0,
    4: 60.0,
    5: 20.0,
    6: 5.0,
    7: 5.0,
}


class SegmentIndex:
    """Grid-based spatial index for snapping route coordinates to logical edges.

    Uses ~500m grid cells for efficient nearest-edge lookup.
    """

    CELL_SIZE = 0.005  # ~500m in degrees

    def __init__(self, graph: RoadGraph) -> None:
        self._graph = graph
        self._grid: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        # grid cell → list of (edge_id, segment_index)
        self._edge_segments: dict[int, list[tuple[float, float, float, float]]] = {}
        self._build_index()

    def _cell(self, lon: float, lat: float) -> tuple[int, int]:
        return int(lon / self.CELL_SIZE), int(lat / self.CELL_SIZE)

    def _build_index(self) -> None:
        for edge in self._graph.edges:
            segs = []
            for i in range(len(edge.coords) - 1):
                lon1, lat1 = edge.coords[i]
                lon2, lat2 = edge.coords[i + 1]
                segs.append((lon1, lat1, lon2, lat2))

                # Insert segment into all grid cells it touches
                min_cx = int(min(lon1, lon2) / self.CELL_SIZE) - 1
                max_cx = int(max(lon1, lon2) / self.CELL_SIZE) + 1
                min_cy = int(min(lat1, lat2) / self.CELL_SIZE) - 1
                max_cy = int(max(lat1, lat2) / self.CELL_SIZE) + 1

                for cx in range(min_cx, max_cx + 1):
                    for cy in range(min_cy, max_cy + 1):
                        self._grid[(cx, cy)].append((edge.edge_id, i))

            self._edge_segments[edge.edge_id] = segs

    def _point_to_segment_dist_m(
        self, px: float, py: float, x1: float, y1: float, x2: float, y2: float
    ) -> float:
        """Perpendicular distance from point to line segment in meters.

        Uses flat-earth approximation with cos(lat) scaling.
        """
        cos_lat = math.cos(math.radians(py))
        # Convert to approximate meters
        dx = (x2 - x1) * cos_lat
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return _haversine_m(px, py, x1, y1)

        t = ((px - x1) * cos_lat * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))

        proj_lon = x1 + t * (x2 - x1)
        proj_lat = y1 + t * (y2 - y1)
        return _haversine_m(px, py, proj_lon, proj_lat)

    def snap_route(self, route_coords: list[list[float]], tolerance_m: float = 50.0) -> set[int]:
        """Snap route coordinates to logical edge IDs within tolerance."""
        matched_edges: set[int] = set()

        for coord in route_coords:
            lon, lat = coord[0], coord[1]
            cx, cy = self._cell(lon, lat)

            best_dist = tolerance_m
            best_eid = -1

            # Check surrounding cells
            for dcx in range(-1, 2):
                for dcy in range(-1, 2):
                    for eid, seg_idx in self._grid.get((cx + dcx, cy + dcy), []):
                        segs = self._edge_segments.get(eid)
                        if segs is None or seg_idx >= len(segs):
                            continue
                        x1, y1, x2, y2 = segs[seg_idx]
                        d = self._point_to_segment_dist_m(lon, lat, x1, y1, x2, y2)
                        if d < best_dist:
                            best_dist = d
                            best_eid = eid

            if best_eid >= 0:
                matched_edges.add(best_eid)

        return matched_edges


def build_anchors(graph: RoadGraph, cities_path: str, zoom_level: int) -> list[int]:
    """Build anchor node set for a zoom level from city data.

    Args:
        graph: The logical edge graph.
        cities_path: Path to cities GeoJSON file.
        zoom_level: Zoom level (2–7).

    Returns:
        List of anchor node IDs.
    """
    pop_threshold = ANCHOR_POP_THRESHOLDS.get(zoom_level, 10_000)
    target_count = ANCHOR_TARGETS.get(zoom_level, 1_000)

    # Load cities
    cities: list[tuple[float, float, int]] = []  # (lon, lat, population)
    try:
        with open(cities_path, encoding="utf-8") as f:
            geojson = json.load(f)
        for feat in geojson.get("features", []):
            props = feat.get("properties", {})
            pop = props.get("population", 0)
            if not isinstance(pop, (int, float)):
                try:
                    pop = int(pop)
                except (ValueError, TypeError):
                    pop = 0
            if pop < pop_threshold:
                continue
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [])
            if len(coords) >= 2:
                cities.append((coords[0], coords[1], int(pop)))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load cities from %s: %s", cities_path, e)

    # Sort by population descending, limit to target
    cities.sort(key=lambda c: c[2], reverse=True)
    cities = cities[:target_count]

    # Snap each city to nearest routable node
    anchors: list[int] = []
    for lon, lat, _pop in cities:
        best_node = _snap_to_nearest_node(graph, lon, lat)
        if best_node is not None and best_node not in anchors:
            anchors.append(best_node)

    # Fallback: if too few anchors, add high-degree nodes
    if len(anchors) < max(10, target_count // 10):
        log.info(
            "Sparse anchor set (%d) for z%d, adding high-degree nodes", len(anchors), zoom_level
        )
        degree_nodes = sorted(
            graph.adj.keys(),
            key=lambda n: len(graph.adj[n]),
            reverse=True,
        )
        for nid in degree_nodes:
            if len(graph.adj[nid]) >= 5 and nid not in anchors:
                anchors.append(nid)
            if len(anchors) >= target_count:
                break

    log.info(
        "Built %d anchors for zoom %d (pop threshold %d)", len(anchors), zoom_level, pop_threshold
    )
    return anchors


def _snap_to_nearest_node(
    graph: RoadGraph, lon: float, lat: float, max_dist_m: float = 50_000.0
) -> int | None:
    """Find the nearest graph node to a (lon, lat) point."""
    best_dist = max_dist_m
    best_node = None
    for nid, (nlon, nlat) in graph.node_coords.items():
        d = _haversine_m(lon, lat, nlon, nlat)
        if d < best_dist:
            best_dist = d
            best_node = nid
    return best_node


def sample_od_pairs(
    anchors: list[int], zoom_level: int, graph: RoadGraph, k_pairs: int | None = None
) -> list[tuple[int, int]]:
    """Sample origin-destination pairs from anchors for SBS.

    Args:
        anchors: List of anchor node IDs.
        zoom_level: Zoom level (2–7).
        graph: The road graph (for distance filtering).
        k_pairs: Number of pairs to sample (default per zoom level).

    Returns:
        Sorted list of (origin, destination) node ID tuples.
    """
    if k_pairs is None:
        k_pairs = DEFAULT_K_PAIRS.get(zoom_level, 5_000)

    min_dist_km = MIN_PAIR_DISTANCE_KM.get(zoom_level, 5.0)
    min_dist_m = min_dist_km * 1_000

    rng = random.Random(42)

    # Generate all valid pairs
    valid_pairs: list[tuple[int, int]] = []
    for i, a in enumerate(anchors):
        for b in anchors[i + 1 :]:
            if a in graph.node_coords and b in graph.node_coords:
                alon, alat = graph.node_coords[a]
                blon, blat = graph.node_coords[b]
                d = _haversine_m(alon, alat, blon, blat)
                if d >= min_dist_m:
                    valid_pairs.append((a, b))

    # Sample
    if len(valid_pairs) > k_pairs:
        rng.shuffle(valid_pairs)
        valid_pairs = valid_pairs[:k_pairs]

    valid_pairs.sort()
    log.info(
        "Sampled %d OD pairs for zoom %d from %d anchors (min %.0f km)",
        len(valid_pairs),
        zoom_level,
        len(anchors),
        min_dist_km,
    )
    return valid_pairs


def _route_pair(
    from_node: int,
    to_node: int,
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
) -> list[list[float]] | None:
    """Query GraphHopper HTTP API for fastest route between two nodes."""
    if not HAS_REQUESTS:
        return None

    from_coord = node_coords.get(from_node)
    to_coord = node_coords.get(to_node)
    if not from_coord or not to_coord:
        return None

    from_lon, from_lat = from_coord
    to_lon, to_lat = to_coord

    try:
        resp = requests.get(
            f"{GRAPHHOPPER_API_URL}/route",
            params={
                "point": [f"{from_lat},{from_lon}", f"{to_lat},{to_lon}"],
                "profile": profile,
                "type": "json",
                "points_encoded": "false",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            paths = data.get("paths", [])
            if paths:
                points = paths[0].get("points", {})
                return points.get("coordinates", [])
    except Exception as e:
        log.debug("Route failed %d→%d: %s", from_node, to_node, e)

    return None


def _route_pair_with_time(
    from_node: int,
    to_node: int,
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
) -> tuple[list[list[float]] | None, float]:
    """Route and return (coordinates, time_ms)."""
    if not HAS_REQUESTS:
        return None, 0.0

    from_coord = node_coords.get(from_node)
    to_coord = node_coords.get(to_node)
    if not from_coord or not to_coord:
        return None, 0.0

    from_lon, from_lat = from_coord
    to_lon, to_lat = to_coord

    try:
        resp = requests.get(
            f"{GRAPHHOPPER_API_URL}/route",
            params={
                "point": [f"{from_lat},{from_lon}", f"{to_lat},{to_lon}"],
                "profile": profile,
                "type": "json",
                "points_encoded": "false",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            paths = data.get("paths", [])
            if paths:
                points = paths[0].get("points", {})
                time_ms = paths[0].get("time", 0)
                return points.get("coordinates", []), time_ms
    except Exception as e:
        log.debug("Route failed %d→%d: %s", from_node, to_node, e)

    return None, 0.0


def route_batch_parallel(
    pairs: list[tuple[int, int]],
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
    max_concurrent: int = 16,
) -> dict[tuple[int, int], list[list[float]]]:
    """Route all OD pairs in parallel using ThreadPoolExecutor."""
    if not HAS_REQUESTS:
        log.warning("requests not available, skipping routing")
        return {}

    results: dict[tuple[int, int], list[list[float]]] = {}

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = {
            pool.submit(_route_pair, a, b, node_coords, graph_dir, profile): (a, b)
            for a, b in pairs
        }
        done = 0
        for future in as_completed(futures):
            pair = futures[future]
            done += 1
            if done % 1000 == 0:
                log.info("Routed %d / %d pairs", done, len(pairs))
            try:
                coords = future.result()
                if coords:
                    results[pair] = coords
            except Exception:
                pass

    log.info("Completed routing: %d / %d pairs succeeded", len(results), len(pairs))
    return results


def accumulate_votes(
    routes: dict[tuple[int, int], list[list[float]]],
    segment_index: SegmentIndex,
) -> dict[int, int]:
    """Accumulate betweenness centrality votes from routed paths.

    For each route, snap to logical edges and increment vote count.
    """
    bc: dict[int, int] = defaultdict(int)

    for (_a, _b), coords in routes.items():
        matched = segment_index.snap_route(coords)
        for eid in matched:
            bc[eid] += 1

    log.info("Accumulated votes on %d edges from %d routes", len(bc), len(routes))
    return dict(bc)


def normalize_sbs(bc: dict[int, int]) -> dict[int, float]:
    """Normalize betweenness counts to SB_z scores (spec §5.4).

    SB_z(e) = log(1 + BC_z[e]) / log(1 + P95_z)
    """
    if not bc:
        return {}

    values = sorted(bc.values())
    p95_idx = int(len(values) * 0.95)
    p95 = values[min(p95_idx, len(values) - 1)]

    if p95 == 0:
        return dict.fromkeys(bc, 0.0)

    denom = math.log(1 + p95)
    result: dict[int, float] = {}
    for eid, count in bc.items():
        result[eid] = min(1.0, math.log(1 + count) / denom)

    return result


def compute_sbs_for_zoom(
    graph: RoadGraph,
    cities_path: str,
    graph_dir: str,
    profile: str,
    zoom_level: int,
    k_pairs: int | None = None,
    max_concurrent: int = 16,
) -> tuple[dict[int, float], int, int]:
    """Full SBS pipeline for one zoom level.

    Returns:
        (sbs_scores, anchor_count, route_count)
    """
    anchors = build_anchors(graph, cities_path, zoom_level)
    pairs = sample_od_pairs(anchors, zoom_level, graph, k_pairs=k_pairs)

    segment_index = SegmentIndex(graph)
    routes = route_batch_parallel(
        pairs,
        graph.node_coords,
        graph_dir,
        profile,
        max_concurrent,
    )

    bc = accumulate_votes(routes, segment_index)
    sbs = normalize_sbs(bc)

    return sbs, len(anchors), len(routes)


def save_sbs(sbs: dict[int, float], path: str) -> None:
    """Save SBS scores to JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in sbs.items()}, f)


def load_sbs(path: str) -> dict[int, float]:
    """Load SBS scores from JSON file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {int(k): v for k, v in data.items()}


def save_anchors(anchors: list[int], path: str) -> None:
    """Save anchor node list to JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(anchors, f)


def load_anchors(path: str) -> list[int]:
    """Load anchor node list from JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)
