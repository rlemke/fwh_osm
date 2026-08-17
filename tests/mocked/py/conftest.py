"""Conftest for OSM geocoder mocked tests.

Handler imports are fully package-qualified (``osm_geocoder.handlers.*``),
so no ``sys.path`` aliasing or stale-module purge is required.  Kept as a
no-op to avoid shadowing the real package modules.

It does one thing: pin the data (and output) roots to a temp dir. See below.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_data_root(tmp_path_factory):
    """Point the whole mocked session at a throwaway data root.

    Without this the storage layer falls back to ``LOCAL_DEFAULT_ROOT``
    (``/Volumes/afl_data``) — the infra host's external disk. On any other
    machine that path does not exist and macOS will not let a process create it,
    so **14 tests failed with ``PermissionError: '/Volumes/afl_data'``** — not
    because anything was broken, but because the offline suite was quietly
    coupled to one machine's disk layout. A suite that only passes on the host
    with that volume mounted cannot tell a real regression from a missing disk,
    which is the state it was in.

    **Session-scoped deliberately.** A function-scoped version fixed only 6 of
    the 14: module-scoped fixtures (``synthetic_pbf``) are built before any
    function-scoped fixture runs, so they resolved storage roots while the env
    was still unset. Scope has to be at least as wide as the widest fixture that
    touches storage.

    ``FW_DATA_ROOT`` is read at call time (``storage.data_root``), so setting it
    here is enough — nothing memoises it at import.

    An explicit ``FW_DATA_ROOT`` in the environment still wins, so a deliberate
    run against a real cache (``FW_DATA_ROOT=/Volumes/afl_data_local pytest``)
    behaves as before.
    """
    if os.environ.get("FW_DATA_ROOT"):
        yield
        return
    root = tmp_path_factory.mktemp("fw-data")
    mp = pytest.MonkeyPatch()
    mp.setenv("FW_DATA_ROOT", str(root))
    mp.setenv("FW_STORAGE", "local")
    # TWO roots, not one. The OSM tools resolve their cache from FW_DATA_ROOT
    # (storage.data_root), but handlers also call facetwork.config.get_temp_dir,
    # which resolves an OUTPUT base from FW_OUTPUT_BASE / FW_LOCAL_OUTPUT_DIR and
    # defaults to /Volumes/afl_data/output — a different fallback in a different
    # package. Pinning only the first left 8 of the 14 failing, which is how this
    # second root was found.
    mp.setenv("FW_OUTPUT_BASE", str(root / "output"))
    yield
    mp.undo()
