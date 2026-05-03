"""Root conftest for osm-geocoder example.

Ensures that the osm-geocoder handlers package is active when tests in this
example tree are collected or run.  Other examples (genomics, jenkins, etc.)
may load their own ``handlers`` package first; this conftest purges those
stale entries from ``sys.modules`` so that subsequent imports resolve to the
osm-geocoder handlers.

The tricky bit is *timing*: pytest creates Module collectors for ALL test
files first, then calls ``Module.collect()`` — which imports the test
module — and later ``Package.setup()`` — which imports ``__init__.py``.
Between these events, other examples' test modules may already have been
imported, loading their own ``handlers`` package into ``sys.modules``.

To solve this, we wrap the ``importtestmodule`` function (used by both
``Module._getobj`` and ``Package.setup``) so that the stale-module purge
happens *immediately* before each osm-geocoder module is imported.
"""

import os
import sys

import _pytest.python

_EXAMPLE_ROOT = os.path.dirname(os.path.abspath(__file__))


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


# Purge at conftest load time.
_ensure_osm_handlers()

# ---------------------------------------------------------------------------
# Monkey-patch importtestmodule to purge stale handlers right before each
# osm-geocoder module is imported by pytest.
# ---------------------------------------------------------------------------
_original_importtestmodule = _pytest.python.importtestmodule


def _patched_importtestmodule(path, config):
    if "osm-geocoder" in str(path):
        _ensure_osm_handlers()
    return _original_importtestmodule(path, config)


_pytest.python.importtestmodule = _patched_importtestmodule
