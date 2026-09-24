"""The boundary index must return exactly what the full scan returned.

`_find_entry_exit_nodes` scanned every edge in the region for every settlement
— O(edges x settlements). Measured 2026-09-24 on Washington: 117,916 x 2,198 is
~259M haversine evaluations, dominating bypass detection at 0.62 settlements/s
(~56 min) against ~17 min for all 79k routing calls the same phase makes.

An index is only worth having if it changes the cost and nothing else, so these
compare it against the naive scan it replaced.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_detection as zd  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph  # noqa: E402


def _naive(graph, center_lon, center_lat, r_outer):
    """The implementation this replaced, kept here as the oracle."""
    entry_nodes, seen = [], set()
    for edge in graph.edges:
        if edge.fc_score < zd.MIN_BYPASS_FC_SCORE:
            continue
        fc_ = graph.node_coords.get(edge.from_node)
        tc_ = graph.node_coords.get(edge.to_node)
        if not fc_ or not tc_:
            continue
        d_from = zd._haversine_m(fc_[0], fc_[1], center_lon, center_lat)
        d_to = zd._haversine_m(tc_[0], tc_[1], center_lon, center_lat)
        if (d_from <= r_outer) != (d_to <= r_outer):
            outer = edge.from_node if d_from > r_outer else edge.to_node
            if outer not in seen:
                seen.add(outer)
                entry_nodes.append(outer)
    return entry_nodes


def _random_graph(n=600, seed=7):
    rng = random.Random(seed)
    g = RoadGraph()
    for i in range(n):
        lon = -122.5 + rng.random() * 0.8
        lat = 47.0 + rng.random() * 0.6
        lon2 = lon + (rng.random() - 0.5) * 0.06
        lat2 = lat + (rng.random() - 0.5) * 0.06
        fc, score = rng.choice([("motorway", 1.0), ("trunk", 0.88), ("primary", 0.75),
                                ("secondary", 0.60), ("tertiary", 0.45),
                                ("unclassified", 0.30), ("residential", 0.18)])
        g.node_coords[2 * i] = (lon, lat)
        g.node_coords[2 * i + 1] = (lon2, lat2)
        g.add_edge(LogicalEdge(edge_id=i, from_node=2 * i, to_node=2 * i + 1, osm_way_ids=[i],
                               coords=[(lon, lat), (lon2, lat2)], length_m=1000.0, fc=fc,
                               fc_score=score, ref="", name="", maxspeed=50, lanes=2,
                               bridge=False, tunnel=False, oneway=False, surface_unpaved=False))
    return g


@pytest.mark.parametrize("r_outer", [1750.0, 3750.0, 7500.0])  # village, town, city
def test_index_matches_the_full_scan_everywhere(r_outer):
    g = _random_graph()
    idx = zd._BoundaryEdgeIndex(g)
    rng = random.Random(11)
    for _ in range(60):
        lon = -122.5 + rng.random() * 0.8
        lat = 47.0 + rng.random() * 0.6
        assert sorted(zd._find_entry_exit_nodes(idx, lon, lat, r_outer)) == \
            sorted(_naive(g, lon, lat, r_outer)), f"mismatch at {lon},{lat} r={r_outer}"


def test_index_yields_each_edge_once():
    """An edge is filed under both endpoints; a query spanning both must not
    hand it back twice."""
    g = _random_graph(n=50, seed=3)
    idx = zd._BoundaryEdgeIndex(g)
    got = list(idx.near(-122.1, 47.3, 50_000.0))
    assert len({e.edge_id for e, _f, _t in got}) == len(got)


def test_index_excludes_classes_below_the_bypass_floor():
    g = _random_graph()
    idx = zd._BoundaryEdgeIndex(g)
    for edge, _f, _t in idx.near(-122.1, 47.3, 80_000.0):
        assert edge.fc_score >= zd.MIN_BYPASS_FC_SCORE


def test_index_examines_far_fewer_edges_than_the_graph_holds():
    g = _random_graph(n=2000, seed=5)
    idx = zd._BoundaryEdgeIndex(g)
    looked_at = len(list(idx.near(-122.1, 47.3, 3750.0)))
    assert looked_at < len(g.edges) / 5, \
        f"index examined {looked_at} of {len(g.edges)} — no better than scanning"


def _naive_center(graph, lon, lat, r_core):
    """The per-pair scan this replaced, kept as the oracle."""
    best_id, best_d = None, float("inf")
    for nid, (nlon, nlat) in graph.node_coords.items():
        d = zd._haversine_m(nlon, nlat, lon, lat)
        if d < best_d and d < r_core:
            best_d, best_id = d, nid
    return best_id


@pytest.mark.parametrize("r_core", [700.0, 1500.0, 3000.0])  # village, town, city
def test_node_index_finds_the_same_centre_node_as_the_scan(r_core):
    g = _random_graph()
    idx = zd._NodeIndex(g.node_coords)
    rng = random.Random(23)
    for _ in range(80):
        lon = -122.5 + rng.random() * 0.8
        lat = 47.0 + rng.random() * 0.6
        got = idx.nearest_within(lon, lat, r_core)
        want = _naive_center(g, lon, lat, r_core)
        if got != want:  # a tie between equidistant nodes is acceptable
            assert got is not None and want is not None
            d_got = zd._haversine_m(*g.node_coords[got], lon, lat)
            d_want = zd._haversine_m(*g.node_coords[want], lon, lat)
            assert abs(d_got - d_want) < 1e-6, f"picked a farther node: {d_got} vs {d_want}"


def test_node_index_respects_the_radius():
    g = _random_graph()
    idx = zd._NodeIndex(g.node_coords)
    assert idx.nearest_within(0.0, 0.0, 1000.0) is None, "nothing is within 1 km of null island"


def test_centre_node_is_computed_per_settlement_not_per_pair():
    """It depends only on the centre and r_core; recomputing it per pair cost
    2.5 billion haversine evaluations on Washington."""
    src = Path(zd.__file__).read_text()
    assert "center_node = node_index.nearest_within(lon, lat, r_core)" in src
    assert "for nid, (nlon, nlat) in graph.node_coords.items():" not in src
