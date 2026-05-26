"""Phase 1 tests for osm.Network.BuildNetwork — the noding core + cache write.

Deterministic synthetic geometry (no PBF, no network): a few hand-placed
LineStrings whose topology we know exactly, plus an end-to-end cache write to a
tmp AFL_DATA_ROOT.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")
pytest.importorskip("networkx")

from osm_geocoder.handlers.network import network_ops as ops


def _line(coords, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": coords}}


# --- the pure noding core ------------------------------------------------------


def test_x_crossing_nodes_at_intersection():
    # Two lines crossing at (0,0): each splits into two → 4 edges, 5 nodes.
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5"),
        _line([[0, -1], [0, 1]], ref="I 80"),
    ]
    g = ops.node_linestrings(feats, snap_tolerance_m=25.0)
    assert g.node_count == 5
    assert g.edge_count == 4
    assert g.connected_components == 1
    assert g.largest_component_frac == pytest.approx(1.0)
    # adjacency references valid edge indices with positive lengths
    for adj in g.adjacency.values():
        for _nbr, length_m, edge_idx in adj:
            assert 0 <= edge_idx < g.edge_count
            assert length_m > 0


def test_disjoint_lines_two_components():
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5"),
        _line([[-1, 5], [1, 5]], ref="I 90"),  # far north, no intersection
    ]
    g = ops.node_linestrings(feats, snap_tolerance_m=25.0)
    assert g.connected_components == 2
    assert g.node_count == 4
    assert g.edge_count == 2
    assert g.largest_component_frac == pytest.approx(0.5)


def test_t_junction_is_connected():
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5"),
        _line([[0, 0], [0, 1]], ref="I 80"),  # starts on the middle of the first
    ]
    g = ops.node_linestrings(feats, snap_tolerance_m=25.0)
    assert g.connected_components == 1
    assert g.node_count == 4   # (-1,0) (1,0) (0,1) and the shared (0,0)
    assert g.edge_count == 3


def test_ref_filter_keeps_only_matching_prefix():
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5"),
        _line([[2, 0], [3, 0]], ref="US 101"),
    ]
    g = ops.node_linestrings(feats, snap_tolerance_m=25.0, ref_filter="I ")
    assert g.edge_count == 1
    assert all(e["ref"].startswith("I ") for e in g.edges)


def test_edge_length_is_geodesic_meters():
    # One degree of latitude is ~110.6-111.2 km depending on geodesic vs haversine.
    g = ops.node_linestrings([_line([[0, 0], [0, 1]], ref="I 5")], snap_tolerance_m=25.0)
    assert g.edge_count == 1
    length_m = g.edges[0]["length_m"]
    assert 110_000 <= length_m <= 111_500


def test_ref_name_recovered_onto_noded_segments():
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5", name="Golden State Fwy"),
        _line([[0, -1], [0, 1]], ref="I 80", name="Eastshore Fwy"),
    ]
    g = ops.node_linestrings(feats, snap_tolerance_m=25.0)
    refs = {e["ref"] for e in g.edges}
    assert refs == {"I 5", "I 80"}


# --- the cache write (end-to-end, tmp AFL_DATA_ROOT) ---------------------------


def _write_input(tmp_path: Path) -> str:
    feats = [
        _line([[-1, 0], [1, 0]], ref="I 5"),
        _line([[0, -1], [0, 1]], ref="I 80"),
    ]
    p = tmp_path / "in.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(p)


def test_build_network_writes_directory_artifact_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path / "data"))
    src = _write_input(tmp_path)

    res = ops.build_network(src, snap_tolerance_m=25.0)

    assert res.node_count == 5
    assert res.edge_count == 4
    assert res.connected_components == 1
    net = Path(res.network_path)
    assert net.is_dir()
    for name in ("nodes.geojson", "edges.geojson", "graph.json"):
        assert (net / name).exists(), name

    # graph.json is the authoritative adjacency: string keys, symmetric.
    graph = json.loads((net / "graph.json").read_text())
    assert len(graph) == 5
    assert all(isinstance(k, str) for k in graph)
    nodes = json.loads((net / "nodes.geojson").read_text())["features"]
    edges = json.loads((net / "edges.geojson").read_text())["features"]
    assert len(nodes) == 5
    assert len(edges) == 4

    # sidecar carries the build-quality metrics + content-addressed lineage.
    side = json.loads((net.parent / (net.name + ".meta.json")).read_text())
    assert side["kind"] == "directory"
    assert side["extra"]["node_count"] == 5
    assert side["extra"]["edge_count"] == 4
    assert side["extra"]["connected_components"] == 1
    assert side["source"]["sha256"]


def test_build_network_is_idempotent_cache_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path / "data"))
    src = _write_input(tmp_path)

    first = ops.build_network(src, snap_tolerance_m=25.0)
    sidecar_file = Path(first.network_path).parent / (Path(first.network_path).name + ".meta.json")
    generated_first = json.loads(sidecar_file.read_text())["generated_at"]

    second = ops.build_network(src, snap_tolerance_m=25.0)
    generated_second = json.loads(sidecar_file.read_text())["generated_at"]

    assert second.network_path == first.network_path
    assert second.node_count == first.node_count
    # A cache hit does not rewrite the artifact.
    assert generated_second == generated_first


def test_distinct_tolerances_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path / "data"))
    src = _write_input(tmp_path)

    a = ops.build_network(src, snap_tolerance_m=25.0)
    b = ops.build_network(src, snap_tolerance_m=100.0)
    assert a.network_path != b.network_path
    assert Path(a.network_path).is_dir() and Path(b.network_path).is_dir()


def test_no_isolated_nodes_and_consistent_component_count(tmp_path, monkeypatch):
    """The isolated-node fix: nodes.geojson, graph.json and the sidecar agree."""
    import networkx as nx

    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path / "data"))
    src = _write_input(tmp_path)
    res = ops.build_network(src, snap_tolerance_m=25.0)
    net = Path(res.network_path)

    graph = json.loads((net / "graph.json").read_text())
    node_ids = {f["properties"]["node_id"] for f in
                json.loads((net / "nodes.geojson").read_text())["features"]}
    # every persisted node participates in the adjacency — no isolated nodes
    assert node_ids == {int(k) for k in graph}

    g = nx.Graph()
    for k, adj in graph.items():
        for nbr, _length, _idx in adj:
            g.add_edge(int(k), int(nbr))
    side = json.loads((net.parent / (net.name + ".meta.json")).read_text())
    assert side["extra"]["connected_components"] == nx.number_connected_components(g)
