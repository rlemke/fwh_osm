"""Tests for OSM handler dispatch adapter pattern.

Verifies that each OSM handler module's handle() function dispatches correctly
using the _facet_name key, that _DISPATCH dicts have the expected keys,
and that register_handlers() calls runner.register_handler the expected
number of times.
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

OSM_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def _osm_import(module_name: str):
    """Import an OSM handlers submodule, ensuring correct sys.path."""
    # Make sure OSM dir is first on path
    if OSM_DIR in sys.path:
        sys.path.remove(OSM_DIR)
    sys.path.insert(0, OSM_DIR)

    full_name = f"handlers.{module_name}"

    # If module is already loaded from the right location, return it
    if full_name in sys.modules:
        mod = sys.modules[full_name]
        mod_file = getattr(mod, "__file__", "")
        if mod_file and "osm-geocoder" in mod_file:
            return mod
        # Wrong location, need to reload
        del sys.modules[full_name]

    # Ensure the handlers package itself is from OSM
    if "handlers" in sys.modules:
        pkg = sys.modules["handlers"]
        pkg_file = getattr(pkg, "__file__", "")
        if pkg_file and "osm-geocoder" not in pkg_file:
            # Wrong package, clear all handlers modules
            stale = [k for k in sys.modules if k == "handlers" or k.startswith("handlers.")]
            for k in stale:
                del sys.modules[k]

    return importlib.import_module(full_name)


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
        assert len(mod._DISPATCH) == 3
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


class TestOsmOperationsHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("operations_handlers")
        assert len(mod._DISPATCH) > 0

    def test_handle_dispatches(self):
        mod = _osm_import("operations_handlers")
        facet = next(iter(mod._DISPATCH))
        mock_cache = {
            "url": "https://example.com/test.osm.pbf",
            "path": "/tmp/test.osm.pbf",
            "date": "2026-01-01",
            "size": 100,
            "wasInCache": True,
            "source": "cache",
        }
        with patch(
            "handlers.shared.pbf_cache.download_region",
            side_effect=lambda region, **_: type(
                "R",
                (),
                {
                    "region": region,
                    "path": mock_cache["path"],
                    "relative_path": f"{region}-latest.osm.pbf",
                    "source_url": mock_cache["url"],
                    "size_bytes": mock_cache["size"],
                    "sha256": "",
                    "md5": "",
                    "source_timestamp": None,
                    "downloaded_at": mock_cache["date"],
                    "was_cached": mock_cache["wasInCache"],
                    "manifest_entry": {},
                },
            )(),
        ):
            result = mod.handle({"_facet_name": facet, "region": "TestRegion"})
        assert isinstance(result, dict)

    def test_register_handlers(self):
        mod = _osm_import("operations_handlers")
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


class TestOsmPostgisHandlers:
    def test_dispatch_keys(self):
        mod = _osm_import("postgis_handlers")
        assert len(mod._DISPATCH) > 0
        for key in mod._DISPATCH:
            assert key.startswith("osm.ops.")

    def test_handle_dispatches(self):
        mod = _osm_import("postgis_handlers")
        facet = next(iter(mod._DISPATCH))
        result = mod.handle({"_facet_name": facet})
        assert isinstance(result, dict)
        assert "stats" in result

    def test_handle_unknown_facet(self):
        mod = _osm_import("postgis_handlers")
        with pytest.raises(ValueError, match="Unknown facet"):
            mod.handle({"_facet_name": "osm.ops.NonExistent"})

    def test_register_handlers(self):
        mod = _osm_import("postgis_handlers")
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
