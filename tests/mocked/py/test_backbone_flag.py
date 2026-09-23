"""The `backbone` flag must reflect the repair that actually ran.

Backbone repair has always executed inside `select_edges`, but its result was
unioned into the selection and then dropped. So `backbone_edges` in metrics.json
was a hardcoded 0 and the per-edge `backbone` column was a hardcoded False in
both segment_scores.csv and edge_importance.jsonl — one of the three flags the
roads docs advertise ("minZoom per logical edge plus bypass/ring/backbone
flags") had never been emitted, in any run, for any region.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_selection as zs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph  # noqa: E402


def _edge(eid, a, b, lon0=-122.0):
    return LogicalEdge(edge_id=eid, from_node=a, to_node=b, osm_way_ids=[eid],
                       coords=[(lon0 + eid * 0.01, 47.0), (lon0 + (eid + 1) * 0.01, 47.0)],
                       length_m=1000.0, fc="primary", fc_score=0.5, ref="", name="",
                       maxspeed=50, lanes=2, bridge=False, tunnel=False, oneway=False,
                       surface_unpaved=False)


def _graph(n=6):
    g = RoadGraph()
    for i in range(n):
        g.add_edge(_edge(i, i, i + 1))
        g.node_coords[i] = (-122.0 + i * 0.01, 47.0)
    g.node_coords[n] = (-122.0 + n * 0.01, 47.0)
    return g


def test_backbone_out_is_populated_per_zoom():
    g = _graph()
    scores = {z: {e.edge_id: 0.5 for e in g.edges} for z in range(2, 8)}
    budgets = {z: {} for z in range(2, 8)}
    anchors = {z: [0, 3, 6] for z in range(2, 8)}
    out: dict[int, set[int]] = {}
    zs.select_edges(g, scores, budgets, anchors, backbone_out=out)
    assert set(out) <= set(range(2, 8))
    assert out, "backbone_out must be filled for the zooms that ran"
    for z, eids in out.items():
        assert isinstance(eids, set)


def test_select_edges_still_works_without_the_out_param():
    """The SelectEdges facet handler calls this without it."""
    g = _graph()
    scores = {z: {e.edge_id: 0.5 for e in g.edges} for z in range(2, 8)}
    sel = zs.select_edges(g, scores, {z: {} for z in range(2, 8)},
                          {z: [0, 3, 6] for z in range(2, 8)})
    assert isinstance(sel, dict)


def test_exports_no_longer_hardcode_the_flag():
    src = (Path(__file__).resolve().parents[3]
           / "src/osm_geocoder/handlers/roads/zoom_builder.py").read_text()
    assert '"backbone": False' not in src, "the per-edge flag must not be hardcoded"
    assert '"backbone": eid in backbone_edges' in src
    assert "backbone_count = len(backbone_edges)" in src
    assert "backbone_count = 0" not in src
