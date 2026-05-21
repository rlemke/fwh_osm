"""Tests for OSM handler dispatch adapter pattern.

Verifies that each OSM handler module's handle() function dispatches correctly
using the _facet_name key, that _DISPATCH dicts have the expected keys,
and that register_handlers() calls runner.register_handler the expected
number of times.
"""

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import osm_geocoder.handlers as _H

# .../src/osm_geocoder/handlers
_HROOT = Path(_H.__file__).resolve().parent
# .../src  (parents of the osm_geocoder package directory)
_SRC = _HROOT.parent.parent


def _osm_import(module_name: str):
    """Import an OSM handlers submodule by locating it under the package.

    Modules now live under ``osm_geocoder/handlers/<subpkg>/<module>.py``.
    Resolve the dotted path by filesystem search so the test does not need
    to hard-code each subpackage.
    """
    if module_name == "__init__":
        return importlib.import_module("osm_geocoder.handlers")
    hits = [p for p in _HROOT.rglob(f"{module_name}.py") if "tests" not in p.parts]
    if not hits:
        raise ImportError(f"no osm handler module {module_name!r}")
    dotted = ".".join(hits[0].relative_to(_SRC).with_suffix("").parts)
    return importlib.import_module(dotted)


class TestOsmParkHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("park_handlers")
        assert len(mod._DISPATCH) > 0
        for key in mod._DISPATCH:
            assert key.startswith("osm.Parks.")

    def test_handle_dispatches(self):
        mod = _osm_import("park_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_handle_unknown_facet(self):
        mod = _osm_import("park_handlers")
        with pytest.raises(ValueError, match="Unknown facet"):
            mod.handle({"_facet_name": "osm.Parks.NonExistent"})

    def test_register_handlers(self):
        mod = _osm_import("park_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmAmenityHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("amenity_handlers")
        assert len(mod._DISPATCH) > 0
        for key in mod._DISPATCH:
            assert key.startswith("osm.Amenities.")

    def test_handle_dispatches(self):
        mod = _osm_import("amenity_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("amenity_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmFilterHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("filter_handlers")
        assert len(mod._DISPATCH) > 0
        for key in mod._DISPATCH:
            assert key.startswith("osm.Filters.")

    def test_handle_dispatches(self):
        mod = _osm_import("filter_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("filter_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmRegionHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("region_handlers")
        # Post cross-region migration: ListRegions, ResolveRegion,
        # ResolveRegions, osm.cache.Download, osm.ops.CacheRegion.
        assert len(mod._DISPATCH) == 5
        assert "osm.Region.ResolveRegion" in mod._DISPATCH

    def test_handle_dispatches(self):
        mod = _osm_import("region_handlers")
        result = mod.handle({"_facet_name": "osm.Region.ListRegions"})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("region_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmElevationHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("elevation_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("elevation_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmRoutingHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("routing_handlers")
        assert len(mod._DISPATCH) == 1
        assert "osm.Routing.ComputePairwiseRoutes" in mod._DISPATCH

    def test_register_handlers(self):
        mod = _osm_import("routing_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == 1


class TestOsmOsmoseHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("osmose_handlers")
        assert len(mod._DISPATCH) == 5

    def test_handle_dispatches(self):
        mod = _osm_import("osmose_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("osmose_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmValidationHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("validation_handlers")
        assert len(mod._DISPATCH) == 5

    def test_handle_dispatches(self):
        mod = _osm_import("validation_handlers")
        result = mod.handle({"_facet_name": "osm.ops.Validation.ValidateCache"})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("validation_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmAirqualityHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("airquality_handlers")
        assert len(mod._DISPATCH) == 3

    def test_handle_dispatches(self):
        mod = _osm_import("airquality_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("airquality_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmPoiHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("poi_handlers")
        assert len(mod._DISPATCH) > 0

    def test_handle_dispatches(self):
        mod = _osm_import("poi_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("poi_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmGraphhopperHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("graphhopper_handlers")
        assert len(mod._DISPATCH) == 6
        for key in mod._DISPATCH:
            assert key.startswith("osm.ops.GraphHopper.")

    def test_register_handlers(self):
        mod = _osm_import("graphhopper_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == 6


class TestOsmTigerHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("tiger_handlers")
        assert len(mod._DISPATCH) > 0

    def test_handle_dispatches(self):
        mod = _osm_import("tiger_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("tiger_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmBoundaryHandlers:
    def test_dispatch_keys(self):
        """Boundary extraction moved to CombinedScan — _DISPATCH is empty."""
        mod = _osm_import("boundary_handlers")
        assert len(mod._DISPATCH) == 0

    def test_handle_raises_for_unknown(self):
        mod = _osm_import("boundary_handlers")
        with pytest.raises(ValueError, match="Unknown facet"):
            mod.handle({"_facet_name": "osm.Boundaries.Fake"})

    def test_register_handlers(self):
        mod = _osm_import("boundary_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == 0


class TestOsmPopulationHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("population_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("population_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmRoadHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("road_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("road_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmRouteHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("route_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("route_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmBuildingHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("building_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("building_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmVisualizationHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("visualization_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("visualization_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmGtfsHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("gtfs_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("gtfs_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmZoomHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("zoom_handlers")
        assert len(mod._DISPATCH) > 0

    def test_register_handlers(self):
        mod = _osm_import("zoom_handlers")
        runner = MagicMock()
        mod.register_handlers(runner)
        assert runner.register_handler.call_count == len(mod._DISPATCH)


class TestOsmInitRegistryHandlers:
    def test_register_all_registry_handlers(self):
        mod = _osm_import("__init__")
        runner = MagicMock()
        mod.register_all_registry_handlers(runner)
        # Only event facet handlers remain (no cache/graphhopper cache registrations)
        assert runner.register_handler.call_count > 50
