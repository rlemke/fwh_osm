"""build_zoom_layers routes only the zooms up to route_max_zoom.

A country-scale run on a major-roads graph routes z2-z4: z5-z7 would pair
hundreds of thousands of towns over continental distances on a graph that has
none of the roads those tiers ride. Every heavy stage is stubbed; what is under
test is which zooms reach the router.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from osm_geocoder.handlers.roads import zoom_builder as zb


@pytest.fixture
def routed_zooms(monkeypatch, tmp_path):
    calls: list[int] = []
    probed: list[int] = []
    graph = SimpleNamespace(edges={1: object()}, node_coords={1: (0.0, 0.0)})
    current = {"z": 0}
    ks: dict[int, int] = {}
    index = SimpleNamespace(cleared=0)
    index.clear_memo = lambda: setattr(index, "cleared", index.cleared + 1)
    detector_index: list = []

    def sample(anchors, z, g, k_pairs=None, scale=1.0):
        current["z"] = z
        ks[z] = k_pairs
        return [(1, 1)]

    def route(pairs, *a, **k):
        calls.append(current["z"])
        return {1: 1.0}, 1

    stubs = {
        "HAS_OSMIUM": True,
        "HAS_REQUESTS": True,
        "_ensure_cities_file": lambda *a, **k: 0,
        "build_logical_graph": lambda *a, **k: graph,
        "region_scale": lambda g: 1.0,
        "build_anchors": lambda *a, **k: [1],
        "save_anchors": lambda *a, **k: None,
        "SegmentIndex": lambda g: index,
        "recipe_fingerprint": lambda: "recipe",
        "sample_od_pairs": sample,
        "probe_router": lambda coords, pairs, profile: probed.append(current["z"]),
        "route_and_accumulate": route,
        "save_sbs": lambda *a, **k: None,
        "detect_bypasses": lambda *a, **k: detector_index.append(k.get("segment_index")) or {},
        "detect_rings": lambda *a, **k: detector_index.append(k.get("segment_index")) or {},
        "save_bypass_flags": lambda *a, **k: None,
        "save_ring_flags": lambda *a, **k: None,
        "compute_scores": lambda *a, **k: {},
        "build_cell_budgets": lambda *a, **k: {},
        "select_edges": lambda *a, **k: {},
        "enforce_monotonic_reveal": lambda s: {},
        "prune_assignments": lambda g, a, b: (a, 0, 0.0),
        "_export_results": lambda *a, **k: ({"sbs": a[3]}, {}),
    }
    for name, value in stubs.items():
        monkeypatch.setattr(zb, name, value)

    def run(**kw):
        calls.clear()
        probed.clear()
        result, _ = zb.build_zoom_layers(
            {"path": "x.pbf"},
            {"graphDir": "/g", "profile": "car"},
            output_dir=str(tmp_path),
            **kw,
        )
        return list(calls), list(probed), result["sbs"]

    run.ks = ks
    run.index = index
    run.detector_index = detector_index
    run.monkeypatch = monkeypatch
    return run


def test_default_routes_every_zoom(routed_zooms):
    calls, _, sbs = routed_zooms()
    assert calls == [2, 3, 4, 5, 6, 7]
    assert all(sbs[z] for z in range(2, 8))


def test_route_max_zoom_stops_routing_above_it(routed_zooms):
    calls, probed, sbs = routed_zooms(route_max_zoom=4)
    assert calls == [2, 3, 4]
    assert probed == [2]
    assert all(sbs[z] for z in (2, 3, 4))
    # Above the cap the band exists but nothing in it was routed.
    assert sbs[5] == sbs[6] == sbs[7] == {}


def test_pulse_fires_on_the_clock_between_counts(monkeypatch):
    from osm_geocoder.handlers.roads import zoom_sbs

    now = [1000.0]
    monkeypatch.setattr(zoom_sbs.time, "monotonic", lambda: now[0])
    pulse = zoom_sbs._Pulse()
    assert not pulse.due(1)
    now[0] += zoom_sbs.HEARTBEAT_SECONDS
    assert pulse.due(2)  # slow items: the clock, not the count
    assert not pulse.due(3)
    assert pulse.due(zoom_sbs.HEARTBEAT_EVERY)  # fast items: the count


def test_max_pairs_caps_every_zoom_below_its_default(routed_zooms):
    routed_zooms(max_pairs=8000)
    assert routed_zooms.ks == {2: 5_000, 3: 8_000, 4: 8_000, 5: 8_000, 6: 8_000, 7: 8_000}


def test_step4_reuses_step3_index_with_its_memo_cleared(routed_zooms):
    """Building fresh indexes in step 4 while step 3's was alive OOM'd the US run."""
    routed_zooms(route_max_zoom=2)
    assert routed_zooms.detector_index == [routed_zooms.index, routed_zooms.index]
    assert routed_zooms.index.cleared >= 2


def test_a_retry_loads_routed_zooms_instead_of_rerouting(routed_zooms, tmp_path):
    from osm_geocoder.handlers.roads import zoom_sbs

    routed_zooms.monkeypatch.setattr(zb, "save_sbs", zoom_sbs.save_sbs)
    first, _, sbs1 = routed_zooms(route_max_zoom=3)
    assert first == [2, 3]
    assert (tmp_path / "sbs_z2.key").exists()

    again, _, sbs2 = routed_zooms(route_max_zoom=3)
    assert again == []  # nothing rerouted
    assert sbs2[2] == sbs1[2] and sbs2[3] == sbs1[3]

    # A different pair set is a different routing: the checkpoint is ignored.
    (tmp_path / "sbs_z3.key").write_text("stale")
    third, _, _ = routed_zooms(route_max_zoom=3)
    assert third == [3]


def test_snap_memo_is_bounded(monkeypatch):
    from osm_geocoder.handlers.roads import zoom_sbs

    edge = SimpleNamespace(edge_id=7, coords=[(0.0, 0.0), (0.01, 0.0)])
    idx = zoom_sbs.SegmentIndex(SimpleNamespace(edges=[edge]))
    monkeypatch.setattr(zoom_sbs.SegmentIndex, "SNAP_MEMO_MAX", 3)
    monkeypatch.setattr(zoom_sbs.SegmentIndex, "STICKY_M", -1.0)  # force grid lookups
    for i in range(10):
        assert idx.snap_route([[0.0005 * i, 0.0], [0.0005 * i, 0.0]]) == {7}
    assert len(idx._snap_memo) <= 3
    assert idx.memo_resets >= 2
