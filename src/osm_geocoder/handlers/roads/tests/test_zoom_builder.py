"""Tests for zoom builder handler and logic modules."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from osm_geocoder.handlers.roads.zoom_builder import (
    _empty_metrics,
    _empty_result,
    _export_zoom_geojson,
)
from osm_geocoder.handlers.roads.zoom_detection import (
    _classify_settlement,
    _find_radial_entries,
    _load_settlements,
    _sample_radial_pairs,
)
from osm_geocoder.handlers.roads.zoom_graph import (
    FC_SCORES,
    LogicalEdge,
    RoadGraph,
    _classify_fc,
    _compute_fc_score,
    _haversine_m,
    _polyline_length_m,
)
from osm_geocoder.handlers.roads.zoom_handlers import (
    NAMESPACE,
    ZOOM_FACETS,
    _make_build_anchors_handler,
    _make_build_logical_graph_handler,
    _make_build_zoom_layers_handler,
    _make_compute_sbs_handler,
    _make_compute_scores_handler,
    _make_detect_bypasses_handler,
    _make_detect_rings_handler,
    _make_export_zoom_layers_handler,
    _make_select_edges_handler,
    register_zoom_handlers,
)
from osm_geocoder.handlers.roads.zoom_sbs import (
    SegmentIndex,
    accumulate_votes,
    build_anchors,
    normalize_sbs,
    sample_od_pairs,
)
from osm_geocoder.handlers.roads.zoom_selection import (
    BASE_KM,
    HAS_H3,
    _flat_budgets,
    compute_scores,
    enforce_monotonic_reveal,
)

requires_h3 = pytest.mark.skipif(not HAS_H3, reason="h3 not installed")


@pytest.fixture(autouse=True)
def _isolate_output_base(tmp_path, monkeypatch):
    """Redirect the OSM output base to a tmp dir.

    The zoom-builder handlers resolve their output directory from
    ``AFL_OUTPUT_BASE`` (default ``/Volumes/afl_data/output``). Point it at a
    per-test tmp dir so the suite never touches the real data volume.
    """
    monkeypatch.setenv("AFL_OUTPUT_BASE", str(tmp_path / "output"))


def _make_edge(
    edge_id,
    from_node,
    to_node,
    coords,
    fc="primary",
    fc_score=0.75,
    length_m=None,
    ref="",
    name="",
    bridge=False,
    tunnel=False,
    oneway=False,
    surface_unpaved=False,
) -> LogicalEdge:
    """Create a LogicalEdge with sensible defaults."""
    if length_m is None:
        length_m = _polyline_length_m(coords)
    return LogicalEdge(
        edge_id=edge_id,
        from_node=from_node,
        to_node=to_node,
        osm_way_ids=[1000 + edge_id],
        coords=coords,
        length_m=length_m,
        fc=fc,
        fc_score=fc_score,
        ref=ref,
        name=name,
        maxspeed=0,
        lanes=2,
        bridge=bridge,
        tunnel=tunnel,
        oneway=oneway,
        surface_unpaved=surface_unpaved,
    )


def _make_test_graph() -> RoadGraph:
    """Create a small test graph with 5 edges and 6 nodes.

    Layout (approximate):
        1 ---e0--- 2 ---e1--- 3
        |                     |
       e2                    e3
        |                     |
        4 --------e4--------- 5
                  6 (disconnected, via e5... not added to keep it simple)
    Nodes at (lon, lat): 1=(0,0), 2=(0.01,0), 3=(0.02,0),
                         4=(0,−0.01), 5=(0.02,−0.01)
    """
    graph = RoadGraph()

    # Node coordinates
    graph.node_coords = {
        1: (0.0, 0.0),
        2: (0.01, 0.0),
        3: (0.02, 0.0),
        4: (0.0, -0.01),
        5: (0.02, -0.01),
    }

    edges = [
        _make_edge(0, 1, 2, [(0.0, 0.0), (0.01, 0.0)], fc="motorway", fc_score=1.0, ref="A1"),
        _make_edge(1, 2, 3, [(0.01, 0.0), (0.02, 0.0)], fc="motorway", fc_score=1.0, ref="A1"),
        _make_edge(2, 1, 4, [(0.0, 0.0), (0.0, -0.01)], fc="primary", fc_score=0.75),
        _make_edge(3, 3, 5, [(0.02, 0.0), (0.02, -0.01)], fc="secondary", fc_score=0.60),
        _make_edge(4, 4, 5, [(0.0, -0.01), (0.02, -0.01)], fc="tertiary", fc_score=0.45),
    ]

    for edge in edges:
        graph.add_edge(edge)

    return graph


def _make_cities_geojson(path: str) -> None:
    """Write a small cities GeoJSON to the given path."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "BigCity", "population": 500000, "place": "city"},
                "geometry": {"type": "Point", "coordinates": [0.005, -0.002]},
            },
            {
                "type": "Feature",
                "properties": {"name": "SmallTown", "population": 15000, "place": "town"},
                "geometry": {"type": "Point", "coordinates": [0.015, -0.005]},
            },
            {
                "type": "Feature",
                "properties": {"name": "Village", "population": 800, "place": "village"},
                "geometry": {"type": "Point", "coordinates": [0.01, -0.008]},
            },
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)


# ===========================================================================
# Test classes
# ===========================================================================


class TestFCScoring:
    """Tests for functional class scoring functions."""

    def test_fc_scores_motorway(self):
        """Motorway has highest base score."""
        assert FC_SCORES["motorway"] == 1.0

    def test_fc_scores_path(self):
        """Path has lowest base score."""
        assert FC_SCORES["path"] == 0.02

    def test_fc_scores_all_keys(self):
        """All expected functional classes present."""
        expected = {
            "motorway",
            "trunk",
            "primary",
            "secondary",
            "tertiary",
            "unclassified",
            "residential",
            "service",
            "track",
            "path",
        }
        assert set(FC_SCORES.keys()) == expected

    def test_compute_fc_score_base(self):
        """Base score without modifiers."""
        assert _compute_fc_score("primary", "", False, False, False, False) == 0.75

    def test_compute_fc_score_ref_bonus(self):
        """Ref tag adds 0.05 bonus."""
        base = _compute_fc_score("primary", "", False, False, False, False)
        with_ref = _compute_fc_score("primary", "M1", False, False, False, False)
        assert with_ref == pytest.approx(base + 0.05)

    def test_compute_fc_score_bridge_bonus(self):
        """Bridge adds 0.03 bonus."""
        base = _compute_fc_score("tertiary", "", False, False, False, False)
        with_bridge = _compute_fc_score("tertiary", "", True, False, False, False)
        assert with_bridge == pytest.approx(base + 0.03)

    def test_compute_fc_score_unpaved_penalty(self):
        """Unpaved surface subtracts 0.10."""
        base = _compute_fc_score("secondary", "", False, False, False, False)
        unpaved = _compute_fc_score("secondary", "", False, False, True, False)
        assert unpaved == pytest.approx(base - 0.10)

    def test_compute_fc_score_restricted_penalty(self):
        """Restricted access subtracts 0.15."""
        base = _compute_fc_score("secondary", "", False, False, False, False)
        restricted = _compute_fc_score("secondary", "", False, False, False, True)
        assert restricted == pytest.approx(base - 0.15)

    def test_compute_fc_score_clamp_to_zero(self):
        """Score cannot go below 0.0."""
        # track base is 0.04, minus 0.10 unpaved, minus 0.15 restricted → negative
        score = _compute_fc_score("track", "", False, False, True, True)
        assert score == 0.0

    def test_classify_fc_primary(self):
        """Primary highway maps to 'primary' FC."""
        assert _classify_fc("primary") == "primary"

    def test_classify_fc_motorway_link(self):
        """Motorway link maps to 'motorway' FC."""
        assert _classify_fc("motorway_link") == "motorway"

    def test_classify_fc_unknown(self):
        """Unknown highway tag returns empty string."""
        assert _classify_fc("bus_guideway") == ""


class TestHaversineDistance:
    """Tests for haversine distance functions."""

    def test_haversine_zero_distance(self):
        """Same point yields zero distance."""
        d = _haversine_m(0.0, 0.0, 0.0, 0.0)
        assert d == pytest.approx(0.0, abs=0.01)

    def test_haversine_known_distance(self):
        """Known distance: 1 degree of latitude ≈ 111 km."""
        d = _haversine_m(0.0, 0.0, 0.0, 1.0)
        assert 110_000 < d < 112_000

    def test_haversine_symmetry(self):
        """Distance is symmetric."""
        d1 = _haversine_m(10.0, 45.0, 11.0, 46.0)
        d2 = _haversine_m(11.0, 46.0, 10.0, 45.0)
        assert d1 == pytest.approx(d2, rel=1e-10)

    def test_polyline_length_single_segment(self):
        """Single-segment polyline returns haversine of the two points."""
        coords = [(0.0, 0.0), (0.0, 1.0)]
        length = _polyline_length_m(coords)
        expected = _haversine_m(0.0, 0.0, 0.0, 1.0)
        assert length == pytest.approx(expected)

    def test_polyline_length_empty(self):
        """Empty / single-point polyline returns 0."""
        assert _polyline_length_m([]) == 0.0
        assert _polyline_length_m([(0.0, 0.0)]) == 0.0


class TestLogicalEdge:
    """Tests for LogicalEdge dataclass."""

    def test_construction(self):
        """LogicalEdge can be constructed with all fields."""
        edge = _make_edge(42, 1, 2, [(0.0, 0.0), (1.0, 1.0)])
        assert edge.edge_id == 42
        assert edge.from_node == 1
        assert edge.to_node == 2

    def test_field_types(self):
        """All fields have correct types."""
        edge = _make_edge(
            0,
            1,
            2,
            [(0.0, 0.0), (1.0, 1.0)],
            bridge=True,
            tunnel=False,
            oneway=True,
            surface_unpaved=True,
        )
        assert isinstance(edge.bridge, bool)
        assert isinstance(edge.tunnel, bool)
        assert isinstance(edge.oneway, bool)
        assert isinstance(edge.surface_unpaved, bool)
        assert isinstance(edge.osm_way_ids, list)
        assert isinstance(edge.coords, list)


class TestRoadGraph:
    """Tests for RoadGraph class."""

    def test_empty_graph(self):
        """Empty graph has no edges or nodes."""
        graph = RoadGraph()
        assert len(graph.edges) == 0
        assert len(graph.node_coords) == 0

    def test_add_edge(self):
        """Adding an edge registers it in edges, edge_by_id, and adj."""
        graph = RoadGraph()
        edge = _make_edge(0, 1, 2, [(0.0, 0.0), (1.0, 1.0)])
        graph.add_edge(edge)
        assert len(graph.edges) == 1
        assert 0 in graph.edge_by_id
        assert 0 in graph.adj[1]
        assert 0 in graph.adj[2]

    def test_neighbors(self):
        """Neighbors returns reachable node IDs."""
        graph = _make_test_graph()
        nbrs = graph.neighbors(2)
        assert set(nbrs) == {1, 3}

    def test_edges_of(self):
        """edges_of returns all incident edges."""
        graph = _make_test_graph()
        edges = graph.edges_of(1)
        edge_ids = {e.edge_id for e in edges}
        assert edge_ids == {0, 2}

    def test_shortest_path_simple(self):
        """Shortest path between adjacent nodes."""
        graph = _make_test_graph()
        path = graph.shortest_path(1, 3)
        # Should go 1→2→3 (edges 0, 1)
        assert path == [0, 1]

    def test_shortest_path_no_path(self):
        """No path returns empty list for disconnected nodes."""
        graph = RoadGraph()
        graph.node_coords[1] = (0.0, 0.0)
        graph.node_coords[2] = (1.0, 1.0)
        edge = _make_edge(0, 1, 1, [(0.0, 0.0), (0.0, 0.0)])  # self-loop
        graph.add_edge(edge)
        # Node 2 is disconnected
        path = graph.shortest_path(1, 2)
        assert path == []

    def test_shortest_path_same_node(self):
        """Same source and destination returns empty list."""
        graph = _make_test_graph()
        path = graph.shortest_path(1, 1)
        assert path == []

    def test_save_load_roundtrip(self):
        """Save and load produces equivalent graph."""
        graph = _make_test_graph()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            graph.save(temp_path)
            loaded = RoadGraph.load(temp_path)

            assert len(loaded.edges) == len(graph.edges)
            assert set(loaded.node_coords.keys()) == set(graph.node_coords.keys())

            for orig, restored in zip(graph.edges, loaded.edges):
                assert orig.edge_id == restored.edge_id
                assert orig.from_node == restored.from_node
                assert orig.to_node == restored.to_node
                assert orig.fc == restored.fc
                assert orig.length_m == pytest.approx(restored.length_m, abs=0.2)
        finally:
            os.unlink(temp_path)


class TestSegmentIndex:
    """Tests for SegmentIndex spatial lookup."""

    def test_snap_route_exact(self):
        """Route coords exactly on an edge snap to that edge."""
        graph = _make_test_graph()
        idx = SegmentIndex(graph)
        # Points along edge 0: (0,0)→(0.01,0)
        matched = idx.snap_route([[0.0, 0.0], [0.005, 0.0], [0.01, 0.0]])
        assert 0 in matched

    def test_snap_route_nearby(self):
        """Route coords near an edge snap within tolerance."""
        graph = _make_test_graph()
        idx = SegmentIndex(graph)
        # Slightly off edge 0 (0.00001 deg ≈ 1m)
        matched = idx.snap_route([[0.005, 0.00001]])
        assert 0 in matched

    def test_snap_route_far(self):
        """Route coords far from any edge return empty set."""
        graph = _make_test_graph()
        idx = SegmentIndex(graph)
        # Far away point
        matched = idx.snap_route([[10.0, 10.0]])
        assert len(matched) == 0

    def test_snap_route_multi_edge(self):
        """Route spanning multiple edges returns multiple edge IDs."""
        graph = _make_test_graph()
        idx = SegmentIndex(graph)
        # Along edge 0 then edge 1: (0,0)→(0.01,0)→(0.02,0)
        matched = idx.snap_route(
            [
                [0.0, 0.0],
                [0.005, 0.0],
                [0.01, 0.0],
                [0.015, 0.0],
                [0.02, 0.0],
            ]
        )
        assert 0 in matched
        assert 1 in matched


class TestAnchorsAndPairs:
    """Tests for anchor building and OD pair sampling."""

    def test_build_anchors_with_cities(self):
        """build_anchors returns anchor node IDs from cities GeoJSON."""
        graph = _make_test_graph()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            temp_path = f.name
            _make_cities_geojson(temp_path)

        try:
            # Zoom 2 requires pop >= 500k → only BigCity qualifies
            anchors = build_anchors(graph, temp_path, 2)
            assert len(anchors) >= 1
            # All anchors are valid node IDs
            for nid in anchors:
                assert nid in graph.node_coords
        finally:
            os.unlink(temp_path)

    def test_build_anchors_fallback_no_cities(self):
        """build_anchors falls back to high-degree nodes with no cities file."""
        graph = _make_test_graph()
        anchors = build_anchors(graph, "/nonexistent/cities.geojson", 7)
        # Should still return some anchors via degree fallback
        assert isinstance(anchors, list)

    def test_sample_od_pairs_deterministic(self):
        """sample_od_pairs is deterministic (seeded RNG)."""
        graph = _make_test_graph()
        anchors = list(graph.node_coords.keys())

        pairs1 = sample_od_pairs(anchors, 7, graph, k_pairs=10)
        pairs2 = sample_od_pairs(anchors, 7, graph, k_pairs=10)
        assert pairs1 == pairs2

    def test_sample_od_pairs_min_distance(self):
        """All OD pairs satisfy the minimum distance threshold."""
        graph = _make_test_graph()
        anchors = list(graph.node_coords.keys())
        # Zoom 7: min distance 5 km. Our graph is ~1 km, so no valid pairs.
        pairs = sample_od_pairs(anchors, 7, graph)
        for a, b in pairs:
            alon, alat = graph.node_coords[a]
            blon, blat = graph.node_coords[b]
            d_km = _haversine_m(alon, alat, blon, blat) / 1000.0
            assert d_km >= 5.0

    def test_sample_od_pairs_limit(self):
        """OD pairs are limited by k_pairs."""
        graph = _make_test_graph()
        anchors = list(graph.node_coords.keys())
        pairs = sample_od_pairs(anchors, 7, graph, k_pairs=2)
        assert len(pairs) <= 2


class TestSBSNormalization:
    """Tests for SBS normalization functions."""

    def test_normalize_sbs_empty(self):
        """Empty betweenness returns empty dict."""
        assert normalize_sbs({}) == {}

    def test_normalize_sbs_single(self):
        """Single edge normalizes to 1.0."""
        result = normalize_sbs({0: 10})
        assert result[0] == pytest.approx(1.0)

    def test_normalize_sbs_clamp(self):
        """All values are between 0.0 and 1.0 (inclusive)."""
        bc = {0: 100, 1: 50, 2: 1, 3: 200}
        result = normalize_sbs(bc)
        for v in result.values():
            assert 0.0 <= v <= 1.0

    def test_accumulate_votes(self):
        """accumulate_votes increments edge counters from routes."""
        graph = _make_test_graph()
        idx = SegmentIndex(graph)
        # Two routes both traversing edge 0
        routes = {
            (1, 3): [[0.0, 0.0], [0.005, 0.0], [0.01, 0.0]],
            (1, 5): [[0.0, 0.0], [0.005, 0.0], [0.01, 0.0]],
        }
        bc = accumulate_votes(routes, idx)
        assert bc.get(0, 0) >= 2


class TestScoreComputation:
    """Tests for per-zoom score computation."""

    def test_compute_scores_basic(self):
        """compute_scores produces scores for all zoom levels."""
        graph = _make_test_graph()
        sbs = {z: {e.edge_id: 0.5 for e in graph.edges} for z in range(2, 8)}
        scores = compute_scores(graph, sbs)
        assert set(scores.keys()) == {2, 3, 4, 5, 6, 7}
        for z in range(2, 8):
            assert len(scores[z]) == len(graph.edges)

    def test_compute_scores_bypass_boost(self):
        """Bypass edges receive a score boost."""
        graph = _make_test_graph()
        sbs = {z: {e.edge_id: 0.5 for e in graph.edges} for z in range(2, 8)}
        no_flags = compute_scores(graph, sbs)
        bypass = compute_scores(graph, sbs, bypass_flags={0: "bypass"})
        # Edge 0 should have higher score with bypass flag
        for z in range(2, 8):
            assert bypass[z][0] > no_flags[z][0]

    def test_compute_scores_ring_boost(self):
        """Ring edges receive a score boost."""
        graph = _make_test_graph()
        sbs = {z: {e.edge_id: 0.5 for e in graph.edges} for z in range(2, 8)}
        no_flags = compute_scores(graph, sbs)
        ring = compute_scores(graph, sbs, ring_flags={0: True})
        for z in range(2, 8):
            assert ring[z][0] > no_flags[z][0]

    def test_compute_scores_clamp(self):
        """Scores are clamped between 0.0 and 1.2."""
        graph = _make_test_graph()
        # Very high SBS to test upper clamp
        sbs = {z: {e.edge_id: 10.0 for e in graph.edges} for z in range(2, 8)}
        scores = compute_scores(graph, sbs)
        for z in range(2, 8):
            for s in scores[z].values():
                assert 0.0 <= s <= 1.2


class TestCellBudgets:
    """Tests for cell budget computation."""

    def test_flat_budgets_fallback(self):
        """_flat_budgets returns a single 'flat' cell per zoom."""
        graph = _make_test_graph()
        budgets = _flat_budgets(graph)
        for z in range(2, 8):
            assert "flat" in budgets[z]
            assert budgets[z]["flat"]["budget_km"] == BASE_KM[z]

    def test_base_km_values(self):
        """BASE_KM covers zoom 2–7 and increases monotonically."""
        for z in range(2, 8):
            assert z in BASE_KM
        for z in range(2, 7):
            assert BASE_KM[z] < BASE_KM[z + 1]


class TestMonotonicReveal:
    """Tests for monotonic zoom reveal enforcement."""

    def test_enforce_monotonic_basic(self):
        """Lower-zoom selections propagate to higher zooms."""
        selected = {
            2: {0, 1},
            3: {2},
            4: {3},
            5: set(),
            6: set(),
            7: {4},
        }
        assignments = enforce_monotonic_reveal(selected)
        # Edge 0 first appears at z2
        assert assignments[0] == 2
        # Edge 1 first appears at z2
        assert assignments[1] == 2
        # Edge 2 first appears at z3
        assert assignments[2] == 3

    def test_enforce_monotonic_min_zoom(self):
        """Each edge gets the minimum zoom where it appears."""
        selected = {
            2: set(),
            3: {0},
            4: {0, 1},
            5: {0, 1, 2},
            6: set(),
            7: set(),
        }
        assignments = enforce_monotonic_reveal(selected)
        assert assignments[0] == 3
        assert assignments[1] == 4
        assert assignments[2] == 5

    def test_enforce_monotonic_empty(self):
        """All empty selections produce no assignments."""
        selected = {z: set() for z in range(2, 8)}
        assignments = enforce_monotonic_reveal(selected)
        assert len(assignments) == 0


class TestBypassDetectionHelpers:
    """Tests for bypass/ring detection helper functions."""

    def test_classify_settlement_city(self):
        """Large population classifies as city."""
        assert _classify_settlement("city", 200_000) == "city"
        assert _classify_settlement("", 100_000) == "city"

    def test_classify_settlement_town(self):
        """Medium population classifies as town."""
        assert _classify_settlement("town", 30_000) == "town"
        assert _classify_settlement("", 10_000) == "town"

    def test_classify_settlement_village(self):
        """Small population classifies as village."""
        assert _classify_settlement("village", 500) == "village"
        assert _classify_settlement("", 1_000) == "village"

    def test_load_settlements_valid(self):
        """_load_settlements parses GeoJSON correctly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            temp_path = f.name
            _make_cities_geojson(temp_path)

        try:
            settlements = _load_settlements(temp_path)
            assert len(settlements) == 3
            names = [s[0] for s in settlements]
            assert "BigCity" in names
            assert "SmallTown" in names
            assert "Village" in names
        finally:
            os.unlink(temp_path)

    def test_load_settlements_bad_file(self):
        """_load_settlements returns empty list for missing file."""
        settlements = _load_settlements("/nonexistent/path.geojson")
        assert settlements == []


class TestRingDetectionHelpers:
    """Tests for ring detection helper functions."""

    def test_find_radial_entries(self):
        """_find_radial_entries returns node IDs near boundary radius."""
        graph = _make_test_graph()
        # Center at (0.01, -0.005), radius that should encompass some nodes
        nodes = _find_radial_entries(graph, 0.01, -0.005, 2000.0, 8)
        assert isinstance(nodes, list)
        for nid in nodes:
            assert nid in graph.node_coords

    def test_sample_radial_pairs(self):
        """_sample_radial_pairs returns non-adjacent pairs."""
        nodes = [1, 2, 3, 4, 5, 6, 7, 8]
        pairs = _sample_radial_pairs(nodes, 20)
        assert isinstance(pairs, list)
        for a, b in pairs:
            assert a != b
            # Not directly adjacent in circular order
            idx_a = nodes.index(a)
            idx_b = nodes.index(b)
            assert abs(idx_a - idx_b) >= 2


class TestExport:
    """Tests for export helper functions."""

    def test_export_zoom_geojson(self):
        """_export_zoom_geojson writes valid GeoJSON."""
        graph = _make_test_graph()
        assignments = {0: 2, 1: 3, 2: 4, 3: 5, 4: 7}

        with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as f:
            temp_path = f.name

        try:
            _export_zoom_geojson(graph, assignments, 4, temp_path)
            with open(temp_path, encoding="utf-8") as f:
                geojson = json.load(f)

            assert geojson["type"] == "FeatureCollection"
            # At z4, should include edges with minZoom <= 4 (edges 0, 1, 2)
            assert len(geojson["features"]) == 3
            for feat in geojson["features"]:
                assert feat["geometry"]["type"] == "LineString"
                assert "min_zoom" in feat["properties"]
        finally:
            os.unlink(temp_path)

    def test_empty_result_structure(self):
        """_empty_result has all expected keys."""
        result = _empty_result("/tmp/test")
        assert result["output_dir"] == "/tmp/test"
        assert result["total_logical_edges"] == 0
        assert result["selected_edges"] == 0
        assert result["csv_path"] == ""
        assert "extraction_date" in result
        assert result["format"] == "CSV+GeoJSON+JSONL"

    def test_empty_metrics_structure(self):
        """_empty_metrics has all expected keys."""
        metrics = _empty_metrics()
        assert metrics["total_logical_edges"] == 0
        assert metrics["selected_edges"] == 0
        assert metrics["pruned_edges"] == 0
        assert metrics["backbone_edges"] == 0
        assert metrics["bypass_edges"] == 0
        assert metrics["ring_edges"] == 0
        assert metrics["processing_seconds"] == 0.0
        assert metrics["route_count"] == 0


class TestHandlerFactories:
    """Tests for handler factory functions returning empty on missing deps."""

    def test_build_logical_graph_no_osmium(self):
        """BuildLogicalGraph returns empty when no osmium."""
        handler = _make_build_logical_graph_handler("BuildLogicalGraph")
        with patch("osm_geocoder.handlers.roads.zoom_handlers.HAS_OSMIUM", False):
            result = handler({"cache": {"path": "/tmp/test.pbf"}})
            assert result["edge_count"] == 0
            assert result["graph_path"] == ""

    def test_build_logical_graph_no_path(self):
        """BuildLogicalGraph returns empty when no path."""
        handler = _make_build_logical_graph_handler("BuildLogicalGraph")
        result = handler({"cache": {}})
        assert result["edge_count"] == 0

    def test_build_anchors_no_path(self):
        """BuildAnchors returns empty when no graph_path."""
        handler = _make_build_anchors_handler("BuildAnchors")
        result = handler({})
        assert result["anchor_count"] == 0
        assert result["anchors_path"] == ""

    def test_compute_sbs_no_path(self):
        """ComputeSBS returns empty when no graph_path."""
        handler = _make_compute_sbs_handler("ComputeSBS")
        result = handler({})
        assert result["route_count"] == 0
        assert result["sbs_path"] == ""

    def test_compute_scores_no_path(self):
        """ComputeScores returns empty when no graph_path."""
        handler = _make_compute_scores_handler("ComputeScores")
        result = handler({})
        assert result["scores_path"] == ""

    def test_detect_bypasses_no_path(self):
        """DetectBypasses returns empty when no graph_path."""
        handler = _make_detect_bypasses_handler("DetectBypasses")
        result = handler({})
        assert result["bypass_count"] == 0
        assert result["bypasses_path"] == ""

    def test_detect_rings_no_path(self):
        """DetectRings returns empty when no graph_path."""
        handler = _make_detect_rings_handler("DetectRings")
        result = handler({})
        assert result["ring_count"] == 0
        assert result["rings_path"] == ""

    def test_select_edges_no_path(self):
        """SelectEdges returns empty when no graph_path."""
        handler = _make_select_edges_handler("SelectEdges")
        result = handler({})
        assert result["selected_count"] == 0

    def test_export_zoom_layers_no_path(self):
        """ExportZoomLayers returns empty when no paths."""
        handler = _make_export_zoom_layers_handler("ExportZoomLayers")
        result = handler({})
        assert result["result"]["total_logical_edges"] == 0

    def test_build_zoom_layers_no_osmium(self):
        """BuildZoomLayers returns empty result when no osmium."""
        handler = _make_build_zoom_layers_handler("BuildZoomLayers")
        with patch("osm_geocoder.handlers.roads.zoom_builder.HAS_OSMIUM", False):
            result = handler({"cache": {"path": "/tmp/test.pbf"}, "graph": {}})
            assert result["result"]["total_logical_edges"] == 0
            assert result["metrics"]["total_logical_edges"] == 0


class TestHandlerRegistration:
    """Tests for handler registration and facet catalog."""

    def test_zoom_facets_count(self):
        """Expected number of zoom facets."""
        assert len(ZOOM_FACETS) == 9

    def test_zoom_facets_names(self):
        """All expected facet names are present."""
        names = [name for name, _ in ZOOM_FACETS]
        assert "BuildLogicalGraph" in names
        assert "BuildAnchors" in names
        assert "ComputeSBS" in names
        assert "ComputeScores" in names
        assert "DetectBypasses" in names
        assert "DetectRings" in names
        assert "SelectEdges" in names
        assert "ExportZoomLayers" in names
        assert "BuildZoomLayers" in names

    def test_register_zoom_handlers(self, monkeypatch):
        """register_zoom_handlers registers 9 handlers with mock poller."""
        monkeypatch.setitem(register_zoom_handlers.__globals__, "HAS_OSMIUM", True)
        poller = MagicMock()
        register_zoom_handlers(poller)
        assert poller.register.call_count == 9

        call_args = [call[0][0] for call in poller.register.call_args_list]
        assert f"{NAMESPACE}.BuildLogicalGraph" in call_args
        assert f"{NAMESPACE}.BuildZoomLayers" in call_args
        assert f"{NAMESPACE}.ComputeSBS" in call_args

    def test_namespace_value(self):
        """Namespace matches FFL namespace."""
        assert NAMESPACE == "osm.Roads.ZoomBuilder"
