"""Bypass detection must actually pair entry/exit nodes.

`_pair_by_angle` was a stub that returned [] on every call — its own comment
said it "will be called from detect_bypasses with proper context", but
detect_bypasses called the stub, not the real `_pair_entry_exit_by_angle`
beneath it. Bypass detection therefore could not return a bypass for ANY
region: every settlement fell out before a single route was requested, while
the step reported success. Measured 2026-09-23 on Washington — 2,198
settlements, 0 pairs examined, 0 bypasses; with the real function wired in, 11
settlements yield 502 pairs and 679 flagged edges.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_detection as zd  # noqa: E402


def _ring_coords(center=(-122.0, 47.0), r=0.05, n=4):
    """n nodes evenly around a centre — every opposite pair is 180° apart."""
    return {
        i: (center[0] + r * math.cos(2 * math.pi * i / n),
            center[1] + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    }


def test_opposite_nodes_pair():
    coords = _ring_coords(n=4)
    pairs = zd._pair_entry_exit_by_angle(list(coords), -122.0, 47.0, coords)
    assert pairs, "four nodes around a centre must yield at least one >90 deg pair"
    for a, b in pairs:
        ang = abs(math.atan2(coords[a][1] - 47.0, coords[a][0] + 122.0)
                  - math.atan2(coords[b][1] - 47.0, coords[b][0] + 122.0))
        ang = min(ang, 2 * math.pi - ang)
        assert ang > math.pi / 2 - 1e-9


def test_clustered_nodes_do_not_pair():
    """Nodes all on one side of a settlement are not an entry/exit pair."""
    coords = {i: (-122.0 + 0.05 + 0.001 * i, 47.0 + 0.001 * i) for i in range(4)}
    assert zd._pair_entry_exit_by_angle(list(coords), -122.0, 47.0, coords) == []


def test_unknown_node_ids_are_skipped_not_fatal():
    coords = _ring_coords(n=4)
    assert zd._pair_entry_exit_by_angle([*coords, 999], -122.0, 47.0, coords)


def test_detect_bypasses_uses_the_real_pairing_not_a_stub():
    src = Path(zd.__file__).read_text()
    assert "_pair_entry_exit_by_angle(entry_exit" in src, "caller must use the real function"
    assert "def _pair_by_angle(" not in src, "the always-empty stub must not come back"


def test_rejections_are_tallied_by_reason():
    """A bypass count of 0 must be explainable — it is otherwise
    indistinguishable from the step never running."""
    src = Path(zd.__file__).read_text()
    assert "Bypass rejections by reason" in src
    assert "entry/exit pairs examined" in src


def test_detection_phases_are_heartbeat_and_cancel_aware(monkeypatch, tmp_path):
    """Detection is SILENT and long — three routing calls per entry/exit pair
    over thousands of settlements. Without a heartbeat the stuck-task watchdog
    reclaims a run whose handler is at 96% CPU (measured 2026-09-23, caught
    12 minutes before the reclaim only because the phase was being watched)."""
    import json as _json

    cities = tmp_path / "cities.geojson"
    cities.write_text(_json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"place": "city", "name": f"C{i}", "population": 200_000},
         "geometry": {"type": "Point", "coordinates": [-122.0 + i * 0.5, 47.0]}}
        for i in range(60)
    ]}))
    monkeypatch.setattr(zd, "DETECT_HEARTBEAT_EVERY", 5)
    monkeypatch.setattr(zd, "HAS_REQUESTS", False)  # no routing; we want the loop only

    g = _tiny_graph()
    beats: list[str] = []
    zd.detect_bypasses(g, str(cities), "gd", "car", heartbeat=beats.append)
    assert any(b.startswith("bypasses:") for b in beats), "bypass loop must report liveness"

    beats.clear()
    zd.detect_rings(g, str(cities), "gd", "car", heartbeat=beats.append)
    assert any(b.startswith("rings:") for b in beats), "ring loop must report liveness"

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        raise Cancelled()

    try:
        zd.detect_bypasses(g, str(cities), "gd", "car", check_cancel=cancel)
    except Cancelled:
        pass
    assert calls["n"] >= 1, "cancellation must be checked inside the settlement loop"


class Cancelled(Exception):
    pass


def _tiny_graph():
    from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph
    g = RoadGraph()
    for i in range(3):
        g.add_edge(LogicalEdge(edge_id=i, from_node=i, to_node=i + 1, osm_way_ids=[i],
                               coords=[(-122.0 + i * 0.01, 47.0), (-122.0 + (i + 1) * 0.01, 47.0)],
                               length_m=1000.0, fc="primary", fc_score=0.5, ref="", name="",
                               maxspeed=50, lanes=2, bridge=False, tunnel=False,
                               oneway=False, surface_unpaved=False))
        g.node_coords[i] = (-122.0 + i * 0.01, 47.0)
    g.node_coords[3] = (-122.0 + 0.03, 47.0)
    return g
