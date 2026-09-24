"""Roads must appear gradually by functional class as you zoom in.

Before this, `select_edges` ranked every edge in the region by score with no
class gate, and score is 75% sampled betweenness at z2. Measured 2026-09-24 on
Washington: zoom 2 drew 340 tertiary and 589 secondary edges beside just 541 of
the state's 8,027 motorway edges — every class at once, and the interstate
network present only as 6.7% fragments, so the tiers looked identical.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_selection as zs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import FC_SCORES, LogicalEdge, RoadGraph  # noqa: E402

FCS = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified"]


def _graph():
    """One edge of every class, each 1 km."""
    g = RoadGraph()
    for i, fc in enumerate(FCS):
        g.add_edge(LogicalEdge(
            edge_id=i, from_node=i, to_node=i + 1, osm_way_ids=[i],
            coords=[(-122.0 + i * 0.01, 47.0), (-122.0 + (i + 1) * 0.01, 47.0)],
            length_m=1000.0, fc=fc, fc_score=FC_SCORES[fc], ref="", name="",
            maxspeed=50, lanes=2, bridge=False, tunnel=False, oneway=False,
            surface_unpaved=False))
        g.node_coords[i] = (-122.0 + i * 0.01, 47.0)
    g.node_coords[len(FCS)] = (-122.0 + len(FCS) * 0.01, 47.0)
    return g


def _select(budget_km: float, flip_scores: bool = False):
    """flip_scores: give the LOWEST class the highest score, so any leakage of
    a below-floor edge is a class-gate failure and not a scoring coincidence."""
    g = _graph()
    scores = {z: {e.edge_id: (e.edge_id if flip_scores else -e.edge_id) for e in g.edges}
              for z in range(2, 8)}
    budgets = {z: {} for z in range(2, 8)}
    if budget_km is not None:
        budgets = {z: {"flat": {"budget_km": budget_km, "anchor_count": 1}} for z in range(2, 8)}
    return g, zs.select_edges(g, scores, budgets, {z: [] for z in range(2, 8)})


def test_nothing_below_the_floor_is_ever_selected():
    g, sel = _select(budget_km=1000.0, flip_scores=True)
    for z, eids in sel.items():
        floor = FC_SCORES[zs.MIN_FC_BY_ZOOM[z]] - 1e-9
        for eid in eids:
            fc = g.edge_by_id[eid].fc
            assert FC_SCORES[fc] >= floor, f"{fc} leaked into zoom {z}"


def test_zoom_2_is_motorway_only():
    g, sel = _select(budget_km=1000.0, flip_scores=True)
    assert {g.edge_by_id[e].fc for e in sel[2]} == {"motorway"}


def test_each_zoom_admits_exactly_one_more_class():
    g, sel = _select(budget_km=1000.0, flip_scores=True)
    seen = [{g.edge_by_id[e].fc for e in sel[z]} for z in range(2, 8)]
    for z, classes in zip(range(2, 8), seen):
        expected = {fc for fc in FCS if FC_SCORES[fc] >= FC_SCORES[zs.MIN_FC_BY_ZOOM[z]] - 1e-9}
        assert classes == expected, f"zoom {z}: {classes} != {expected}"


def test_the_skeleton_survives_a_zero_budget():
    """A budget-truncated backbone reads as a broken network, not a sparse one,
    so every eligible skeleton-class edge must appear whatever the budget says."""
    g, sel = _select(budget_km=0.0)
    for z in range(2, 8):
        floor = FC_SCORES[zs.MIN_FC_BY_ZOOM[z]] - 1e-9
        expected = {e.edge_id for e in g.edges
                    if e.fc in zs.SKELETON_FCS and e.fc_score >= floor}
        assert expected <= sel[z], f"zoom {z} dropped part of the skeleton on a zero budget"
    # and at z2 nothing else can get in, because nothing else is eligible
    assert {g.edge_by_id[e].fc for e in sel[2]} == {"motorway"}


def test_the_skeleton_does_not_charge_the_budget():
    """It is mandatory structural content, not discretionary density.

    The first cut charged it, reasoning that lower classes should see the space
    the backbone occupies. The measurement refuted that: with it charged,
    Washington's zooms 6 and 7 admitted NOTHING new — every edge selected there
    was a backbone-repair edge (463/463 and 354/354) — so reveal stopped dead
    after z3 and the map was not gradual at all.
    """
    g = _graph()
    scores = {z: {e.edge_id: 1.0 for e in g.edges} for z in range(2, 8)}
    # 1 km of budget: enough for exactly one non-skeleton edge, and only if the
    # motorway+trunk skeleton (2 km) did not spend it first.
    budgets = {z: {"flat": {"budget_km": 1.0, "anchor_count": 0}} for z in range(2, 8)}
    sel = zs.select_edges(g, scores, budgets, {z: [] for z in range(2, 8)})
    assert "primary" in {g.edge_by_id[e].fc for e in sel[4]}, \
        "the skeleton must not consume the budget that lower classes need"


def test_backbone_repair_cannot_smuggle_in_a_lower_class():
    """The third path a below-floor road took to a zoom it does not belong at,
    after the greedy pass and the sparse top-up. Measured on Washington: all
    2,227 non-motorway edges at zoom 2 arrived this way."""
    g = _graph()
    # anchors at both ends force repair to look for a connecting path
    anchors = {z: [0, len(FCS)] for z in range(2, 8)}
    scores = {z: {e.edge_id: 1.0 for e in g.edges} for z in range(2, 8)}
    budgets = {z: {"flat": {"budget_km": 0.0, "anchor_count": 0}} for z in range(2, 8)}
    backbone: dict[int, set[int]] = {}
    sel = zs.select_edges(g, scores, budgets, anchors, backbone_out=backbone)
    for z, eids in backbone.items():
        floor = FC_SCORES[zs.MIN_FC_BY_ZOOM[z]] - 1e-9
        for eid in eids:
            assert g.edge_by_id[eid].fc_score >= floor, \
                f"backbone put {g.edge_by_id[eid].fc} into zoom {z}"
    assert {g.edge_by_id[e].fc for e in sel[2]} == {"motorway"}


def test_the_sparse_floor_cannot_smuggle_in_a_lower_class():
    """The top-up that keeps a sparse cell from going blank is class-gated too:
    it was the main route by which tertiary reached zoom 2, because rural cells
    are the common case."""
    g = _graph()
    scores = {z: {e.edge_id: e.edge_id for e in g.edges} for z in range(2, 8)}  # low class scores highest
    budgets = {z: {"flat": {"budget_km": 0.0, "anchor_count": 1}} for z in range(2, 8)}
    sel = zs.select_edges(g, scores, budgets, {z: [] for z in range(2, 8)})
    assert {g.edge_by_id[e].fc for e in sel[2]} == {"motorway"}
    assert {g.edge_by_id[e].fc for e in sel[3]} == {"motorway", "trunk"}
