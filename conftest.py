"""Root conftest for the osm-geocoder example package.

Historically this file purged a bare top-level ``handlers`` package from
``sys.modules`` and prepended the example root to ``sys.path`` so that
``from handlers.xxx import ...`` resolved to this example's handlers.

After the extraction into the standalone ``osm_geocoder`` package, all
handler imports (and mock-patch targets) are fully package-qualified
(``osm_geocoder.handlers.<subpkg>.<module>``), so the sys.path alias and the
stale-module purge are no longer needed.  They are intentionally left as a
no-op to avoid shadowing or purging the real ``osm_geocoder.handlers.*``
modules.

This file also registers the ``--mongodb`` and ``--hdfs`` opt-in flags that
gate the live integration tests under ``tests/real/``. Registering them here
(rather than in ``tests/real/py/conftest.py``) means the options always exist
during collection, so the HDFS module's ``skipif("not
config.getoption('--hdfs')")`` marker resolves cleanly even when neither flag
is passed — the live tests simply skip. The actual skip logic still lives in
``tests/real/py/conftest.py`` (``--mongodb``) and in the HDFS test module's
``pytestmark`` (``--hdfs``).
"""


def pytest_addoption(parser):
    """Register the opt-in flags for the live integration suites.

    Both default to off so the offline run (no flags) collects and skips the
    ``tests/real/`` suites without error.
    """
    group = parser.getgroup("osm-geocoder integration")
    group.addoption(
        "--mongodb",
        action="store_true",
        default=False,
        help="Run integration tests that require a live MongoDB server.",
    )
    group.addoption(
        "--hdfs",
        action="store_true",
        default=False,
        help="Run integration tests that require a live HDFS cluster.",
    )
