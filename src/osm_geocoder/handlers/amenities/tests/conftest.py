"""Conftest for OSM geocoder category tests.

Adds the osm-geocoder example root to sys.path so that
``from handlers.xxx import ...`` works from category test locations.
"""

import os
import sys

import pytest

# handlers/{cat}/tests/ → examples/osm-geocoder/
_EXAMPLE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _ensure_osm_handlers():
    """Purge stale handlers and ensure osm-geocoder handlers are active."""
    for key in list(sys.modules.keys()):
        if key == "handlers" or key.startswith("handlers."):
            mod = sys.modules[key]
            mod_file = getattr(mod, "__file__", "") or ""
            if "osm-geocoder" not in mod_file:
                del sys.modules[key]
    if _EXAMPLE_ROOT in sys.path:
        sys.path.remove(_EXAMPLE_ROOT)
    sys.path.insert(0, _EXAMPLE_ROOT)


# Purge at collection time so module-level imports in test files work
_ensure_osm_handlers()


@pytest.fixture(autouse=True)
def _osm_handlers_on_path():
    """Ensure osm-geocoder handlers are on sys.path before each test."""
    _ensure_osm_handlers()
