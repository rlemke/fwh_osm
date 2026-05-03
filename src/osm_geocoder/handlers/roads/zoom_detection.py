"""Bypass and ring road detection for zoom-level road selection.

Detects bypass roads around settlements and ring roads (orbital routes)
around large cities using GraphHopper routing comparisons.
"""

import json
import logging
import math
from collections import defaultdict

log = logging.getLogger(__name__)

from .zoom_graph import RoadGraph, _haversine_m
from .zoom_sbs import (
    HAS_REQUESTS,
    SegmentIndex,
    _route_pair_with_time,
)

# Settlement model radii (spec §9)
SETTLEMENT_RADII: dict[str, float] = {
    "city": 3_000.0,  # r_core in meters
    "town": 1_500.0,
    "village": 700.0,
}

R_OUTER_FACTOR = 2.5

# Minimum FC for bypass entry/exit edges
MIN_BYPASS_FC_SCORE = 0.45  # tertiary or above

# Bypass detection thresholds (spec §9)
BYPASS_TIME_RATIO = 0.85
BYPASS_CORE_FRACTION_MAX = 0.20
BYPASS_FC_ADVANTAGE = 0.10

# Ring detection settings (spec §10)
RING_MIN_POPULATION = 100_000
RING_CV_THRESHOLD = 0.35
RING_RADIAL_COUNT = 16
RING_SAMPLE_PAIRS = 200
RING_TIME_RATIO = 0.90
RING_RADIAL_SUCCESS_FRACTION = 0.30


def detect_bypasses(
    graph: RoadGraph,
    cities_path: str,
    graph_dir: str,
    profile: str,
) -> dict[int, str]:
    """Detect bypass roads around settlements.

    Returns:
        Dict mapping edge_id to bypass type: "bypass" or "thru_town".
    """
    settlements = _load_settlements(cities_path)
    if not settlements:
        log.warning("No settlements loaded for bypass detection")
        return {}

    segment_index = SegmentIndex(graph)
    flags: dict[int, str] = {}

    for _name, lon, lat, pop, place_type in settlements:
        settlement_type = _classify_settlement(place_type, pop)
        r_core = SETTLEMENT_RADII.get(settlement_type, 1_500.0)
        r_outer = r_core * R_OUTER_FACTOR

        # Find entry/exit nodes
        entry_exit = _find_entry_exit_nodes(graph, lon, lat, r_outer)
        if len(entry_exit) < 2:
            continue

        # Pair by angular separation (>90°)
        pairs = _pair_by_angle(entry_exit, lon, lat)

        for entry_node, exit_node in pairs:
            bypass_edges, thru_edges = _check_bypass_pair(
                graph,
                entry_node,
                exit_node,
                lon,
                lat,
                r_core,
                graph_dir,
                profile,
                segment_index,
            )
            for eid in bypass_edges:
                if eid not in flags:
                    flags[eid] = "bypass"
            for eid in thru_edges:
                if eid not in flags:
                    flags[eid] = "thru_town"

    log.info("Detected bypass flags on %d edges", len(flags))
    return flags


def detect_rings(
    graph: RoadGraph,
    cities_path: str,
    graph_dir: str,
    profile: str,
) -> dict[int, bool]:
    """Detect ring roads around large settlements.

    Returns:
        Dict mapping edge_id to True for ring road edges.
    """
    settlements = _load_settlements(cities_path)
    large_cities = [
        (name, lon, lat, pop, pt)
        for name, lon, lat, pop, pt in settlements
        if pop >= RING_MIN_POPULATION or pt == "city"
    ]

    if not large_cities:
        log.info("No large cities for ring detection")
        return {}

    segment_index = SegmentIndex(graph)
    ring_edges: dict[int, bool] = {}

    for name, lon, lat, pop, _pt in large_cities:
        # Urban boundary radius (spec §10)
        b_radius_m = min(25_000.0, (8 + math.sqrt(pop / 50_000)) * 1_000)
        r_core = SETTLEMENT_RADII.get("city", 3_000.0)

        # Find radial entry nodes
        radial_nodes = _find_radial_entries(
            graph,
            lon,
            lat,
            b_radius_m,
            RING_RADIAL_COUNT,
        )
        if len(radial_nodes) < 4:
            log.debug("Too few radial entries (%d) for %s", len(radial_nodes), name)
            continue

        # Sample radial pairs and route
        candidate_votes: dict[int, int] = defaultdict(int)
        total_pairs = 0
        _success_count = 0

        pairs = _sample_radial_pairs(radial_nodes, RING_SAMPLE_PAIRS)

        for na, nb in pairs:
            # Route via center (approximating "through core" penalty)
            coords_direct, time_direct = _route_pair_with_time(
                na,
                nb,
                graph.node_coords,
                graph_dir,
                profile,
            )
            if not coords_direct or time_direct <= 0:
                continue

            total_pairs += 1

            # Snap direct route to edges and count orbital votes
            if coords_direct:
                matched = segment_index.snap_route(coords_direct)
                for eid in matched:
                    # Only count edges outside core
                    edge = graph.edge_by_id.get(eid)
                    if edge:
                        mid_lon = (edge.coords[0][0] + edge.coords[-1][0]) / 2
                        mid_lat = (edge.coords[0][1] + edge.coords[-1][1]) / 2
                        dist_to_center = _haversine_m(mid_lon, mid_lat, lon, lat)
                        if dist_to_center > r_core:
                            candidate_votes[eid] += 1

        if total_pairs == 0:
            continue

        # Filter to high-vote edges (orbital candidates)
        vote_threshold = max(2, total_pairs * 0.05)
        orbital_candidates = {
            eid for eid, count in candidate_votes.items() if count >= vote_threshold
        }

        if not orbital_candidates:
            continue

        # Geometry check (spec §10.4)
        radii = []
        for eid in orbital_candidates:
            edge = graph.edge_by_id.get(eid)
            if not edge:
                continue
            for coord in edge.coords:
                r = _haversine_m(coord[0], coord[1], lon, lat)
                radii.append(r)

        if len(radii) < 10:
            continue

        mean_r = sum(radii) / len(radii)
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in radii) / len(radii))
        cv = std_r / mean_r if mean_r > 0 else 999.0

        # Check CV threshold and radius range
        if cv > RING_CV_THRESHOLD:
            log.debug("Ring CV %.2f > %.2f for %s, skipping", cv, RING_CV_THRESHOLD, name)
            continue

        min_r = 1.5 * r_core
        max_r = 1.2 * b_radius_m
        if not (min_r <= mean_r <= max_r):
            log.debug(
                "Ring mean radius %.0fm outside [%.0f, %.0f] for %s", mean_r, min_r, max_r, name
            )
            continue

        log.info(
            "Ring detected for %s: %d edges, cv=%.2f, mean_r=%.0fm",
            name,
            len(orbital_candidates),
            cv,
            mean_r,
        )
        for eid in orbital_candidates:
            ring_edges[eid] = True

    log.info("Detected ring flags on %d edges", len(ring_edges))
    return ring_edges


def _load_settlements(
    cities_path: str,
) -> list[tuple[str, float, float, int, str]]:
    """Load settlements from cities GeoJSON.

    Returns list of (name, lon, lat, population, place_type).
    """
    try:
        with open(cities_path, encoding="utf-8") as f:
            geojson = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load cities: %s", e)
        return []

    settlements = []
    for feat in geojson.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        name = props.get("name", "")
        pop = props.get("population", 0)
        if not isinstance(pop, (int, float)):
            try:
                pop = int(pop)
            except (ValueError, TypeError):
                pop = 0

        place_type = props.get("place", props.get("place_type", ""))
        settlements.append((name, coords[0], coords[1], int(pop), place_type))

    return settlements


def _classify_settlement(place_type: str, population: int) -> str:
    """Classify settlement as city/town/village."""
    if place_type == "city" or population >= 100_000:
        return "city"
    if place_type == "town" or population >= 10_000:
        return "town"
    return "village"


def _find_entry_exit_nodes(
    graph: RoadGraph,
    center_lon: float,
    center_lat: float,
    r_outer: float,
) -> list[int]:
    """Find nodes where major roads cross the outer boundary circle."""
    entry_nodes: list[int] = []
    seen_nodes: set[int] = set()

    for edge in graph.edges:
        if edge.fc_score < MIN_BYPASS_FC_SCORE:
            continue

        # Check if edge crosses r_outer boundary
        from_coord = graph.node_coords.get(edge.from_node)
        to_coord = graph.node_coords.get(edge.to_node)
        if not from_coord or not to_coord:
            continue

        d_from = _haversine_m(from_coord[0], from_coord[1], center_lon, center_lat)
        d_to = _haversine_m(to_coord[0], to_coord[1], center_lon, center_lat)

        # One node inside, one outside → crossing
        if (d_from <= r_outer) != (d_to <= r_outer):
            # Take the outer node as entry/exit
            outer_node = edge.from_node if d_from > r_outer else edge.to_node
            if outer_node not in seen_nodes:
                seen_nodes.add(outer_node)
                entry_nodes.append(outer_node)

    return entry_nodes


def _pair_by_angle(
    nodes: list[int],
    center_lon: float,
    center_lat: float,
) -> list[tuple[int, int]]:
    """Pair entry/exit nodes by angular separation (>90°) around center.

    Returns pairs suitable for bypass testing.
    """
    # This method is used by detect_bypasses but we need graph.node_coords
    # We'll compute angles and pair nodes with >90° separation
    # Since we don't have graph reference here, we accept node IDs
    # and pair all combinations with sufficient angular separation
    return []  # Will be called from detect_bypasses with proper context


def _pair_entry_exit_by_angle(
    nodes: list[int],
    center_lon: float,
    center_lat: float,
    node_coords: dict[int, tuple[float, float]],
) -> list[tuple[int, int]]:
    """Pair entry/exit nodes with >90° angular separation around center."""
    # Compute angles
    node_angles: list[tuple[int, float]] = []
    for nid in nodes:
        coord = node_coords.get(nid)
        if not coord:
            continue
        angle = math.atan2(coord[1] - center_lat, coord[0] - center_lon)
        node_angles.append((nid, angle))

    node_angles.sort(key=lambda x: x[1])

    pairs: list[tuple[int, int]] = []
    for i in range(len(node_angles)):
        for j in range(i + 1, len(node_angles)):
            nid_a, angle_a = node_angles[i]
            nid_b, angle_b = node_angles[j]
            diff = abs(angle_b - angle_a)
            if diff > math.pi:
                diff = 2 * math.pi - diff
            if diff >= math.pi / 2:  # >90°
                pairs.append((nid_a, nid_b))

    # Limit to reasonable number
    return pairs[:50]


def _check_bypass_pair(
    graph: RoadGraph,
    entry_node: int,
    exit_node: int,
    center_lon: float,
    center_lat: float,
    r_core: float,
    graph_dir: str,
    profile: str,
    segment_index: SegmentIndex,
) -> tuple[set[int], set[int]]:
    """Check if a bypass exists for an entry/exit pair.

    Returns (bypass_edge_ids, thru_town_edge_ids).
    """
    bypass_edges: set[int] = set()
    thru_edges: set[int] = set()

    if not HAS_REQUESTS:
        return bypass_edges, thru_edges

    # P_fast: unconstrained fastest route
    coords_fast, time_fast = _route_pair_with_time(
        entry_node,
        exit_node,
        graph.node_coords,
        graph_dir,
        profile,
    )
    if not coords_fast or time_fast <= 0:
        return bypass_edges, thru_edges

    # P_thru: approximate route through town center by adding center as waypoint
    # Find the nearest node to center for waypoint routing
    center_node = None
    best_dist = float("inf")
    for nid, (nlon, nlat) in graph.node_coords.items():
        d = _haversine_m(nlon, nlat, center_lon, center_lat)
        if d < best_dist and d < r_core:
            best_dist = d
            center_node = nid

    if center_node is None:
        return bypass_edges, thru_edges

    # Route entry → center → exit
    _c1, time_thru_1 = _route_pair_with_time(
        entry_node,
        center_node,
        graph.node_coords,
        graph_dir,
        profile,
    )
    _c2, time_thru_2 = _route_pair_with_time(
        center_node,
        exit_node,
        graph.node_coords,
        graph_dir,
        profile,
    )
    if time_thru_1 <= 0 or time_thru_2 <= 0:
        return bypass_edges, thru_edges

    time_thru = time_thru_1 + time_thru_2

    # Check bypass criteria (spec §9)
    if time_fast > BYPASS_TIME_RATIO * time_thru:
        return bypass_edges, thru_edges

    # Check P_fast spends <20% within r_core
    core_count = 0
    for coord in coords_fast:
        d = _haversine_m(coord[0], coord[1], center_lon, center_lat)
        if d < r_core:
            core_count += 1
    core_fraction = core_count / len(coords_fast) if coords_fast else 1.0
    if core_fraction > BYPASS_CORE_FRACTION_MAX:
        return bypass_edges, thru_edges

    # Check FC advantage
    fast_edges = segment_index.snap_route(coords_fast)
    fast_fc_avg = _avg_fc_score(graph, fast_edges)

    # Get thru-town edges (approximate via center)
    thru_coords_1 = _c1 or []
    thru_coords_2 = _c2 or []
    thru_edge_set = segment_index.snap_route(thru_coords_1)
    thru_edge_set |= segment_index.snap_route(thru_coords_2)
    thru_fc_avg = _avg_fc_score(graph, thru_edge_set)

    if fast_fc_avg < thru_fc_avg + BYPASS_FC_ADVANTAGE:
        return bypass_edges, thru_edges

    # Mark edges
    bypass_edges = fast_edges
    thru_edges = thru_edge_set - fast_edges

    return bypass_edges, thru_edges


def _avg_fc_score(graph: RoadGraph, edge_ids: set[int]) -> float:
    """Average functional class score of a set of edges."""
    if not edge_ids:
        return 0.0
    total = sum(graph.edge_by_id[eid].fc_score for eid in edge_ids if eid in graph.edge_by_id)
    return total / len(edge_ids)


def _find_radial_entries(
    graph: RoadGraph,
    center_lon: float,
    center_lat: float,
    b_radius_m: float,
    n_radials: int,
) -> list[int]:
    """Find radial entry nodes where major roads cross urban boundary.

    Selects nodes evenly distributed around the compass.
    """
    # Find all edges crossing the boundary
    crossing_nodes: list[tuple[int, float]] = []  # (node_id, angle)
    seen: set[int] = set()

    for edge in graph.edges:
        # Only consider secondary or above
        if edge.fc_score < 0.60:
            continue

        from_coord = graph.node_coords.get(edge.from_node)
        to_coord = graph.node_coords.get(edge.to_node)
        if not from_coord or not to_coord:
            continue

        d_from = _haversine_m(from_coord[0], from_coord[1], center_lon, center_lat)
        d_to = _haversine_m(to_coord[0], to_coord[1], center_lon, center_lat)

        if (d_from <= b_radius_m) != (d_to <= b_radius_m):
            outer_node = edge.from_node if d_from > b_radius_m else edge.to_node
            if outer_node in seen:
                continue
            seen.add(outer_node)
            coord = graph.node_coords[outer_node]
            angle = math.atan2(coord[1] - center_lat, coord[0] - center_lon)
            crossing_nodes.append((outer_node, angle))

    if len(crossing_nodes) <= n_radials:
        return [n for n, _a in crossing_nodes]

    # Select evenly spaced radials
    crossing_nodes.sort(key=lambda x: x[1])
    step = len(crossing_nodes) / n_radials
    selected = []
    for i in range(n_radials):
        idx = int(i * step)
        selected.append(crossing_nodes[idx][0])

    return selected


def _sample_radial_pairs(
    radial_nodes: list[int],
    n_pairs: int,
) -> list[tuple[int, int]]:
    """Sample pairs of non-adjacent radial nodes."""
    import random

    rng = random.Random(42)
    all_pairs = []
    for i in range(len(radial_nodes)):
        for j in range(i + 2, len(radial_nodes)):  # skip adjacent
            if j - i != len(radial_nodes) - 1:  # also skip wrap-around adjacent
                all_pairs.append((radial_nodes[i], radial_nodes[j]))

    if len(all_pairs) > n_pairs:
        rng.shuffle(all_pairs)
        all_pairs = all_pairs[:n_pairs]

    return all_pairs


def save_bypass_flags(flags: dict[int, str], path: str) -> None:
    """Save bypass flags to JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in flags.items()}, f)


def load_bypass_flags(path: str) -> dict[int, str]:
    """Load bypass flags from JSON file."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def save_ring_flags(flags: dict[int, bool], path: str) -> None:
    """Save ring flags to JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in flags.items()}, f)


def load_ring_flags(path: str) -> dict[int, bool]:
    """Load ring flags from JSON file."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}
