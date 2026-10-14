"""Car ferries survive a major-roads filter connected, and enter the zoom graph.

Measured 2026-10-13: a motorway..primary extract of Europe kept every ferry
route but none of the port roads reaching them, so Great Britain and Ireland
were separate networks from the continent and no route could cross.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

osmium = pytest.importorskip("osmium")

TOOLS = Path(__file__).resolve().parents[3] / "src" / "osm_geocoder" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from _osm_tools import ferry_access as fa  # noqa: E402

needs_osmium_cli = pytest.mark.skipif(shutil.which("osmium") is None, reason="needs osmium CLI")


@pytest.mark.parametrize(
    "tags,expected",
    [
        ({"route": "ferry"}, True),
        ({"route": "ferry", "motor_vehicle": "yes", "foot": "yes"}, True),
        ({"route": "ferry", "motorcar": "yes", "foot": "yes"}, True),
        ({"route": "ferry", "foot": "yes"}, False),  # passenger ferry
        ({"route": "ferry", "bicycle": "yes"}, False),
        ({"route": "ferry", "motor_vehicle": "no"}, False),
        ({"route": "shuttle_train"}, True),
        ({"highway": "primary"}, False),
    ],
)
def test_car_ferry_rule_matches_graphhopper(tags, expected):
    assert fa.is_car_ferry(tags) is expected


# Mainland: primary p1-p2; the terminal f1 hangs off p2 by service s1. A
# residential side street r1 branches off s1 and must NOT be added (shortest
# path only). Island: ferry f1-f2, then service s2 to the island primary q1-q2.
# A passenger ferry g1-g2 with its own service road must be ignored.
NODES = {
    1: (0.000, 0.000), 2: (0.010, 0.000),        # mainland primary
    3: (0.010, 0.005), 4: (0.010, 0.010),        # service s1 -> terminal f1
    5: (0.020, 0.005),                           # residential side street
    6: (0.010, 0.100), 7: (0.010, 0.105),        # terminal f2, service s2
    8: (0.010, 0.110), 9: (0.020, 0.110),        # island primary
    10: (0.000, 0.005), 11: (0.000, 0.090),      # passenger ferry g1-g2
}
WAYS = {
    101: ([1, 2], {"highway": "primary"}),
    102: ([2, 3, 4], {"highway": "service"}),
    103: ([3, 5], {"highway": "residential"}),
    104: ([4, 6], {"route": "ferry", "motor_vehicle": "yes", "foot": "yes"}),
    105: ([6, 7, 8], {"highway": "service"}),
    106: ([8, 9], {"highway": "primary"}),
    107: ([10, 11], {"route": "ferry", "foot": "yes"}),
    108: ([1, 10], {"highway": "service"}),
}


def _write(path: Path) -> None:
    w = osmium.SimpleWriter(str(path))
    try:
        for nid, (lon, lat) in NODES.items():
            w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), tags={}, version=1))
        for wid, (refs, tags) in WAYS.items():
            w.add_way(osmium.osm.mutable.Way(id=wid, nodes=refs, tags=tags, version=1))
    finally:
        w.close()


def _way_ids(pbf: Path) -> set[int]:
    class H(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.ids: set[int] = set()

        def way(self, w) -> None:  # noqa: ANN001
            self.ids.add(w.id)

    h = H()
    h.apply_file(str(pbf))
    return h.ids


@pytest.fixture
def extracts(tmp_path):
    src = tmp_path / "src.osm.pbf"
    _write(src)
    filtered = tmp_path / "filtered.osm.pbf"
    subprocess.run(
        ["osmium", "tags-filter", "-o", str(filtered), str(src), "w/highway=primary", "w/route=ferry"],
        check=True,
    )
    return src, filtered


@needs_osmium_cli
def test_filter_alone_strands_the_ferry(extracts):
    _src, filtered = extracts
    assert _way_ids(filtered) == {101, 104, 106, 107}  # no port roads


@needs_osmium_cli
def test_adds_only_the_shortest_link_at_each_car_ferry_end(extracts, tmp_path):
    src, filtered = extracts
    out = tmp_path / "out.osm.pbf"
    s = fa.add_ferry_access(str(src), str(filtered), str(out), radius_km=3, work_dir=str(tmp_path / "w"))
    ids = _way_ids(out)
    assert {102, 105} <= ids  # both terminals linked
    assert 103 not in ids  # a side street is not on the shortest path
    assert 108 not in ids  # the passenger ferry is not connected
    assert s.car_ferries == 1
    assert s.ends == 2 and s.ends_connected == 2 and s.ends_unreachable == 0
    assert s.ways_added == 2


@needs_osmium_cli
def test_zoom_graph_holds_the_car_ferry(extracts, tmp_path):
    from osm_geocoder.handlers.roads.zoom_graph import FERRY_FC, build_logical_graph

    src, filtered = extracts
    out = tmp_path / "out.osm.pbf"
    fa.add_ferry_access(str(src), str(filtered), str(out), work_dir=str(tmp_path / "w"))
    g = build_logical_graph(str(out))
    ferries = [e for e in g.edges if e.fc == FERRY_FC]
    assert len(ferries) == 1  # the passenger ferry is not routable by car
    assert ferries[0].length_m > 9_000  # f1 -> f2, ~10 km


def test_prune_never_drops_a_selected_ferry():
    """The port roads are below the graph's classes, so a ferry is always its
    own component in the graph -- an "orphan" the fragment prune would drop."""
    from osm_geocoder.handlers.roads.zoom_graph import FERRY_FC
    from osm_geocoder.handlers.roads.zoom_selection import prune_layers

    class E:
        def __init__(self, eid, fc, a, b, m):
            self.edge_id, self.fc, self.from_node, self.to_node, self.length_m = eid, fc, a, b, m
            self.coords = [(0.0, 0.0), (0.0, 0.01)]
            self.ref, self.name = "", ""

    class G:
        edges = [E(1, FERRY_FC, 10, 11, 800.0)]
        edge_by_id = {1: edges[0]}

    layers = {z: {1} for z in range(2, 8)}
    kept, dropped, _km = prune_layers(G(), layers)
    assert dropped == 0 and all(1 in kept[z] for z in kept)


def test_ferry_never_passes_a_class_floor():
    from osm_geocoder.handlers.roads.zoom_graph import FC_SCORES, FERRY_FC
    from osm_geocoder.handlers.roads.zoom_selection import MIN_FC_BY_ZOOM

    lowest = min(FC_SCORES[c] for c in MIN_FC_BY_ZOOM.values())
    assert FC_SCORES[FERRY_FC] < lowest


def test_recipe_fingerprint_names_the_ferry_behaviour():
    import inspect

    from osm_geocoder.handlers.roads import zoom_builder

    assert '"ferries"' in inspect.getsource(zoom_builder.recipe_fingerprint)
