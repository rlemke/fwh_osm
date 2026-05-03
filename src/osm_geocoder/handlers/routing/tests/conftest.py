"""Conftest for routing adapter tests."""

import os
import sys

import pytest

_EXAMPLE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _ensure_osm_handlers():
    for key in list(sys.modules.keys()):
        if key == "handlers" or key.startswith("handlers."):
            mod = sys.modules[key]
            mod_file = getattr(mod, "__file__", "") or ""
            if "osm-geocoder" not in mod_file:
                del sys.modules[key]
    if _EXAMPLE_ROOT in sys.path:
        sys.path.remove(_EXAMPLE_ROOT)
    sys.path.insert(0, _EXAMPLE_ROOT)


_ensure_osm_handlers()


@pytest.fixture(autouse=True)
def _osm_handlers_on_path():
    _ensure_osm_handlers()
