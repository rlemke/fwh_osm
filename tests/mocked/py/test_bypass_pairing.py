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
