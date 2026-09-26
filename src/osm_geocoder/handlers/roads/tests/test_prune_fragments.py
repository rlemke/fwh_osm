"""Orphan-fragment pruning: the selection's own leftovers, not the data's.

Selection is PER EDGE and checks no connectivity — the greedy pass adds whatever
fits a cell's budget and the sparse-region floor pads under-filled rural cells one
edge at a time, neither asking whether the edge touches anything already chosen. So
short stubs appear in the middle of nowhere. Measured 2026-09-25 on the published
layers: iowa z7 held 727 components for one real network, nebraska z7 held 1,012,
most of them one or two edges.

⚠️ They are not disconnected in OSM. At z7 the class floor is `unclassified` and the
roads joining a farm stub to the network are `residential`/`service`, below the
floor — so the connector exists but is invisible at this zoom. Pruning is therefore
cartographic generalisation, and the one thing it must never do is delete a network
that is genuinely isolated in the data.
"""

import pytest

from osm_geocoder.handlers.roads import zoom_selection as zs
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph
from osm_geocoder.handlers.roads.zoom_selection import MIN_COMPONENT_KM, prune_fragments


def _edge(eid: int, a: int, b: int, km: float, fc: str = "unclassified") -> LogicalEdge:
    return LogicalEdge(eid, a, b, [], [(0.0, 0.0), (0.0, 0.0)], km * 1000.0,
                       fc, 0.5, "", "", 0, 0, False, False, False, False)


def _graph(*edges: LogicalEdge) -> RoadGraph:
    g = RoadGraph()
    for e in edges:
        g.add_edge(e)
    return g


class TestPruneFragments:
    def test_short_cut_fragment_is_dropped(self):
        """The reported defect: a stub whose connector was not selected."""
        g = _graph(
            _edge(1, 10, 11, 20.0),   # the network
            _edge(2, 11, 12, 20.0),
            _edge(3, 50, 51, 0.4),    # the stub
            _edge(4, 51, 52, 0.4),
            _edge(9, 52, 11, 0.3),    # its connector, NOT selected
        )
        kept, dropped, km = prune_fragments(g, {1, 2, 3, 4}, 7)
        assert kept == {1, 2}
        assert dropped == 2
        assert km == pytest.approx(0.8)

    def test_a_genuinely_isolated_network_survives(self):
        """⚠️ An island's roads are short AND disconnected — and real.

        The discriminator is whether WE cut it: if no node of the component
        continues into an unselected edge, nothing was removed to create it.
        """
        g = _graph(
            _edge(1, 10, 11, 20.0),
            _edge(3, 50, 51, 0.4),    # island, complete: nothing else touches it
            _edge(4, 51, 52, 0.4),
        )
        kept, dropped, _ = prune_fragments(g, {1, 3, 4}, 7)
        assert kept == {1, 3, 4}
        assert dropped == 0

    def test_long_fragment_survives_even_though_cut(self):
        g = _graph(
            _edge(1, 10, 11, 20.0),
            _edge(3, 50, 51, 9.0),    # over the z7 bar of 2.5 km
            _edge(9, 51, 11, 0.3),    # unselected connector
        )
        kept, dropped, _ = prune_fragments(g, {1, 3}, 7)
        assert kept == {1, 3}
        assert dropped == 0

    def test_backbone_edges_are_never_dropped(self):
        """Backbone repair connects anchors; undoing its work would defeat it."""
        g = _graph(
            _edge(1, 10, 11, 20.0),
            _edge(3, 50, 51, 0.4),
            _edge(9, 51, 11, 0.3),
        )
        kept, dropped, _ = prune_fragments(g, {1, 3}, 7, protected={3})
        assert kept == {1, 3}
        assert dropped == 0

    def test_the_threshold_rises_with_zoom(self):
        """Counter-intuitive but deliberate: the denser the tier, the smaller a
        share an orphan of a given length is, so the safer it is to drop. The
        first cut had it the other way round and removed 29% of z4's length."""
        assert MIN_COMPONENT_KM[7] > MIN_COMPONENT_KM[4]
        assert all(MIN_COMPONENT_KM[z] <= MIN_COMPONENT_KM[z + 1] for z in range(2, 7))

    def test_a_1_2km_fragment_survives_at_z4_and_dies_at_z7(self):
        """The same piece is judged against the tier it appears in."""
        def run(zoom):
            g = _graph(
                _edge(1, 10, 11, 50.0),
                _edge(3, 50, 51, 1.2),
                _edge(9, 51, 11, 0.3),
            )
            return prune_fragments(g, {1, 3}, zoom)[0]

        assert 3 in run(4)      # 0.5 km bar
        assert 3 not in run(7)  # 2.5 km bar

    def test_empty_selection_is_not_an_error(self):
        assert prune_fragments(_graph(_edge(1, 1, 2, 5.0)), set(), 7) == (set(), 0, 0.0)

    def test_a_zoom_with_no_threshold_is_left_alone(self):
        g = _graph(_edge(1, 10, 11, 0.1), _edge(9, 11, 12, 0.1))
        kept, dropped, _ = prune_fragments(g, {1}, 99)
        assert kept == {1} and dropped == 0


class TestPruneAssignments:
    """⚠️ The regression this class exists for.

    Pruning each zoom's own selection BEFORE the monotonic union cannot see what
    the union puts on the map: a component clearing z6's 1.5 km bar is carried into
    z7 and never re-tested against z7's 2.5 km bar (69 such edges survived on
    natively-built Washington). But pruning the unioned layers INDEPENDENTLY breaks
    monotonic reveal, because the bar rises with zoom — measured on the backfilled
    layers, Maryland lost 112 edges between z6 and z7 and Arizona 87. A road that
    vanishes as you zoom IN is worse than the stub it replaced.
    """

    def _fixture(self):
        # A long spine, plus a 2.0 km orphan: over z5's 1.0 km bar, under z7's 2.5.
        g = _graph(
            _edge(1, 10, 11, 40.0),
            _edge(2, 11, 12, 40.0),
            _edge(3, 50, 51, 2.0),   # the orphan
            _edge(9, 51, 12, 0.2),   # its unselected connector
        )
        # Already monotonic: each zoom a superset of the one below.
        assignments = {
            5: {1, 3},
            6: {1, 2, 3},
            7: {1, 2, 3},
        }
        return g, assignments

    def test_a_zoom_specific_drop_is_applied_to_every_zoom(self):
        g, assignments = self._fixture()
        pruned, n, km = zs.prune_assignments(g, assignments)
        assert n == 1 and km == pytest.approx(2.0)
        for z, kept in pruned.items():
            assert 3 not in kept, f"the orphan survived at z{z}"

    def test_monotonic_reveal_survives_the_prune(self):
        """Nothing may disappear as you zoom in."""
        g, assignments = self._fixture()
        pruned, _, _ = zs.prune_assignments(g, assignments)
        for z in sorted(pruned)[:-1]:
            assert pruned[z] <= pruned[z + 1], f"z{z} is not a subset of z{z + 1}"

    def test_the_real_network_is_untouched(self):
        g, assignments = self._fixture()
        pruned, _, _ = zs.prune_assignments(g, assignments)
        assert pruned[7] == {1, 2}
        assert pruned[5] == {1}

    def test_nothing_to_do_is_a_clean_no_op(self):
        g = _graph(_edge(1, 10, 11, 40.0), _edge(2, 11, 12, 40.0))
        pruned, n, km = zs.prune_assignments(g, {7: {1, 2}})
        assert (n, km) == (0, 0.0)
        assert pruned == {7: {1, 2}}

    def test_backbone_edges_are_protected_per_zoom(self):
        g, assignments = self._fixture()
        pruned, n, _ = zs.prune_assignments(g, assignments, backbone_by_zoom={7: {3}})
        assert n == 0
        assert all(3 in kept for kept in pruned.values())
