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

    def sample(anchors, z, g, k_pairs=None, scale=1.0):
        current["z"] = z
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
        "SegmentIndex": lambda g: None,
        "sample_od_pairs": sample,
        "probe_router": lambda coords, pairs, profile: probed.append(current["z"]),
        "route_and_accumulate": route,
        "save_sbs": lambda *a, **k: None,
        "detect_bypasses": lambda *a, **k: {},
        "detect_rings": lambda *a, **k: {},
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
