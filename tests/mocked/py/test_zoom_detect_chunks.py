"""Bypass / ring detection as chunk tasks must give the one-task result.

Step 4 was one loop: three routes per entry/exit pair of every settlement and
200 per large city, one request after another -- 1.6 h + over an hour on the US
build, and 3.4x that with the router across WiFi (2026-10-15). Split into a
planner (geometry) and chunk tasks (routing), the merged flags must be EXACTLY
what the loop produces, and a kind missing any chunk must not be merged.
"""

from __future__ import annotations

import heapq
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_builder as zb  # noqa: E402
from osm_geocoder.handlers.roads import zoom_detection as zd  # noqa: E402
from osm_geocoder.handlers.roads import zoom_sbs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph  # noqa: E402

STEP = 0.01
N = 41  # 41 x 41 nodes, 0.4 deg square
SPEED = {"motorway": 30.0, "primary": 20.0, "tertiary": 9.0}
FC = {"motorway": 1.0, "primary": 0.75, "tertiary": 0.45}


def _grid() -> RoadGraph:
    """A street grid: tertiary everywhere, primary every 5th line, and a
    motorway square 0.12 deg around the centre (a ring / bypass)."""
    g = RoadGraph()
    nid = lambda i, j: i * N + j  # noqa: E731
    for i in range(N):
        for j in range(N):
            g.node_coords[nid(i, j)] = (-120.2 + i * STEP, 40.0 + j * STEP)
    eid = 0
    c = N // 2
    for i in range(N):
        for j in range(N):
            for di, dj in ((1, 0), (0, 1)):
                a, b = (i, j), (i + di, j + dj)
                if b[0] >= N or b[1] >= N:
                    continue
                on_ring = (max(abs(a[0] - c), abs(a[1] - c)) == 12
                           and max(abs(b[0] - c), abs(b[1] - c)) == 12)
                line = i if di == 0 else j
                fc = "motorway" if on_ring else ("primary" if line % 5 == 0 else "tertiary")
                ca, cb = g.node_coords[nid(*a)], g.node_coords[nid(*b)]
                length = zd._haversine_m(ca[0], ca[1], cb[0], cb[1])
                g.add_edge(LogicalEdge(
                    edge_id=1000 + eid, from_node=nid(*a), to_node=nid(*b), osm_way_ids=[eid],
                    coords=[ca, cb], length_m=length, fc=fc, fc_score=FC[fc], ref="", name="",
                    maxspeed=50, lanes=2, bridge=False, tunnel=False, oneway=False,
                    surface_unpaved=False))
                eid += 1
    return g


def _router(g: RoadGraph):
    adj: dict[int, list] = {}
    for e in g.edges:
        t = e.length_m / SPEED[e.fc]
        adj.setdefault(e.from_node, []).append((e.to_node, t))
        adj.setdefault(e.to_node, []).append((e.from_node, t))

    def route(a, b, node_coords, graph_dir, profile):
        dist, prev, heap = {a: 0.0}, {}, [(0.0, a)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == b:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, w in adj.get(u, ()):
                if d + w < dist.get(v, math.inf):
                    dist[v], prev[v] = d + w, u
                    heapq.heappush(heap, (d + w, v))
        if b not in dist:
            return [], 0.0
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return [list(g.node_coords[n]) for n in reversed(path)], dist[b] * 1000

    return route


def _cities(tmp: Path) -> str:
    feats = [("Big", -120.0, 40.2, 400_000, "city"), ("Mid", -120.1, 40.1, 120_000, "city"),
             ("Town", -119.9, 40.3, 15_000, "town"), ("Vil", -120.05, 40.25, 900, "village"),
             ("Edge", -119.85, 40.05, 30_000, "town")]
    p = tmp / "cities.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": n, "population": pop, "place": pt},
         "geometry": {"type": "Point", "coordinates": [lon, lat]}}
        for n, lon, lat, pop, pt in feats]}))
    return str(p)


@pytest.fixture
def world(monkeypatch, tmp_path):
    g = _grid()
    monkeypatch.setattr(zd, "_route_pair_with_time", _router(g))
    monkeypatch.setattr(zd, "HAS_REQUESTS", True)
    monkeypatch.setattr(zb, "HAS_REQUESTS", True)
    ctx = SimpleNamespace(cities_path=_cities(tmp_path), road_graph=g, pbf_path="x.pbf",
                          graph_dir="gd", profile="car")
    routing = tmp_path / "routing"
    (routing / "chunks").mkdir(parents=True)
    index = zoom_sbs.SegmentIndex(g)
    index.save(str(routing / "index"))
    zb.np_save_edge_fc(index, g, str(routing / "index"))
    return ctx, routing


def _run_chunks(ctx, routing, detect_pairs):
    paths, summary = zb.plan_detection(ctx, routing, detect_pairs)
    docs = [zb.detect_zoom_chunk(json.loads((routing.parent / p).read_text()),
                                 str(routing / "index")) for p in paths]
    return paths, summary, docs


@pytest.mark.parametrize("detect_pairs", [1, 7, 10_000])
def test_merged_chunks_equal_the_one_task_detection(world, detect_pairs):
    ctx, routing = world
    g = ctx.road_graph
    serial_b = zd.detect_bypasses(g, ctx.cities_path, "gd", "car")
    serial_r = zd.detect_rings(g, ctx.cities_path, "gd", "car")
    assert serial_b and serial_r, "the fixture must exercise both detectors"

    _paths, summary, docs = _run_chunks(ctx, routing, detect_pairs)
    key = summary["key"]
    merged_b, _stats = zb._merge_detection(docs, "bypass", key)
    merged_r, _ = zb._merge_detection(docs, "ring", key)
    assert merged_b == serial_b
    assert list(merged_b) == list(serial_b), "first-wins order must match too"
    assert merged_r == serial_r


def test_a_kind_missing_a_chunk_is_not_merged(world):
    ctx, routing = world
    _paths, summary, docs = _run_chunks(ctx, routing, 1)
    assert summary["bypass"]["chunks"] > 1
    docs = [d for d in docs if not (d["kind"] == "bypass" and d["index"] == 1)]
    assert zb._merge_detection(docs, "bypass", summary["key"]) is None
    assert zb._merge_detection(docs, "ring", summary["key"]) is not None


def test_results_from_another_plan_are_never_merged(world):
    ctx, routing = world
    _paths, _summary, docs = _run_chunks(ctx, routing, 10_000)
    assert zb._merge_detection(docs, "bypass", "another-key") is None


def test_the_key_changes_with_the_settlements(world):
    ctx, _routing = world
    s = zd._load_settlements(ctx.cities_path)
    assert zb._detect_key(ctx, s) != zb._detect_key(ctx, s[:-1])


def test_radial_entries_with_the_index_match_the_full_scan(world):
    ctx, _routing = world
    g = ctx.road_graph
    idx = zd._BoundaryEdgeIndex(g)
    for lon, lat, r in ((-120.0, 40.2, 13_000.0), (-120.1, 40.1, 9_000.0),
                        (-119.85, 40.05, 25_000.0)):
        assert zd._find_radial_entries(g, lon, lat, r, 16, index=idx) == \
            zd._find_radial_entries(g, lon, lat, r, 16)


def test_edge_geometry_and_class_come_back_from_the_saved_index(world):
    ctx, routing = world
    import numpy as np

    idx = zoom_sbs.SegmentIndex.load(str(routing / "index"))
    fc = np.load(routing / "index" / zb.EDGE_FC_FILE)
    for e in ctx.road_graph.edges[::97]:
        assert idx.edge_coords(e.edge_id) == [tuple(c) for c in e.coords]
        assert float(fc[idx.row_of(e.edge_id)]) == pytest.approx(e.fc_score)
    assert idx.row_of(-5) == -1 and idx.edge_coords(-5) is None
