"""NodeGrid returns exactly what a scan of every node returns."""

from __future__ import annotations

import random

import pytest

from osm_geocoder.handlers.roads.zoom_graph import _haversine_m
from osm_geocoder.handlers.roads.zoom_sbs import NodeGrid, _snap_to_nearest_node


def _scan(coords, lon, lat, max_dist_m):
    best_d, best = max_dist_m, None
    for nid, (nlon, nlat) in coords.items():
        d = _haversine_m(lon, lat, nlon, nlat)
        if d < best_d:
            best_d, best = d, nid
    return best


@pytest.mark.parametrize("lat0", [0.0, 37.0, 61.0, -45.0])
def test_matches_full_scan(lat0):
    rng = random.Random(7)
    coords = {i: (rng.uniform(-3, 3), rng.uniform(lat0 - 2, lat0 + 2)) for i in range(3000)}
    grid = NodeGrid(coords)
    for _ in range(400):
        lon, lat = rng.uniform(-4, 4), rng.uniform(lat0 - 3, lat0 + 3)
        for max_d in (2_000.0, 50_000.0, 500_000.0):
            assert grid.nearest(lon, lat, max_d) == _scan(coords, lon, lat, max_d)


def test_nothing_within_range_is_none():
    grid = NodeGrid({1: (0.0, 0.0)})
    assert grid.nearest(1.0, 1.0, 50_000.0) is None


def test_snap_builds_the_index_once_per_graph():
    class G:
        node_coords = {1: (0.0, 0.0), 2: (0.01, 0.0)}

    g = G()
    assert _snap_to_nearest_node(g, 0.009, 0.0) == 2
    first = g._node_grid
    assert _snap_to_nearest_node(g, 0.0, 0.0) == 1
    assert g._node_grid is first
