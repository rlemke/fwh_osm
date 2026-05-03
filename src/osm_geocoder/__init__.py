"""OSM geocoder example package — Facetwork workflows + handlers for
OpenStreetMap data extraction, PostGIS imports, routing graphs (GraphHopper,
Valhalla, OSRM, pgRouting), and Folium HTML map rendering.

Discovered by the Facetwork runner via the ``facetwork.examples`` entry
point declared in ``pyproject.toml``::

    [project.entry-points."facetwork.examples"]
    osm-geocoder = "osm_geocoder:example"

Once ``pip install -e .`` has been run from this repository, Facetwork's
``scripts/start-runner --example osm-geocoder`` and
``scripts/seed-examples`` will pick this package up automatically — no
edits to the Facetwork repository required.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.examples import ExamplePackage

from .handlers import register_all_registry_handlers

# Long execution timeouts: PostGIS imports for large regions (e.g. California
# 1.2GB PBF) can take hours. Heartbeats fire during the osmium scan but cannot
# fire during blocking PostgreSQL UPSERT calls, so the timeout must
# accommodate the longest possible single-batch DB write.
_RUNNER_ENV = {
    "AFL_TASK_EXECUTION_TIMEOUT_MS": "14400000",  # 4 hours
    "AFL_STUCK_TIMEOUT_MS": "14400000",
}

example = ExamplePackage(
    name="osm-geocoder",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
    runner_env=_RUNNER_ENV,
)
