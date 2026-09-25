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

from .zoom_graph import FC_SCORES, RoadGraph, _haversine_m

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

# Base budget per cell in km (spec §6), calibrated to the H3 cell it is spent in.
#
# ⚠️ These were 80/160/260/420/650/900 km — for a resolution-7 hexagon of
# ~5.2 km2. That is 15-170 km of road per 5 km2, i.e. denser than Manhattan,
# so the budget could not bind: measured 2026-09-24 on Washington, whose 23,752
# cells hold a MEDIAN of 2.7 km of road each, it bound in 30 cells at z2 and in
# ZERO cells from z4 down. The mechanism was inert.
#
# It went unnoticed because `h3` is not installed in the runner image, so
# `build_cell_budgets` fell back to `_flat_budgets` — ONE cell for the entire
# region — and that single statewide budget (260 km at z4, against 91,899 km of
# road) is what actually limited selection. The adaptive per-cell budget of
# spec 6 had never run at all.
# Minimum length (km) a DISCONNECTED piece of the selection must reach to be kept.
#
# ⚠️ Selection is PER EDGE and checks no connectivity. The greedy pass adds any
# edge that fits its cell budget, and the sparse-region floor pads an under-filled
# rural cell with "the highest-scoring unselected edges in this cell" one at a
# time — neither asks whether the edge touches anything already selected. Backbone
# repair enforces connectivity only between sampled ANCHORS, so it never reaches
# these. The result is orphan stubs, and they are not rare: measured 2026-09-25 on
# the published layers,
#
#   iowa     z7  48,482 edges -> 727 components; 671 under 2 km (92% of components)
#   nebraska z7  46,511 edges -> 1,012 components; 941 under 2 km (93%)
#
# — one giant network plus ~700-1,000 fragments, most of them ONE OR TWO edges.
# Only 2-3% of edges, but they dominate the eye in empty farmland because the real
# network is elsewhere.
#
# ⚠️ They are NOT disconnected in OSM. At z7 the class floor is `unclassified` and
# the roads joining these farm stubs to the network are `residential`/`service`,
# below the floor — the connector is real but invisible at this zoom. So this is
# cartographic generalisation, not data repair: at this scale an isolated two-block
# fragment carries no information, and dropping it is the standard treatment.
# ⚠️ CALIBRATED BY SWEEP, not chosen. The first cut scaled the threshold like the
# other ladders (50 km at z2 down to 2 km at z7) and was badly wrong in the middle:
# it removed 29% of z4's selected LENGTH on Iowa and 22% of z5's on Nebraska. At the
# low zooms the selection is a deliberately sparse SAMPLE, so it fragments by design
# and component length stops being a proxy for "orphan" — pruning there deletes the
# content, not the noise.
#
# Measured trade-off (% of selected length removed, iowa / nebraska):
#
#        0.5 km      1.0 km      2.0 km      5.0 km
#   z4   0.51/0.60   1.59/1.50   4.84/5.08  14.57/11.54
#   z5   0.32/0.42   0.72/0.93   2.15/3.47   8.12/14.21
#   z6   0.16/0.28   0.36/0.62   0.67/1.14   1.64/2.56
#   z7   0.07/0.13   0.14/0.28   0.22/0.52   0.34/0.83
#
# So the ladder RISES with zoom, which is right for the reason it looks backwards:
# the denser the tier, the smaller a share an orphan of a given length is, and the
# safer it is to drop. These values hold collateral under ~1% of length everywhere
# while still catching the reported stubs, which are well under 2 km.
MIN_COMPONENT_KM: dict[int, float] = {
    2: 0.5, 3: 0.5, 4: 0.5, 5: 1.0, 6: 1.5, 7: 2.5,
}

BASE_KM: dict[int, float] = {
    2: 1.0,
    3: 1.5,
    4: 2.5,
    5: 4.0,
    6: 7.0,
    7: 12.0,
}

# Sparse region floor km (spec §8.3) — same recalibration as BASE_KM above.
# At 10-180 km per 5.2 km2 cell this floor exceeded the road that EXISTS in a
# typical cell, so it padded almost every cell to its limit with whatever
# scored highest, which is how tertiary roads reached continental zooms.
MIN_KM: dict[int, float] = {
    2: 0.2,
    3: 0.3,
    4: 0.5,
    5: 0.8,
    6: 1.5,
    7: 2.5,
}

# Functional-class floor per zoom: an edge of a lower class is not a candidate
# at that zoom AT ALL. Without it, score alone decides what appears, and score
# is 75% sampled betweenness at z2 — so a tertiary street that happens to carry
# sampled routes outranks an interstate segment that does not.
#
# Measured 2026-09-24 on Washington: zoom 2 drew 340 tertiary and 589 secondary
# edges beside just 541 of the state's 8,027 motorway edges. Every class
# appeared at once (the thing progressive reveal exists to prevent) while the
# interstates appeared only as 6.7% fragments, so the tiers were visually
# indistinguishable.
MIN_FC_BY_ZOOM: dict[int, str] = {
    2: "motorway",       # interstates only — the continental skeleton
    3: "trunk",          # + intercity routes between major populated places
    4: "primary",
    5: "secondary",
    6: "tertiary",
    7: "unclassified",
}

# Classes revealed COMPLETE at the zoom they become eligible, exempt from the
# per-cell budget (they still CHARGE it, so lower classes see the space they
# consumed). A skeleton that is budget-truncated reads as a broken network
# rather than a sparse one: 6.7% of a state's motorway mileage drawn as
# disconnected stubs is worse than drawing none of it. The budget's job is
# capping local density, not deciding which roads form the backbone.
SKELETON_FCS: frozenset[str] = frozenset({"motorway", "trunk"})

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
    backbone_out: dict[int, set[int]] | None = None,
) -> dict[int, set[int]]:
    """Budgeted greedy selection with backbone repair (spec §8).

    ``backbone_out``, when given, is filled with ``{zoom: edges added by
    backbone repair}``. The repair has always run, but its result was unioned
    into the selection and then dropped — so the ``backbone`` flag the exports
    advertise was a hardcoded ``False`` on every edge of every file, and the
    ``backbone_edges`` metric a hardcoded 0. An out-param rather than a changed
    return type: the ``SelectEdges`` facet handler calls this too.

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

        # Class floor for this zoom (see MIN_FC_BY_ZOOM).
        fc_floor = FC_SCORES.get(MIN_FC_BY_ZOOM.get(z, "unclassified"), 0.0) - 1e-9

        # Skeleton classes first and in full, so the backbone is revealed
        # connected rather than budget-truncated.
        #
        # ⚠️ It does NOT charge the cell budget. It did in the first cut of
        # this, on the reasoning that lower classes should see the space the
        # backbone occupies — and the measurement refuted that: with the
        # skeleton charged, zooms 6 and 7 admitted NOTHING new (every edge
        # selected there was a backbone-repair edge, 463/463 and 354/354), so
        # reveal stopped dead after z3. The budget's job is capping
        # DISCRETIONARY density; mandatory structural content is not
        # discretionary, and making it compete starves everything after it.
        for edge in graph.edges:
            if edge.fc in SKELETON_FCS and edge.fc_score >= fc_floor:
                selected.add(edge.edge_id)

        # Greedy selection (spec §8.1)
        for eid, _score in candidates:
            edge = graph.edge_by_id.get(eid)
            if not edge:
                continue
            if edge.fc_score < fc_floor or eid in selected:
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
        backbone_added = _backbone_repair(graph, selected, anchors, edge_cells, fc_floor)
        selected |= backbone_added
        if backbone_out is not None:
            backbone_out[z] = set(backbone_added)

        # Sparse region floor (spec §8.3)
        min_km = MIN_KM.get(z, 10.0)
        for cell, budget_info in z_budgets.items():
            if budget_info.get("anchor_count", 0) > 0:
                if cell_used_km.get(cell, 0) < min_km:
                    # Find highest-scoring unselected edges in this cell.
                    # ⚠️ Subject to the SAME class floor as the greedy pass. This
                    # top-up exists so a sparse cell is not blank — but at a
                    # continental zoom a rural cell SHOULD hold nothing but the
                    # interstate crossing it, and padding it to 10 km with
                    # whatever local road scored highest is precisely how
                    # tertiary and unclassified roads reached zoom 2. Measured
                    # 2026-09-24: this path, not the greedy pass, put most of
                    # them there, because rural cells are the common case.
                    for eid, _score in candidates:
                        if eid in selected:
                            continue
                        cells = edge_cells.get(eid, set())
                        if cell in cells:
                            edge = graph.edge_by_id.get(eid)
                            if edge and edge.fc_score >= fc_floor:
                                selected.add(eid)
                                edge_km = edge.length_m / 1000.0
                                cell_used_km[cell] += edge_km / max(1, len(cells))
                                if cell_used_km[cell] >= min_km:
                                    break

        # Generalisation: drop the orphans the three passes above create.
        selected, pruned_n, pruned_km = prune_fragments(
            graph, selected, z, protected=backbone_added
        )

        selected_by_zoom[z] = selected
        log.info(
            "Zoom %d: selected %d edges (%.0f km); pruned %d orphan edges (%.0f km, "
            "components under %.0f km)",
            z,
            len(selected),
            sum(graph.edge_by_id[e].length_m / 1000 for e in selected if e in graph.edge_by_id),
            pruned_n,
            pruned_km,
            MIN_COMPONENT_KM.get(z, 0.0),
        )

    return selected_by_zoom




def prune_fragments(
    graph: RoadGraph,
    selected: set[int],
    zoom: int,
    protected: set[int] | None = None,
) -> tuple[set[int], int, float]:
    """Drop short pieces of the selection that connect to nothing.

    Returns ``(kept, dropped_edge_count, dropped_km)``.

    Two rules decide a component's fate, and the second is what keeps this from
    deleting real places:

    1. **Long enough is kept.** ``MIN_COMPONENT_KM[zoom]`` is the bar.
    2. **Complete is kept, however short.** A component none of whose nodes touch
       an unselected edge is not a fragment we created — it is a road network that
       is genuinely isolated in the data, an island's roads being the obvious case.
       Only components we CUT (some node continues into an edge we did not select)
       are eligible to be pruned.

    ``protected`` edges (backbone repair's output) are never dropped, and neither
    is any component containing one.
    """
    protected = protected or set()
    min_km = MIN_COMPONENT_KM.get(zoom)
    if not min_km or not selected:
        return set(selected), 0, 0.0

    # Components over the SELECTED subgraph, by node adjacency.
    comp_of: dict[int, int] = {}
    components: list[list[int]] = []
    sub_adj: dict[int, list[int]] = defaultdict(list)
    for eid in selected:
        edge = graph.edge_by_id.get(eid)
        if not edge:
            continue
        sub_adj[edge.from_node].append(eid)
        sub_adj[edge.to_node].append(eid)

    for start in sub_adj:
        if start in comp_of:
            continue
        cid = len(components)
        members: list[int] = []
        stack = [start]
        comp_of[start] = cid
        while stack:
            node = stack.pop()
            members.append(node)
            for eid in sub_adj.get(node, ()):
                edge = graph.edge_by_id.get(eid)
                if not edge:
                    continue
                other = edge.to_node if edge.from_node == node else edge.from_node
                if other not in comp_of:
                    comp_of[other] = cid
                    stack.append(other)
        components.append(members)

    comp_edges: dict[int, set[int]] = defaultdict(set)
    comp_km: dict[int, float] = defaultdict(float)
    for eid in selected:
        edge = graph.edge_by_id.get(eid)
        if not edge:
            continue
        cid = comp_of.get(edge.from_node)
        if cid is None:
            continue
        comp_edges[cid].add(eid)
        comp_km[cid] += edge.length_m / 1000.0

    kept = set(selected)
    dropped = 0
    dropped_km = 0.0
    for cid, edges in comp_edges.items():
        if comp_km[cid] >= min_km or edges & protected:
            continue
        # Rule 2: did we cut this, or is it isolated in the data?
        cut = any(
            any(e not in selected for e in graph.adj.get(node, ()))
            for node in components[cid]
        )
        if not cut:
            continue
        kept -= edges
        dropped += len(edges)
        dropped_km += comp_km[cid]

    return kept, dropped, dropped_km

def _backbone_repair(
    graph: RoadGraph,
    selected: set[int],
    anchors: list[int],
    edge_cells: dict[int, set[str]],
    fc_floor: float = 0.0,
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
                # Search the ELIGIBLE SUBGRAPH: a path found here is
                # class-compliant by construction, so the first one is usable.
                # Filtering afterwards meant rejecting a path and searching
                # again from the next anchor — ~7 min per zoom on Washington.
                path_edges = graph.shortest_path(anchor, other_anchor, fc_floor)
                if path_edges:
                    # ⚠️ Subject to the zoom's class floor. This was the THIRD
                    # path by which a below-floor road reached a zoom it does
                    # not belong at, after the greedy pass and the sparse-region
                    # top-up. Measured 2026-09-24 on Washington: of the 10,198
                    # edges at zoom 2, 2,227 were not motorway — 313 of them
                    # tertiary — and every one arrived here, which the (newly
                    # honest) `backbone` flag made provable.
                    #
                    # A path that needs a lower class to complete is not a
                    # backbone at this zoom: dropping it leaves those anchors
                    # unjoined, which is the truthful picture of a network whose
                    # arterials genuinely do not connect them.
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
