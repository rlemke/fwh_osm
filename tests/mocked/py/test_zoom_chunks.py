"""Routing as separate tasks: plan -> chunks on any host -> merged votes.

The US z2-z4 build routed 15,317 pairs as ONE task for 2 h 30 m, so a restart
lost up to a whole zoom and only one host's CPUs worked on it. Split, the
merged votes must be EXACTLY what one loop over the same pairs produces, and a
zoom missing any chunk must not be merged (the finisher routes it instead).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_builder as zb  # noqa: E402
from osm_geocoder.handlers.roads import zoom_handlers as zh  # noqa: E402
from osm_geocoder.handlers.roads import zoom_sbs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph  # noqa: E402


def _edge(eid, a, b, coords):
    return LogicalEdge(edge_id=eid, from_node=a, to_node=b, osm_way_ids=[eid], coords=coords,
                       length_m=1000.0, fc="primary", fc_score=0.5, ref="", name="", maxspeed=50,
                       lanes=2, bridge=False, tunnel=False, oneway=False, surface_unpaved=False)


def _graph():
    """A row of 6 nodes, 0.01 deg apart, joined by edges 100..104."""
    g = RoadGraph()
    g.node_coords = {i: (-120.0 + 0.01 * i, 40.0) for i in range(6)}
    for i in range(5):
        g.add_edge(_edge(100 + i, i, i + 1, [g.node_coords[i], g.node_coords[i + 1]]))
    return g


PAIRS = {2: [(0, 5), (1, 4), (0, 2)], 3: [(0, 1), (2, 5), (3, 4), (1, 3), (0, 4)]}


@pytest.fixture
def planned(monkeypatch, tmp_path):
    g = _graph()
    ctx = SimpleNamespace(pbf_path="x.pbf", graph_dir="s3://b/cache/osm/graphhopper/r-latest/car",
                          profile="car", cities_path="", city_count=0, road_graph=g, scale=1.0,
                          anchors_by_zoom={})
    monkeypatch.setattr(zb, "_prepare", lambda *a, **k: ctx)
    monkeypatch.setattr(zb, "_zoom_pairs", lambda c, z, mp: (PAIRS[z], f"key-z{z}"))
    monkeypatch.setattr(zb, "probe_router", lambda *a, **k: None)
    monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)

    def route(a, b, nc, gd, pr, tally=None, detail=None):
        # The straight line along the row: every edge between a and b.
        lo, hi = sorted((a, b))
        return [[-120.0 + 0.01 * i, 40.0] for i in range(lo, hi + 1)]

    monkeypatch.setattr(zoom_sbs, "_route_pair", route)
    monkeypatch.setenv("FW_ZOOM_SNAP_WORKERS", "0")
    plan = zb.plan_zoom_routing({}, {}, 0, str(tmp_path / "w"), route_max_zoom=3, chunk_pairs=2)
    return plan, g, tmp_path / "w"


def _route_all(plan, workdir):
    docs = []
    for rel in plan["chunks"]:
        chunk = json.loads((workdir / rel).read_text())
        docs.append(zb.route_zoom_chunk(chunk, str(workdir / "routing" / "index")))
    return docs


def test_plan_cuts_each_zoom_into_chunks(planned):
    plan, _g, workdir = planned
    assert plan["chunks"] == ["routing/chunks/z2_000.json", "routing/chunks/z2_001.json",
                              "routing/chunks/z3_000.json", "routing/chunks/z3_001.json",
                              "routing/chunks/z3_002.json"]
    assert plan["zooms"] == {"2": {"key": "key-z2", "pairs": 3, "chunks": 2},
                             "3": {"key": "key-z3", "pairs": 5, "chunks": 3}}
    c = json.loads((workdir / "routing/chunks/z3_002.json").read_text())
    assert c["pairs"] == [[0, 4]] and c["index"] == 2 and c["count"] == 3
    assert set(c["coords"]) == {"0", "4"}  # only the nodes this chunk routes between
    assert (workdir / "routing/index/ID").read_text()


def test_merged_chunks_equal_one_loop_over_the_same_pairs(planned):
    plan, g, workdir = planned
    docs = _route_all(plan, workdir)
    idx = zoom_sbs.SegmentIndex(g)
    for z, pairs in PAIRS.items():
        merged = zb._merge_chunk_votes(docs, z, f"key-z{z}")
        whole = zoom_sbs.route_and_accumulate(pairs, g.node_coords, "", "car", idx, 2)
        assert merged == whole


def test_a_zoom_missing_a_chunk_is_not_merged(planned):
    plan, _g, workdir = planned
    docs = [d for d in _route_all(plan, workdir) if not (d["zoom"] == 3 and d["index"] == 1)]
    assert zb._merge_chunk_votes(docs, 2, "key-z2") is not None
    assert zb._merge_chunk_votes(docs, 3, "key-z3") is None


def test_votes_from_another_plan_are_never_merged(planned):
    plan, _g, workdir = planned
    docs = _route_all(plan, workdir)
    assert zb._merge_chunk_votes(docs, 2, "a-different-key") is None


def test_routing_paths_and_a_local_index_dir():
    routing, index, votes = zh._routing_paths("s3://b/out/us/routing/chunks/z4_007.json")
    assert routing == "s3://b/out/us/routing"
    assert index == "s3://b/out/us/routing/index"
    assert votes == "s3://b/out/us/routing/votes/z4_007.json"
    assert zh._local_index("/local/index") == "/local/index"


def test_route_zoom_chunks_is_a_capped_foreach_over_the_plan():
    src = (Path(zb.__file__).parent / "ffl" / "osmzoombuilder.ffl").read_text()
    body = src[src.index("facet RouteZoomChunks"):]
    assert "foreach c in $.chunks limit $.width" in body.split("}")[0]
    assert "votes += r.votes_path" in body.split("}")[0]


def test_route_zoom_chunk_handler_is_idempotent(planned, monkeypatch):
    """Delivery is at-least-once: a second delivery returns the votes the first
    wrote instead of routing the chunk again."""
    plan, _g, workdir = planned
    handler = zh._make_route_zoom_chunk_handler("RouteZoomChunk")
    chunk = str(workdir / plan["chunks"][0])
    first = handler({"chunk": chunk})
    assert Path(first["votes_path"]).exists() and first["routed"] == 2

    def must_not_route(*a, **k):
        raise AssertionError("routed a chunk whose votes already exist")

    monkeypatch.setattr(zb, "route_zoom_chunk", must_not_route)
    monkeypatch.setattr(zh, "route_zoom_chunk", must_not_route)
    assert handler({"chunk": chunk}) == first
