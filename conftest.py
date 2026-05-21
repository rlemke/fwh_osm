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
"""
