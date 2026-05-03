"""Scoring, adaptive cell budgets, greedy selection, and backbone repair.

Computes per-zoom edge scores, builds H3 hexagonal cell budgets,
performs budgeted greedy selection with backbone connectivity repair,
and enforces monotonic zoom reveal.
"""

import logging
from collections import defaultdict

log = logging.getLogger(__name__)

try:
    import h3

    HAS_H3 = True
except ImportError:
    HAS_H3 = False

from .zoom_graph import RoadGraph, _haversine_m

# Weight schedule (spec §7)
W_SB: dict[int, float] = {2: 0.75, 3: 0.70, 4: 0.65, 5: 0.60, 6: 0.55, 7: 0.50}
W_FC: dict[int, float] = {2: 0.25, 3: 0.30, 4: 0.35, 5: 0.40, 6: 0.45, 7: 0.50}
W_BT = 0.05
W_REF = 0.03
W_SPECIAL: dict[int, float] = {
    2: 0.03,
    3: 0.03,
    4: 0.08,
    5: 0.08,
    6: 0.08,
    7: 0.04,
}

# Base budget per cell in km (spec §6)
BASE_KM: dict[int, float] = {
    2: 80.0,
    3: 160.0,
    4: 260.0,
    5: 420.0,
    6: 650.0,
    7: 900.0,
}

# Sparse region floor km (spec §8.3)
MIN_KM: dict[int, float] = {
    2: 10.0,
    3: 20.0,
    4: 40.0,
    5: 70.0,
    6: 120.0,
    7: 180.0,
}

# H3 resolution for cell budgets (~1.2km edge, ~5.2 km² area)
H3_RESOLUTION = 7

# Density factor thresholds
DENSITY_SPARSE = 0.2  # km road per km² → factor 1.3
DENSITY_NORMAL = 1.0  # → factor 1.0
DENSITY_DENSE = 5.0  # → factor 0.6
DENSITY_ULTRA = 15.0  # → factor 0.4


def compute_scores(
    graph: RoadGraph,
    sbs_by_zoom: dict[int, dict[int, float]],
    bypass_flags: dict[int, str] | None = None,
    ring_flags: dict[int, bool] | None = None,
) -> dict[int, dict[int, float]]:
    """Compute per-zoom scores for all edges (spec §7).

    Score_z(e) = wSB(z)*SB_z(e) + wFC(z)*fcScore(e)
               + wBT*bridgeTunnel(e) + wREF*refBonus(e)
               + wSPECIAL*ringBoost(e,z) + wSPECIAL*bypassBoost(e,z)

    Returns:
        Dict[zoom_level, Dict[edge_id, score]]
    """
    bypass_flags = bypass_flags or {}
    ring_flags = ring_flags or {}

    scores: dict[int, dict[int, float]] = {}

    for z in range(2, 8):
        z_scores: dict[int, float] = {}
        sbs = sbs_by_zoom.get(z, {})
        w_sb = W_SB.get(z, 0.5)
        w_fc = W_FC.get(z, 0.5)
        w_special = W_SPECIAL.get(z, 0.05)

        for edge in graph.edges:
            eid = edge.edge_id
            sb = sbs.get(eid, 0.0)
            fc = edge.fc_score

            score = w_sb * sb + w_fc * fc

            # Bridge/tunnel bonus
            if edge.bridge or edge.tunnel:
                score += W_BT

            # Ref bonus
            if edge.ref:
                score += W_REF

            # Bypass boost
            if bypass_flags.get(eid) == "bypass":
                score += w_special

            # Ring boost
            if ring_flags.get(eid, False):
                score += w_special

            z_scores[eid] = max(0.0, min(1.2, score))

        scores[z] = z_scores

    return scores


def build_cell_budgets(
    graph: RoadGraph,
    anchors_by_zoom: dict[int, list[int]],
) -> dict[int, dict[str, dict]]:
    """Build adaptive H3 cell budgets per zoom level (spec §6).

    Returns:
        Dict[zoom_level, Dict[cell_id, {budget_km, density_factor}]]
    """
    if not HAS_H3:
        log.warning("h3 not available, using flat budgets")
        return _flat_budgets(graph)

    # Map edges to H3 cells
    edge_cells: dict[int, set[str]] = {}  # edge_id → set of cell IDs
    cell_road_km: dict[str, float] = defaultdict(float)
    cell_edges: dict[str, set[int]] = defaultdict(set)

    for edge in graph.edges:
        cells = _edge_to_cells(edge)
        edge_cells[edge.edge_id] = cells
        km_per_cell = (edge.length_m / 1000.0) / max(1, len(cells))
        for cell in cells:
            cell_road_km[cell] += km_per_cell
            cell_edges[cell].add(edge.edge_id)

    # Compute anchor density per cell per zoom
    cell_anchor_count: dict[int, dict[str, int]] = {}
    for z, anchors in anchors_by_zoom.items():
        counts: dict[str, int] = defaultdict(int)
        for nid in anchors:
            coord = graph.node_coords.get(nid)
            if coord:
                cell = h3.latlng_to_cell(coord[1], coord[0], H3_RESOLUTION)
                counts[cell] += 1
        cell_anchor_count[z] = dict(counts)

    # H3 cell area in km²
    cell_area_km2 = h3.cell_area(h3.latlng_to_cell(45.0, 0.0, H3_RESOLUTION), unit="km^2")

    budgets: dict[int, dict[str, dict]] = {}
    all_cells = set(cell_road_km.keys())

    for z in range(2, 8):
        z_budgets: dict[str, dict] = {}
        base = BASE_KM.get(z, 500.0)
        anchors_z = cell_anchor_count.get(z, {})

        for cell in all_cells:
            road_km = cell_road_km.get(cell, 0.0)
            density = road_km / cell_area_km2 if cell_area_km2 > 0 else 0.0

            # Density factor
            if density < DENSITY_SPARSE:
                factor = 1.3
            elif density < DENSITY_NORMAL:
                factor = 1.0
            elif density < DENSITY_DENSE:
                factor = 0.6
            else:
                factor = 0.4

            budget_km = base * factor

            z_budgets[cell] = {
                "budget_km": budget_km,
                "density_factor": factor,
                "road_km": road_km,
                "anchor_count": anchors_z.get(cell, 0),
            }

        budgets[z] = z_budgets

    return budgets


def _flat_budgets(graph: RoadGraph) -> dict[int, dict[str, dict]]:
    """Fallback flat budgets when H3 is not available."""
    budgets: dict[int, dict[str, dict]] = {}
    for z in range(2, 8):
        budgets[z] = {
            "flat": {
                "budget_km": BASE_KM.get(z, 500.0),
                "density_factor": 1.0,
                "road_km": sum(e.length_m / 1000 for e in graph.edges),
                "anchor_count": 0,
            }
        }
    return budgets


def _edge_to_cells(edge) -> set[str]:
    """Map an edge to H3 cells by sampling points along its polyline."""
    if not HAS_H3:
        return {"flat"}

    cells: set[str] = set()
    # Sample every ~500m
    step_m = 500.0
    _total_m = 0.0

    for i in range(len(edge.coords)):
        lon, lat = edge.coords[i]
        try:
            cell = h3.latlng_to_cell(lat, lon, H3_RESOLUTION)
            cells.add(cell)
        except Exception:
            pass

        if i < len(edge.coords) - 1:
            seg_len = _haversine_m(
                edge.coords[i][0],
                edge.coords[i][1],
                edge.coords[i + 1][0],
                edge.coords[i + 1][1],
            )
            # Add intermediate sample points
            n_samples = int(seg_len / step_m)
            for s in range(1, n_samples + 1):
                t = s / (n_samples + 1)
                ilon = edge.coords[i][0] + t * (edge.coords[i + 1][0] - edge.coords[i][0])
                ilat = edge.coords[i][1] + t * (edge.coords[i + 1][1] - edge.coords[i][1])
                try:
                    cell = h3.latlng_to_cell(ilat, ilon, H3_RESOLUTION)
                    cells.add(cell)
                except Exception:
                    pass

    return cells if cells else {"flat"}


def select_edges(
    graph: RoadGraph,
    scores: dict[int, dict[int, float]],
    budgets: dict[int, dict[str, dict]],
    anchors_by_zoom: dict[int, list[int]],
    bypass_flags: dict[int, str] | None = None,
    ring_flags: dict[int, bool] | None = None,
) -> dict[int, set[int]]:
    """Budgeted greedy selection with backbone repair (spec §8).

    Returns:
        Dict[zoom_level, set of selected edge_ids]
    """
    bypass_flags = bypass_flags or {}
    ring_flags = ring_flags or {}

    # Precompute edge-to-cell mapping
    if HAS_H3:
        edge_cells: dict[int, set[str]] = {}
        for edge in graph.edges:
            edge_cells[edge.edge_id] = _edge_to_cells(edge)
    else:
        edge_cells = {e.edge_id: {"flat"} for e in graph.edges}

    selected_by_zoom: dict[int, set[int]] = {}

    for z in range(2, 8):
        z_scores = scores.get(z, {})
        z_budgets = budgets.get(z, {})
        anchors = anchors_by_zoom.get(z, [])

        # Track used budget per cell
        cell_used_km: dict[str, float] = defaultdict(float)

        # Sort edges by score descending
        candidates = sorted(
            z_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        selected: set[int] = set()

        # Greedy selection (spec §8.1)
        for eid, _score in candidates:
            edge = graph.edge_by_id.get(eid)
            if not edge:
                continue

            edge_km = edge.length_m / 1000.0
            cells = edge_cells.get(eid, set())

            # Check if adding this edge exceeds budget in any cell
            can_add = True
            for cell in cells:
                budget_info = z_budgets.get(cell, {})
                budget_km = budget_info.get("budget_km", BASE_KM.get(z, 500.0))
                km_per_cell = edge_km / max(1, len(cells))
                if cell_used_km[cell] + km_per_cell > budget_km:
                    can_add = False
                    break

            if can_add:
                selected.add(eid)
                for cell in cells:
                    km_per_cell = edge_km / max(1, len(cells))
                    cell_used_km[cell] += km_per_cell

        # Backbone connectivity repair (spec §8.2)
        backbone_added = _backbone_repair(graph, selected, anchors, edge_cells)
        selected |= backbone_added

        # Sparse region floor (spec §8.3)
        min_km = MIN_KM.get(z, 10.0)
        for cell, budget_info in z_budgets.items():
            if budget_info.get("anchor_count", 0) > 0:
                if cell_used_km.get(cell, 0) < min_km:
                    # Find highest-scoring unselected edges in this cell
                    for eid, _score in candidates:
                        if eid in selected:
                            continue
                        cells = edge_cells.get(eid, set())
                        if cell in cells:
                            edge = graph.edge_by_id.get(eid)
                            if edge:
                                selected.add(eid)
                                edge_km = edge.length_m / 1000.0
                                cell_used_km[cell] += edge_km / max(1, len(cells))
                                if cell_used_km[cell] >= min_km:
                                    break

        selected_by_zoom[z] = selected
        log.info(
            "Zoom %d: selected %d edges (%.0f km)",
            z,
            len(selected),
            sum(graph.edge_by_id[e].length_m / 1000 for e in selected if e in graph.edge_by_id),
        )

    return selected_by_zoom


def _backbone_repair(
    graph: RoadGraph,
    selected: set[int],
    anchors: list[int],
    edge_cells: dict[int, set[str]],
) -> set[int]:
    """Ensure backbone connectivity between anchors via selected edges.

    For each anchor, check connectivity to at least 2 other anchors
    via selected edges. If not, add shortest path edges.
    """
    if len(anchors) < 2:
        return set()

    # Build subgraph adjacency from selected edges
    sub_adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for eid in selected:
        edge = graph.edge_by_id.get(eid)
        if edge:
            sub_adj[edge.from_node].append((edge.to_node, eid))
            sub_adj[edge.to_node].append((edge.from_node, eid))

    anchor_set = set(anchors)
    added: set[int] = set()

    # Check connectivity for a sample of anchors
    sample_size = min(50, len(anchors))
    import random

    rng = random.Random(42)
    sampled_anchors = rng.sample(anchors, sample_size) if len(anchors) > sample_size else anchors

    for anchor in sampled_anchors:
        # BFS to find reachable anchors
        visited: set[int] = set()
        queue = [anchor]
        visited.add(anchor)
        reachable_anchors = 0

        while queue and reachable_anchors < 2:
            node = queue.pop(0)
            for neighbor, _eid in sub_adj.get(node, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
                    if neighbor in anchor_set:
                        reachable_anchors += 1

        if reachable_anchors < 2:
            # Find nearest unconnected anchor and add shortest path
            for other_anchor in anchors:
                if other_anchor == anchor or other_anchor in visited:
                    continue
                path_edges = graph.shortest_path(anchor, other_anchor)
                if path_edges:
                    added.update(path_edges)
                    break

    if added:
        log.info("Backbone repair added %d edges", len(added))

    return added


def enforce_monotonic_reveal(
    selected_by_zoom: dict[int, set[int]],
) -> dict[int, int]:
    """Enforce monotonic zoom reveal and assign minZoom per edge (spec §11).

    S'_2 = S_2; for z=3..7: S'_z = S_z ∪ S'_{z-1}
    minZoom(e) = smallest z where e ∈ S'_z

    Returns:
        Dict[edge_id, min_zoom_level]
    """
    cumulative: dict[int, set[int]] = {}
    cumulative[2] = set(selected_by_zoom.get(2, set()))

    for z in range(3, 8):
        cumulative[z] = set(selected_by_zoom.get(z, set())) | cumulative[z - 1]

    # Assign minZoom
    assignments: dict[int, int] = {}
    for z in range(2, 8):
        for eid in cumulative[z]:
            if eid not in assignments:
                assignments[eid] = z

    # Log distribution
    dist: dict[int, int] = defaultdict(int)
    for _eid, z in assignments.items():
        dist[z] += 1
    log.info("Monotonic reveal: %s", ", ".join(f"z{z}={dist.get(z, 0)}" for z in range(2, 8)))

    return assignments
