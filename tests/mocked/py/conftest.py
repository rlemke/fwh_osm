"""Conftest for OSM geocoder mocked tests.

Handler imports are fully package-qualified (``osm_geocoder.handlers.*``),
so no ``sys.path`` aliasing or stale-module purge is required.  Kept as a
no-op to avoid shadowing the real package modules.
"""
