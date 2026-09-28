"""Selection admits whole ROADS, not individual edges.

The defect: selection was per edge under a per-cell budget and nothing tied an
edge to the road it belongs to, so an interior segment that lost its cell's
budget left a HOLE while its neighbours stayed. Because the layers are
cumulative, that hole is what the map draws at that zoom.

Measured on the published New York run before this change: of 947 named routes
with >=5 selected edges, 338 were more broken at z5 than they are complete
(45%), 318 at z6, 202 at z4. NY 34B is 2 pieces whole and EIGHT at z5 — and
every one of its 34 edges is `secondary`, i.e. class-eligible at z5, so the
holes were purely budget. After: 7 at z5, 7 at z6, 13 at z4.
"""

import pytest

from osm_geocoder.handlers.roads import zoom_selection as zs
from osm_geocoder.handlers.roads.zoom_graph import LogicalEdge, RoadGraph


def _edge(eid, a, b, km=1.0, ref="", name="", fc="secondary", fc_score=0.6):
    return LogicalEdge(eid, a, b, [], [(0.0, 0.0), (0.01, 0.0)], km * 1000.0,
                       fc, fc_score, ref, name, 0, 0, False, False, False, False)


def _graph(*edges):
    g = RoadGraph()
    for e in edges:
        g.add_edge(e)
    return g


class TestCorridorConstruction:
    def test_a_ref_run_is_one_corridor(self):
        g = _graph(_edge(1, 1, 2, ref="NY 34B"),
                   _edge(2, 2, 3, ref="NY 34B"),
                   _edge(3, 3, 4, ref="NY 34B"))
        corridors = zs.build_corridors(g, {1, 2, 3})
        assert len(corridors) == 1
        assert sorted(corridors[0]) == [1, 2, 3]

    def test_the_SAME_ref_in_two_places_is_two_corridors(self):
        """⚠️ Connectivity is part of the identity, not just the name.

        A ref can reappear far away, and `name:Main Street` appears in every
        town. Grouping by name alone would make ONE budget decision for edges
        300 km apart.
        """
        g = _graph(_edge(1, 1, 2, ref="NY 34B"),
                   _edge(2, 2, 3, ref="NY 34B"),
                   _edge(3, 90, 91, ref="NY 34B"))   # elsewhere
        assert len(zs.build_corridors(g, {1, 2, 3})) == 2

    def test_name_is_the_fallback_identity(self):
        g = _graph(_edge(1, 1, 2, name="Main Street"),
                   _edge(2, 2, 3, name="Main Street"))
        assert len(zs.build_corridors(g, {1, 2})) == 1

    def test_an_unnamed_edge_is_its_own_corridor(self):
        """Keeps per-edge behaviour for the unnamed local roads that make up
        most of the z7 tier, where there is no route to keep continuous."""
        g = _graph(_edge(1, 1, 2), _edge(2, 2, 3))
        assert len(zs.build_corridors(g, {1, 2})) == 2

    def test_only_eligible_edges_form_corridors(self):
        """A route that drops below the zoom's class floor is two corridors
        there and one a zoom later — the floor decides what MAY be drawn, this
        decides that whatever may be drawn is drawn whole."""
        g = _graph(_edge(1, 1, 2, ref="US 20"),
                   _edge(2, 2, 3, ref="US 20"),
                   _edge(3, 3, 4, ref="US 20"))
        assert len(zs.build_corridors(g, {1, 3})) == 2   # 2 held back by the floor


class TestCorridorScore:
    def test_is_length_weighted_mean_not_sum(self):
        """⚠️ The budget is charged in KILOMETRES, so the ranking must be
        score-per-km. A sum ranks a long mediocre road above a short vital one
        for being long; a max lets one good segment carry a whole route into a
        continental zoom."""
        g = _graph(_edge(1, 1, 2, km=1.0, ref="A"), _edge(2, 2, 3, km=9.0, ref="A"))
        scores = {1: 1.0, 2: 0.0}
        assert zs.corridor_score([1, 2], scores, g) == pytest.approx(0.1)

    def test_empty_corridor_scores_zero(self):
        assert zs.corridor_score([], {}, _graph()) == 0.0


class TestAtomicAdmission:
    """A budget too small for the whole road admits NONE of it.

    Cells are stubbed rather than derived from H3: the point under test is the
    admission rule, and real cells would make the fixture depend on where the
    synthetic coordinates happen to land.
    """

    def _setup(self, monkeypatch, cells_of, budget_by_cell, n=5, km=1.0):
        g = _graph(*[_edge(i, i, i + 1, km=km, ref="NY 34B") for i in range(1, n + 1)])
        monkeypatch.setattr(zs, "HAS_H3", True)
        monkeypatch.setattr(zs, "_edge_to_cells", lambda e: cells_of(e.edge_id))
        scores = {z: {i: 0.9 for i in range(1, n + 1)} for z in range(2, 8)}
        budgets = {
            z: {
                c: {"budget_km": b, "density_factor": 1.0, "road_km": 99.0,
                    "anchor_count": 0}
                for c, b in budget_by_cell.items()
            }
            for z in range(2, 8)
        }
        return g, scores, budgets, {z: [] for z in range(2, 8)}

    def test_a_road_that_fits_is_taken_whole(self, monkeypatch):
        monkeypatch.setattr(zs, "CORRIDOR_SELECTION", True)
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.0)
        args = self._setup(monkeypatch, lambda _e: {"c"}, {"c": 100.0})
        assert zs.select_edges(*args)[5] == {1, 2, 3, 4, 5}

    def test_a_road_that_does_not_fit_is_deferred_WHOLE(self, monkeypatch):
        """The point of the change: no half-drawn road."""
        monkeypatch.setattr(zs, "CORRIDOR_SELECTION", True)
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.0)
        args = self._setup(monkeypatch, lambda _e: {"c"}, {"c": 3.0})
        assert zs.select_edges(*args)[5] == set(), "a partial road is what this forbids"

    def test_per_edge_mode_takes_the_partial_road(self, monkeypatch):
        """The same budget under the legacy path leaves a hole — this is the
        behaviour the change replaces, kept behind FW_LZ_CORRIDOR_SELECT=0 so
        the two can be compared on one run."""
        monkeypatch.setattr(zs, "CORRIDOR_SELECTION", False)
        args = self._setup(monkeypatch, lambda _e: {"c"}, {"c": 3.0})
        got = zs.select_edges(*args)[5]
        assert 0 < len(got) < 5


class TestOverdraft:
    """Strict all-or-nothing refuses a long road for clipping one full cell.

    Measured: at 0.00 Wyoming loses a third of its z5 length and 45% of its
    named routes, because in a sparse state the few long routes ARE the map.
    """

    def _road(self, monkeypatch, full_cells: set[int]):
        """A 10 km road, one 1 km edge per cell; `full_cells` have no budget."""
        g = _graph(*[_edge(i, i, i + 1, km=1.0, ref="US 20") for i in range(1, 11)])
        monkeypatch.setattr(zs, "HAS_H3", True)
        monkeypatch.setattr(zs, "_edge_to_cells", lambda e: {f"c{e.edge_id}"})
        monkeypatch.setattr(zs, "CORRIDOR_SELECTION", True)
        scores = {z: {i: 0.9 for i in range(1, 11)} for z in range(2, 8)}
        budgets = {
            z: {
                f"c{i}": {"budget_km": 0.0 if i in full_cells else 50.0,
                          "density_factor": 1.0, "road_km": 1.0, "anchor_count": 0}
                for i in range(1, 11)
            }
            for z in range(2, 8)
        }
        return g, scores, budgets, {z: [] for z in range(2, 8)}

    def test_one_saturated_cell_must_not_refuse_the_whole_road(self, monkeypatch):
        args = self._road(monkeypatch, full_cells={4})   # 10% unaffordable
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.0)
        assert zs.select_edges(*args)[5] == set()
        args = self._road(monkeypatch, full_cells={4})
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.25)
        assert len(zs.select_edges(*args)[5]) == 10

    def test_a_mostly_unaffordable_road_is_still_refused(self, monkeypatch):
        args = self._road(monkeypatch, full_cells={1, 2, 3, 4, 5, 6})  # 60%
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.25)
        assert zs.select_edges(*args)[5] == set()

    def test_the_overdraft_is_still_CHARGED(self, monkeypatch):
        """An admitted overdraft must be visible to everything selected after
        it, or the budget stops meaning anything for the rest of the zoom."""
        args = self._road(monkeypatch, full_cells={4})
        monkeypatch.setattr(zs, "CORRIDOR_OVERDRAFT", 0.25)
        g, scores, budgets, anchors = args
        # a second road sharing the saturated cell must not also get in
        extra = _edge(99, 400, 401, km=1.0, ref="NY 9")
        g.add_edge(extra)
        for z in scores:
            scores[z][99] = 0.1
        monkeypatch.setattr(
            zs, "_edge_to_cells", lambda e: {"c4"} if e.edge_id == 99 else {f"c{e.edge_id}"}
        )
        assert 99 not in zs.select_edges(g, scores, budgets, anchors)[5]


class TestTheRecipeFingerprintSeesIt:
    def test_changing_the_selection_unit_changes_the_fingerprint(self, monkeypatch):
        """⚠️ The fingerprint hashes CONSTANTS, so a behaviour change is
        invisible to it and every cached layer comes back with the old one.
        Third time in a month (basemap_fingerprint, page_fingerprint, prune)."""
        from osm_geocoder.handlers.roads import zoom_builder as zb

        before = zb.recipe_fingerprint()
        monkeypatch.setattr(zs, "CORRIDOR_SELECTION", False)
        assert zb.recipe_fingerprint() != before
