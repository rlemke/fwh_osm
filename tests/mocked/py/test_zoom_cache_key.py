"""The zoom builder's output cache must key on everything that changes output.

It keyed on `min_population` alone. Measured repeatedly 2026-09-23/24: a run
into a NEW output_base came back with the PREVIOUS run's `output_dir` and file
paths, and every algorithm change had to be forced through by bumping
`min_population` by 1. A hollow result is indistinguishable from a correct one
to a key that cannot see the difference between them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_selection as zs  # noqa: E402
from osm_geocoder.handlers.roads.zoom_builder import recipe_fingerprint  # noqa: E402


def test_fingerprint_is_stable_across_calls():
    assert recipe_fingerprint() == recipe_fingerprint()


def test_retuning_the_class_ladder_changes_the_fingerprint(monkeypatch):
    before = recipe_fingerprint()
    monkeypatch.setitem(zs.MIN_FC_BY_ZOOM, 2, "primary")
    assert recipe_fingerprint() != before, "a ladder change must invalidate the cache"


def test_changing_the_skeleton_changes_the_fingerprint(monkeypatch):
    before = recipe_fingerprint()
    monkeypatch.setattr(zs, "SKELETON_FCS", frozenset({"motorway"}))
    assert recipe_fingerprint() != before


def test_changing_score_weights_or_budgets_changes_the_fingerprint(monkeypatch):
    before = recipe_fingerprint()
    # Derive the perturbation from the CURRENT value — a literal can silently
    # become a no-op when the constant is retuned to match it, which is exactly
    # what happened when BASE_KM[2] was recalibrated to 1.0.
    monkeypatch.setitem(zs.W_SB, 2, zs.W_SB[2] + 0.1)
    assert recipe_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setitem(zs.BASE_KM, 2, zs.BASE_KM[2] + 1.0)
    assert recipe_fingerprint() != before


def test_the_handler_keys_on_destination_and_graph_too():
    src = (Path(__file__).resolve().parents[3]
           / "src/osm_geocoder/handlers/roads/zoom_handlers.py").read_text()
    for field in ('"output_dir": output_dir', '"graph_dir"', '"profile"', '"recipe"'):
        assert field in src, f"cache key is missing {field}"
    assert 'cached_result(qualified, cache, {"min_population": min_population}' not in src
