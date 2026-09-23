"""Zoom builder liveness + snap memo.

Regression for the 2026-09-22 Washington run: vote accumulation for zoom 3
alone ran past the 30-minute stuck-task watchdog with no heartbeat, the task
was reclaimed and a second runner restarted the whole job while the first kept
burning a core. Step logs are not progress; only the heartbeat is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_sbs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph  # noqa: E402


class Cancelled(Exception):
    pass


def _edge(eid, a, b, coords):
    return LogicalEdge(edge_id=eid, from_node=a, to_node=b, osm_way_ids=[eid], coords=coords,
                       length_m=1000.0, fc="primary", fc_score=0.5, ref="", name="", maxspeed=50,
                       lanes=2, bridge=False, tunnel=False, oneway=False, surface_unpaved=False)


def _graph():
    g = RoadGraph()
    g.node_coords = {1: (-122.30, 47.60), 2: (-122.20, 47.60), 3: (-122.20, 47.70)}
    g.add_edge(_edge(10, 1, 2, [(-122.30, 47.60), (-122.25, 47.60), (-122.20, 47.60)]))  # E-W
    g.add_edge(_edge(11, 2, 3, [(-122.20, 47.60), (-122.20, 47.65), (-122.20, 47.70)]))  # N-S
    return g


def _route_along_10():
    return [[-122.30 + i * 0.005, 47.60] for i in range(21)]


def test_snap_memo_gives_identical_matches_and_hits_on_repeat():
    idx = zoom_sbs.SegmentIndex(_graph())
    first = idx.snap_route(_route_along_10())
    misses_after_first = idx.snap_misses
    second = idx.snap_route(_route_along_10())
    assert first == second == {10}
    assert idx.snap_misses == misses_after_first, "second pass must not recompute"
    assert idx.snap_hits >= 20, "every coordinate of the repeated route is a memo hit"


def test_snap_memo_keeps_misses_as_misses():
    idx = zoom_sbs.SegmentIndex(_graph())
    far = [[-121.0, 46.0], [-121.0, 46.0]]  # nowhere near an edge
    assert idx.snap_route(far) == set()
    assert idx.snap_route(far) == set()
    assert idx.snap_hits >= 1


def test_accumulate_votes_heartbeats_and_honours_cancel(monkeypatch):
    monkeypatch.setattr(zoom_sbs, "HEARTBEAT_EVERY", 2)
    idx = zoom_sbs.SegmentIndex(_graph())
    routes = {(i, i + 1): _route_along_10() for i in range(6)}
    beats: list[str] = []
    bc = zoom_sbs.accumulate_votes(routes, idx, heartbeat=beats.append)
    assert bc == {10: 6}
    assert len(beats) == 3 and beats[-1].startswith("snapped 6/6")

    def cancel():
        raise Cancelled()

    with pytest.raises(Cancelled):
        zoom_sbs.accumulate_votes(routes, idx, heartbeat=beats.append, check_cancel=cancel)


def test_route_batch_heartbeats_and_cancel_stops_the_pool(monkeypatch):
    monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
    monkeypatch.setattr(zoom_sbs, "HEARTBEAT_EVERY", 2)
    monkeypatch.setattr(zoom_sbs, "_route_pair", lambda a, b, nc, gd, pr: [[0.0, 0.0], [1.0, 1.0]])
    pairs = [(i, i + 1) for i in range(7)]
    beats: list[str] = []
    out = zoom_sbs.route_batch_parallel(pairs, {}, "g", "car", 2, heartbeat=beats.append)
    assert len(out) == 7
    assert beats and beats[-1] == "routed 7/7 pairs"
    assert any(b.startswith("routed 2/7") for b in beats)

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        zoom_sbs.route_batch_parallel(pairs * 20, {}, "g", "car", 2, check_cancel=cancel)


def test_route_and_accumulate_streams_snaps_and_retains_nothing(monkeypatch):
    """Votes must equal route-then-snap, with a bounded submit window and
    heartbeats; nothing about the routes is kept (the 25 GB OOM shape)."""
    monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
    monkeypatch.setattr(zoom_sbs, "HEARTBEAT_EVERY", 2)
    monkeypatch.setattr(zoom_sbs, "_route_pair", lambda a, b, nc, gd, pr: _route_along_10())
    idx = zoom_sbs.SegmentIndex(_graph())
    pairs = [(i, i + 1) for i in range(9)]
    beats: list[str] = []
    bc, routed = zoom_sbs.route_and_accumulate(
        pairs, {}, "g", "car", idx, max_concurrent=2, heartbeat=beats.append, submit_window=3
    )
    assert routed == 9 and bc == {10: 9}
    assert beats[-1] == "routed 9/9 pairs" and len(beats) >= 4

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        zoom_sbs.route_and_accumulate(pairs * 10, {}, "g", "car", idx, 2, check_cancel=cancel)
