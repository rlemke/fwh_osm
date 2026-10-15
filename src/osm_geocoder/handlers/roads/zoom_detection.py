"""Bypass and ring road detection for zoom-level road selection.

Detects bypass roads around settlements and ring roads (orbital routes)
around large cities using GraphHopper routing comparisons.
"""

import json
import logging
import math
from collections import defaultdict
from collections.abc import Callable

log = logging.getLogger(__name__)

from ..shared._output import ensure_dir, open_output, read_storage_json
from .zoom_graph import RoadGraph, _haversine_m
from .zoom_sbs import (
    is_settlement,
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

# How often the settlement loops report liveness (see detect_bypasses).
DETECT_HEARTBEAT_EVERY = 25

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
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    segment_index: SegmentIndex | None = None,
) -> dict[int, str]:
    """Detect bypass roads around settlements.

    ⚠️ Heartbeat-aware because this phase is SILENT and long: it issues three
    routing calls per entry/exit pair, and on a state-sized region that is
    thousands of settlements. Measured 2026-09-23 on Washington, immediately
    after the pairing stub was fixed so the step began doing real work: the
    task emitted nothing between "step 4" and the end, and the 30-minute
    stuck-task watchdog was on course to reclaim a run whose handler was at
    96% CPU. Step logs are not progress; only the heartbeat is.

    Two halves, so the routing half can also run as separate tasks
    (``zoom_builder.plan_zoom_routing``): ``plan_bypasses`` (geometry: the
    entry/exit pairs of each settlement, needs the whole graph) and
    ``evaluate_bypasses`` (three routes per pair, needs only node coordinates,
    the snap index and road classes).

    Returns:
        Dict mapping edge_id to bypass type: "bypass" or "thru_town".
    """
    settlements = _load_settlements(cities_path)
    if not settlements:
        log.warning("No settlements loaded for bypass detection")
        return {}

    # Reuse the caller's index when given: at country scale each SegmentIndex
    # is several GiB, and building fresh ones here while step 3's is still
    # alive is what pushed the US build past the VM's memory.
    if segment_index is None:
        segment_index = SegmentIndex(graph)
    items, stats = plan_bypasses(graph, settlements, heartbeat, check_cancel)
    flags = evaluate_bypasses(items, graph.node_coords, graph_dir, profile, segment_index,
                              _graph_fc(graph), stats, heartbeat, check_cancel)
    log_bypass_result(flags, len(settlements), stats)
    return flags


def plan_bypasses(
    graph: RoadGraph,
    settlements: list,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """The geometry half: for each settlement that has any, its entry/exit
    pairs to route, in settlement order. Returns (items, rejection tallies)."""
    boundary_index = _BoundaryEdgeIndex(graph)
    node_index = _NodeIndex(graph.node_coords)
    items: list[dict] = []
    stats: dict[str, int] = {}

    for n, (_name, lon, lat, pop, place_type) in enumerate(settlements, 1):
        if n % DETECT_HEARTBEAT_EVERY == 0:
            if check_cancel is not None:
                check_cancel()
            if heartbeat is not None:
                heartbeat(f"bypasses: {n:,}/{len(settlements):,} settlements (planning)")
        settlement_type = _classify_settlement(place_type, pop)
        r_core = SETTLEMENT_RADII.get(settlement_type, 1_500.0)
        r_outer = r_core * R_OUTER_FACTOR

        # Find entry/exit nodes
        center_node = node_index.nearest_within(lon, lat, r_core)
        entry_exit = _find_entry_exit_nodes(boundary_index, lon, lat, r_outer)
        if len(entry_exit) < 2:
            _tally(stats, "settlement: <2 entry/exit nodes on a road of sufficient class")
            continue

        # Pair by angular separation (>90°).
        # ⚠️ This used to call `_pair_by_angle`, a STUB that returned [] on
        # every call — its own comment said it "will be called from
        # detect_bypasses with proper context", but detect_bypasses called the
        # stub, never the real `_pair_entry_exit_by_angle` right beneath it,
        # which needs `node_coords`. So bypass detection could not return a
        # bypass for ANY region, ever: every settlement fell out here, before a
        # single route was requested. Measured 2026-09-23 on Washington — 2,198
        # settlements, 0 entry/exit pairs examined, 0 bypasses, and the step
        # still reported success in 3m41s of work that was all index-building.
        pairs = _pair_entry_exit_by_angle(entry_exit, lon, lat, graph.node_coords)
        if not pairs:
            _tally(stats, "settlement: no entry/exit pair >90 deg apart")
            continue
        items.append({"lon": lon, "lat": lat, "r_core": r_core, "center": center_node,
                      "pairs": [list(p) for p in pairs]})
    return items, stats


def evaluate_bypasses(
    items: list[dict],
    node_coords,
    graph_dir: str,
    profile: str,
    segment_index: SegmentIndex,
    fc_of: Callable[[int], float | None],
    stats: dict[str, int],
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> dict[int, str]:
    """The routing half: route each planned pair and flag bypass / through-town
    edges. The first flag an edge gets wins, in item order, so evaluating
    items in chunks and merging the chunks in order gives the same flags."""
    flags: dict[int, str] = {}
    for n, item in enumerate(items, 1):
        if n % DETECT_HEARTBEAT_EVERY == 0:
            if check_cancel is not None:
                check_cancel()
            if heartbeat is not None:
                heartbeat(f"bypasses: {n:,}/{len(items):,} settlements routed")
        for entry_node, exit_node in item["pairs"]:
            _tally(stats, "_pairs_examined")
            bypass_edges, thru_edges = _check_bypass_pair(
                node_coords,
                fc_of,
                entry_node,
                exit_node,
                item["lon"],
                item["lat"],
                item["r_core"],
                graph_dir,
                profile,
                segment_index,
                item["center"],
                stats,
            )
            for eid in bypass_edges:
                if eid not in flags:
                    flags[eid] = "bypass"
            for eid in thru_edges:
                if eid not in flags:
                    flags[eid] = "thru_town"
    return flags


def log_bypass_result(flags: dict, settlements: int, stats: dict[str, int]) -> None:
    stats = dict(stats)
    pairs_examined = stats.pop("_pairs_examined", 0)
    log.info(
        "Detected bypass flags on %d edges (%d settlements, %d entry/exit pairs examined)",
        len(flags), settlements, pairs_examined,
    )
    if stats:
        log.info(
            "Bypass rejections by reason: %s",
            ", ".join(f"{k} = {v}" for k, v in sorted(stats.items(), key=lambda kv: -kv[1])),
        )


def _tally(stats: dict[str, int], reason: str) -> None:
    stats[reason] = stats.get(reason, 0) + 1


def _graph_fc(graph: RoadGraph) -> Callable[[int], float | None]:
    def fc_of(eid: int) -> float | None:
        e = graph.edge_by_id.get(eid)
        return e.fc_score if e is not None else None

    return fc_of


def _graph_coords(graph: RoadGraph) -> Callable[[int], list | None]:
    def coords_of(eid: int) -> list | None:
        e = graph.edge_by_id.get(eid)
        return e.coords if e is not None else None

    return coords_of


def large_cities(settlements: list) -> list:
    return [
        (name, lon, lat, pop, pt)
        for name, lon, lat, pop, pt in settlements
        if pop >= RING_MIN_POPULATION or pt == "city"
    ]


def detect_rings(
    graph: RoadGraph,
    cities_path: str,
    graph_dir: str,
    profile: str,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    segment_index: SegmentIndex | None = None,
) -> dict[int, bool]:
    """Detect ring roads around large settlements.

    Heartbeat-aware for the same reason as ``detect_bypasses``: it routes
    ``RING_SAMPLE_PAIRS`` pairs per city and reports nothing until it ends.
    Split into ``plan_rings`` and ``evaluate_rings`` the same way.

    Returns:
        Dict mapping edge_id to True for ring road edges.
    """
    cities = large_cities(_load_settlements(cities_path))
    if not cities:
        log.info("No large cities for ring detection")
        return {}

    # Reuse the caller's index when given: at country scale each SegmentIndex
    # is several GiB, and building fresh ones here while step 3's is still
    # alive is what pushed the US build past the VM's memory.
    if segment_index is None:
        segment_index = SegmentIndex(graph)
    items = plan_rings(graph, cities, heartbeat, check_cancel)
    ring_edges = evaluate_rings(items, graph.node_coords, graph_dir, profile, segment_index,
                                _graph_coords(graph), heartbeat, check_cancel)
    log.info("Detected ring flags on %d edges", len(ring_edges))
    return ring_edges


def plan_rings(
    graph: RoadGraph,
    cities: list,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> list[dict]:
    """The geometry half: each large city's radial pairs to route."""
    index = _BoundaryEdgeIndex(graph)
    items: list[dict] = []
    for n, (name, lon, lat, pop, _pt) in enumerate(cities, 1):
        if check_cancel is not None:
            check_cancel()
        if heartbeat is not None:
            heartbeat(f"rings: {n:,}/{len(cities):,} cities (planning)")
        # Urban boundary radius (spec §10)
        b_radius_m = min(25_000.0, (8 + math.sqrt(pop / 50_000)) * 1_000)
        radial_nodes = _find_radial_entries(graph, lon, lat, b_radius_m, RING_RADIAL_COUNT,
                                            index=index)
        if len(radial_nodes) < 4:
            log.debug("Too few radial entries (%d) for %s", len(radial_nodes), name)
            continue
        items.append({"name": name, "lon": lon, "lat": lat, "b_radius": b_radius_m,
                      "pairs": [list(p) for p in _sample_radial_pairs(radial_nodes,
                                                                      RING_SAMPLE_PAIRS)]})
    return items


def evaluate_rings(
    items: list[dict],
    node_coords,
    graph_dir: str,
    profile: str,
    segment_index: SegmentIndex,
    coords_of: Callable[[int], list | None],
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> dict[int, bool]:
    """The routing half: route each city's radial pairs and keep the edges
    that form a ring. Each city is judged on its own, so chunks merge by union."""
    ring_edges: dict[int, bool] = {}
    r_core = SETTLEMENT_RADII.get("city", 3_000.0)
    for n, item in enumerate(items, 1):
        if check_cancel is not None:
            check_cancel()
        if heartbeat is not None:
            heartbeat(f"rings: {n:,}/{len(items):,} cities routed")
        name, lon, lat, b_radius_m = item["name"], item["lon"], item["lat"], item["b_radius"]

        # Sample radial pairs and route
        candidate_votes: dict[int, int] = defaultdict(int)
        total_pairs = 0
        for na, nb in item["pairs"]:
            # Route via center (approximating "through core" penalty)
            coords_direct, time_direct = _route_pair_with_time(
                na,
                nb,
                node_coords,
                graph_dir,
                profile,
            )
            if not coords_direct or time_direct <= 0:
                continue

            total_pairs += 1

            # Snap direct route to edges and count orbital votes
            matched = segment_index.snap_route(coords_direct)
            for eid in matched:
                # Only count edges outside core
                coords = coords_of(eid)
                if coords:
                    mid_lon = (coords[0][0] + coords[-1][0]) / 2
                    mid_lat = (coords[0][1] + coords[-1][1]) / 2
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
            coords = coords_of(eid)
            if not coords:
                continue
            for coord in coords:
                radii.append(_haversine_m(coord[0], coord[1], lon, lat))

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
    return ring_edges


def _load_settlements(
    cities_path: str,
) -> list[tuple[str, float, float, int, str]]:
    """Load settlements from cities GeoJSON.

    Returns list of (name, lon, lat, population, place_type).
    """
    try:
        geojson = read_storage_json(cities_path)
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

        if not is_settlement(props):
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


class _BoundaryEdgeIndex:
    """Grid of the edges that could cross a settlement's boundary circle.

    ⚠️ Replaces a full scan of every edge in the region PER SETTLEMENT — an
    O(edges x settlements) cost. Measured 2026-09-24 on Washington: 117,916
    edges x 2,198 settlements is ~259M haversine evaluations, and it dominated
    bypass detection at 0.62 settlements/s (~56 min), against ~17 min for all
    79k routing calls the same phase makes. The geometry, not the router, was
    the expense.

    Exact rather than approximate: an edge crosses the circle only if one of
    its endpoints is INSIDE it, so every crossing edge is reachable from a cell
    within r_outer of the centre. The identical haversine test then runs on
    that shortlist, so the result is unchanged.
    """

    CELL_DEG = 0.02  # ~2.2 km of latitude; r_outer here is 1.75-7.5 km

    def __init__(self, graph: RoadGraph) -> None:
        self._grid: dict[tuple[int, int], list[tuple]] = defaultdict(list)
        # Position of each indexed edge in graph.edges, so a caller that must
        # see edges in the order a full scan would (ring detection) can sort.
        self._ordinal: dict[int, int] = {}
        for i, edge in enumerate(graph.edges):
            if edge.fc_score < MIN_BYPASS_FC_SCORE:
                continue
            from_coord = graph.node_coords.get(edge.from_node)
            to_coord = graph.node_coords.get(edge.to_node)
            if not from_coord or not to_coord:
                continue
            entry = (edge, from_coord, to_coord)
            self._ordinal[edge.edge_id] = i
            for lon, lat in (from_coord, to_coord):
                self._grid[(int(lon / self.CELL_DEG), int(lat / self.CELL_DEG))].append(entry)

    def near_in_graph_order(self, center_lon: float, center_lat: float, radius_m: float,
                            min_fc: float):
        """``near`` restricted to ``fc_score >= min_fc`` (at least the index's
        own floor), in graph.edges order -- exactly the edges, and the order, a
        full scan with that floor would visit among those that can cross."""
        got = [e for e in self.near(center_lon, center_lat, radius_m) if e[0].fc_score >= min_fc]
        got.sort(key=lambda e: self._ordinal[e[0].edge_id])
        return got

    def near(self, center_lon: float, center_lat: float, radius_m: float):
        """Yield each candidate edge once, with its endpoint coordinates."""
        d_lat = radius_m / 111_320.0
        d_lon = radius_m / max(1.0, 111_320.0 * math.cos(math.radians(center_lat)))
        nx = int(d_lon / self.CELL_DEG) + 1
        ny = int(d_lat / self.CELL_DEG) + 1
        cx = int(center_lon / self.CELL_DEG)
        cy = int(center_lat / self.CELL_DEG)
        seen: set[int] = set()
        for x in range(cx - nx, cx + nx + 1):
            for y in range(cy - ny, cy + ny + 1):
                for entry in self._grid.get((x, y), ()):
                    if entry[0].edge_id not in seen:
                        seen.add(entry[0].edge_id)
                        yield entry


class _NodeIndex:
    """Grid of every graph node, for "nearest node within r" lookups.

    ⚠️ Replaces a scan of ALL node_coords, which ran once per entry/exit PAIR
    even though its inputs (the settlement centre and r_core) vary only per
    SETTLEMENT. Measured 2026-09-24 on Washington: 95,062 nodes x 26,459 pairs
    = 2.5 BILLION haversine evaluations, ~28 minutes — the dominant cost of
    bypass detection, ahead of the 79k routing calls it exists to set up.
    Hoisting the call to the settlement loop removes 92% of it by itself; the
    grid removes the rest.
    """

    CELL_DEG = 0.02

    def __init__(self, node_coords: dict[int, tuple[float, float]]) -> None:
        self._grid: dict[tuple[int, int], list[tuple[int, float, float]]] = defaultdict(list)
        for nid, (lon, lat) in node_coords.items():
            self._grid[(int(lon / self.CELL_DEG), int(lat / self.CELL_DEG))].append((nid, lon, lat))

    def nearest_within(self, lon: float, lat: float, radius_m: float) -> int | None:
        d_lat = radius_m / 111_320.0
        d_lon = radius_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
        nx = int(d_lon / self.CELL_DEG) + 1
        ny = int(d_lat / self.CELL_DEG) + 1
        cx, cy = int(lon / self.CELL_DEG), int(lat / self.CELL_DEG)
        best_id, best_d = None, radius_m
        for x in range(cx - nx, cx + nx + 1):
            for y in range(cy - ny, cy + ny + 1):
                for nid, nlon, nlat in self._grid.get((x, y), ()):
                    d = _haversine_m(nlon, nlat, lon, lat)
                    if d < best_d:
                        best_d, best_id = d, nid
        return best_id


def _find_entry_exit_nodes(
    index: "_BoundaryEdgeIndex",
    center_lon: float,
    center_lat: float,
    r_outer: float,
) -> list[int]:
    """Find nodes where major roads cross the outer boundary circle."""
    entry_nodes: list[int] = []
    seen_nodes: set[int] = set()

    for edge, from_coord, to_coord in index.near(center_lon, center_lat, r_outer):
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
    node_coords,
    fc_of: Callable[[int], float | None],
    entry_node: int,
    exit_node: int,
    center_lon: float,
    center_lat: float,
    r_core: float,
    graph_dir: str,
    profile: str,
    segment_index: SegmentIndex,
    center_node: int | None,
    stats: dict[str, int] | None = None,
) -> tuple[set[int], set[int]]:
    """Check if a bypass exists for an entry/exit pair.

    Every rejection is tallied into ``stats`` by reason. A bypass count of 0 is
    otherwise unexplainable — and indistinguishable from the step not running
    at all, which is exactly what it did for as long as the cities file was
    empty (it returned ``{}`` immediately). Measured 2026-09-23 on Washington:
    the step does 3m41s of real routing and still reports 0, so the breakdown
    below is the only way to tell a correct 0 from a mis-tuned threshold.

    Returns (bypass_edge_ids, thru_town_edge_ids).
    """
    bypass_edges: set[int] = set()
    thru_edges: set[int] = set()

    def _reject(reason: str) -> tuple[set[int], set[int]]:
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1
        return bypass_edges, thru_edges

    if not HAS_REQUESTS:
        return _reject("no requests library")

    # P_fast: unconstrained fastest route
    coords_fast, time_fast = _route_pair_with_time(
        entry_node,
        exit_node,
        node_coords,
        graph_dir,
        profile,
    )
    if not coords_fast or time_fast <= 0:
        return _reject("no fastest route between entry/exit")

    # P_thru: approximate route through town center by adding center as waypoint.
    # center_node is computed ONCE PER SETTLEMENT by the caller — it depends
    # only on the centre and r_core, and recomputing it per pair cost 2.5
    # billion haversine evaluations on Washington (see _NodeIndex).
    if center_node is None:
        return _reject("no graph node within r_core of the centre")

    # Route entry → center → exit
    _c1, time_thru_1 = _route_pair_with_time(
        entry_node,
        center_node,
        node_coords,
        graph_dir,
        profile,
    )
    _c2, time_thru_2 = _route_pair_with_time(
        center_node,
        exit_node,
        node_coords,
        graph_dir,
        profile,
    )
    if time_thru_1 <= 0 or time_thru_2 <= 0:
        return _reject("no route through the centre")

    time_thru = time_thru_1 + time_thru_2

    # Check bypass criteria (spec §9)
    if time_fast > BYPASS_TIME_RATIO * time_thru:
        return _reject("gate 1: fastest route not >=15% quicker than through-town")

    # Check P_fast spends <20% within r_core
    core_count = 0
    for coord in coords_fast:
        d = _haversine_m(coord[0], coord[1], center_lon, center_lat)
        if d < r_core:
            core_count += 1
    core_fraction = core_count / len(coords_fast) if coords_fast else 1.0
    if core_fraction > BYPASS_CORE_FRACTION_MAX:
        return _reject("gate 2: fastest route stays inside the core")

    # Check FC advantage
    fast_edges = segment_index.snap_route(coords_fast)
    fast_fc_avg = _avg_fc(fc_of, fast_edges)

    # Get thru-town edges (approximate via center)
    thru_coords_1 = _c1 or []
    thru_coords_2 = _c2 or []
    thru_edge_set = segment_index.snap_route(thru_coords_1)
    thru_edge_set |= segment_index.snap_route(thru_coords_2)
    thru_fc_avg = _avg_fc(fc_of, thru_edge_set)

    if fast_fc_avg < thru_fc_avg + BYPASS_FC_ADVANTAGE:
        return _reject("gate 3: no road-class advantage")

    # Mark edges
    bypass_edges = fast_edges
    thru_edges = thru_edge_set - fast_edges

    return bypass_edges, thru_edges


def _avg_fc(fc_of: Callable[[int], float | None], edge_ids: set[int]) -> float:
    """Average functional class score of a set of edges (unknown edges count 0)."""
    if not edge_ids:
        return 0.0
    total = sum(fc for fc in (fc_of(eid) for eid in edge_ids) if fc is not None)
    return total / len(edge_ids)


def _avg_fc_score(graph: RoadGraph, edge_ids: set[int]) -> float:
    """Average functional class score of a set of edges."""
    return _avg_fc(_graph_fc(graph), edge_ids)


RING_MIN_FC_SCORE = 0.60  # secondary or above


def _find_radial_entries(
    graph: RoadGraph,
    center_lon: float,
    center_lat: float,
    b_radius_m: float,
    n_radials: int,
    index: "_BoundaryEdgeIndex | None" = None,
) -> list[int]:
    """Find radial entry nodes where major roads cross urban boundary.

    Selects nodes evenly distributed around the compass.

    ⚠️ With ``index``, only edges that can cross the circle are visited, in
    graph order, so the result is identical. Without it every edge of the
    region is scanned per city: on the US graph (~2M edges) that was ~4 s a
    city, and most of ring detection's hour for 1,082 cities.
    """
    # Find all edges crossing the boundary
    crossing_nodes: list[tuple[int, float]] = []  # (node_id, angle)
    seen: set[int] = set()

    if index is not None:
        candidates = index.near_in_graph_order(center_lon, center_lat, b_radius_m,
                                               RING_MIN_FC_SCORE)
    else:
        candidates = []
        for edge in graph.edges:
            if edge.fc_score < RING_MIN_FC_SCORE:
                continue
            from_coord = graph.node_coords.get(edge.from_node)
            to_coord = graph.node_coords.get(edge.to_node)
            if not from_coord or not to_coord:
                continue
            candidates.append((edge, from_coord, to_coord))

    for edge, from_coord, to_coord in candidates:

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
    ensure_dir(path)
    with open_output(path, "w") as f:
        json.dump({str(k): v for k, v in flags.items()}, f)


def load_bypass_flags(path: str) -> dict[int, str]:
    """Load bypass flags from JSON file."""
    if not path:
        return {}
    try:
        data = read_storage_json(path)
        return {int(k): v for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}


def save_ring_flags(flags: dict[int, bool], path: str) -> None:
    """Save ring flags to JSON file."""
    ensure_dir(path)
    with open_output(path, "w") as f:
        json.dump({str(k): v for k, v in flags.items()}, f)


def load_ring_flags(path: str) -> dict[int, bool]:
    """Load ring flags from JSON file."""
    if not path:
        return {}
    try:
        data = read_storage_json(path)
        return {int(k): v for k, v in data.items()}
    except (OSError, json.JSONDecodeError):
        return {}
