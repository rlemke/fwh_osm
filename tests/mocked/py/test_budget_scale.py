"""Cell budgets must be on the scale of the cell they are spent in.

`BASE_KM` was 80–900 km per H3 resolution-7 hexagon (~5.2 km²) — 15 to 170 km
of road per 5 km², denser than Manhattan. Measured 2026-09-24 on Washington,
whose 23,752 cells hold a MEDIAN of 2.7 km of road: the budget bound in 30
cells at z2 and in ZERO cells from z4 down. The mechanism was inert.

It went unnoticed because `h3` was not installed, so `build_cell_budgets` fell
back to one flat cell for the whole region — and THAT single statewide budget
is what actually limited selection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_selection as zs  # noqa: E402

H3_RES7_AREA_KM2 = 5.16


def test_budgets_are_plausible_road_density_for_the_cell():
    """A budget implies a road density. Above ~8 km/km² it cannot bind on any
    real region, which makes it decoration rather than a constraint."""
    for z, km in zs.BASE_KM.items():
        density = km / H3_RES7_AREA_KM2
        assert density <= 8.0, (
            f"zoom {z}: {km} km per {H3_RES7_AREA_KM2} km² cell is "
            f"{density:.0f} km/km² — denser than any real road network")


def test_budgets_increase_with_zoom():
    zooms = sorted(zs.BASE_KM)
    assert all(zs.BASE_KM[a] < zs.BASE_KM[b] for a, b in zip(zooms, zooms[1:]))


def test_the_sparse_floor_never_exceeds_the_budget():
    """A floor above the budget forces every cell to its limit, which is how
    low-class roads reached continental zooms."""
    for z, floor in zs.MIN_KM.items():
        assert floor < zs.BASE_KM[z], f"zoom {z}: floor {floor} >= budget {zs.BASE_KM[z]}"


def test_h3_is_a_declared_dependency():
    """Without it `build_cell_budgets` silently degrades to ONE cell for the
    whole region and the per-cell mechanism does not run."""
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text()
    assert '"h3' in pyproject, "h3 must be declared, not optional-by-accident"
