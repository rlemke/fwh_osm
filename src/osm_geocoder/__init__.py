"""OSM geocoder example package — Facetwork workflows + handlers for
OpenStreetMap data extraction, PostGIS imports, routing graphs (GraphHopper,
Valhalla, OSRM, pgRouting), and Folium HTML map rendering.

Discovered by the Facetwork runner via the ``facetwork.domains`` entry
point declared in ``pyproject.toml``::

    [project.entry-points."facetwork.domains"]
    osm-geocoder = "osm_geocoder:domain"

Once ``pip install -e .`` has been run from this repository, Facetwork's
``fw runner start --domain osm-geocoder`` and
``fw ffl seed`` will pick this package up automatically — no
edits to the Facetwork repository required.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

# Long execution timeouts: PostGIS imports for large regions (e.g. California
# 1.2GB PBF) can take hours. Heartbeats fire during the osmium scan but cannot
# fire during blocking PostgreSQL UPSERT calls, so the timeout must
# accommodate the longest possible single-batch DB write.
_RUNNER_ENV = {
    "FW_TASK_EXECUTION_TIMEOUT_MS": "14400000",  # 4 hours
    "FW_STUCK_TIMEOUT_MS": "14400000",
}

# Integration-test FFL fixtures live at the repo root (outside src/) so they
# stay grouped with their pytest scripts. Surface them through the discovery
# API so `fw ffl seed` and friends pick them up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXTRA_FFL = [_REPO_ROOT / "tests" / "real" / "ffl"]

domain = DomainPackage(
    name="osm-geocoder",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
    runner_env=_RUNNER_ENV,
    extra_ffl_dirs=_EXTRA_FFL,
)
