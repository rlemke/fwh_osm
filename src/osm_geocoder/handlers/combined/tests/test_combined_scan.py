"""Tests for the combined single-pass OSM scanner.

Tests plugin base, combined handler dispatch, and end-to-end scanning
using mock pyosmium objects.
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from ..plugin_base import ElementType, TagInterest

# ── TagInterest tests ────────────────────────────────────────────────


class TestTagInterest:
    def test_matches_by_key(self):
        ti = TagInterest(keys={"amenity", "shop"})
        assert ti.matches({"amenity": "restaurant"})
        assert ti.matches({"shop": "supermarket"})
        assert not ti.matches({"highway": "primary"})

    def test_matches_by_key_value(self):
        ti = TagInterest(key_values={"highway": {"cycleway", "path"}})
        assert ti.matches({"highway": "cycleway"})
        assert ti.matches({"highway": "path"})
        assert not ti.matches({"highway": "primary"})
        assert not ti.matches({"amenity": "cafe"})

    def test_matches_combined(self):
        ti = TagInterest(
            keys={"amenity"},
            key_values={"highway": {"cycleway"}},
        )
        assert ti.matches({"amenity": "cafe"})
        assert ti.matches({"highway": "cycleway"})
        assert not ti.matches({"highway": "primary"})

    def test_empty_no_match(self):
        ti = TagInterest()
        assert not ti.matches({"amenity": "cafe"})


# ── ElementType flag tests ───────────────────────────────────────────


class TestElementType:
    def test_combined_flags(self):
        combined = ElementType.NODE | ElementType.WAY
        assert ElementType.NODE in combined
        assert ElementType.WAY in combined
        assert ElementType.AREA not in combined

    def test_single_flag(self):
        assert ElementType.NODE in ElementType.NODE
        assert ElementType.WAY not in ElementType.NODE


# ── Plugin instantiation tests ───────────────────────────────────────


class TestPluginRegistry:
    def test_all_plugins_importable(self):
        from ..combined_handler import _build_plugin_registry

        registry = _build_plugin_registry()
        assert "amenities" in registry
        assert "population" in registry
        assert "roads" in registry
        assert "routes" in registry
        assert "parks" in registry
        assert "buildings" in registry
        assert "boundaries" in registry
        assert len(registry) == 7

    def test_plugins_implement_interface(self):
        from ..combined_handler import _build_plugin_registry

        registry = _build_plugin_registry()
        for name, cls in registry.items():
            plugin = cls()
            # Use class name check to avoid isinstance failures from
            # conftest sys.modules purging causing reimport
            base_names = [b.__name__ for b in type(plugin).__mro__]
            assert "ExtractorPlugin" in base_names
            assert plugin.category == name
            assert type(plugin.element_types).__name__ == "ElementType"
            assert type(plugin.tag_interest).__name__ == "TagInterest"


# ── Amenity plugin unit tests ────────────────────────────────────────


class TestAmenityPlugin:
    def test_processes_amenity_node(self):
        from ..plugins.amenity_plugin import AmenityPlugin

        plugin = AmenityPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(1, {"amenity": "restaurant", "name": "Pizza Place"}, -86.5, 34.7)
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["name"] == "Pizza Place"
            assert geojson["features"][0]["geometry"]["coordinates"] == [-86.5, 34.7]

    def test_ignores_non_amenity(self):
        from ..plugins.amenity_plugin import AmenityPlugin

        plugin = AmenityPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(1, {"highway": "primary"}, 0, 0)
            assert plugin._writer.feature_count == 0

    def test_finalize_writes_geojson(self):
        from ..plugins.amenity_plugin import AmenityPlugin

        plugin = AmenityPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(1, {"amenity": "cafe", "name": "Coffee"}, -86.5, 34.7)
            result = plugin.finalize("test-pbf", tmpdir)
            assert result.feature_count == 1
            assert os.path.exists(result.output_path)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["type"] == "FeatureCollection"
            assert len(geojson["features"]) == 1


# ── Population plugin unit tests ─────────────────────────────────────


class TestPopulationPlugin:
    def test_processes_city_node(self):
        from ..plugins.population_plugin import PopulationPlugin

        plugin = PopulationPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(
                1,
                {"place": "city", "name": "Birmingham", "population": "200000"},
                -86.8,
                33.5,
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["population"] == 200000
            assert geojson["features"][0]["properties"]["place"] == "city"

    def test_ignores_non_place(self):
        from ..plugins.population_plugin import PopulationPlugin

        plugin = PopulationPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(1, {"amenity": "school"}, 0, 0)
            assert plugin._writer.feature_count == 0

    def test_handles_missing_population(self):
        from ..plugins.population_plugin import PopulationPlugin

        plugin = PopulationPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_node(1, {"place": "village", "name": "Tiny"}, 0, 0)
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["population"] == 0


# ── Road plugin unit tests ───────────────────────────────────────────


class TestRoadPlugin:
    def test_processes_highway_way(self):
        from ..plugins.road_plugin import RoadPlugin

        plugin = RoadPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            coords = [(-86.5, 34.7), (-86.6, 34.8)]
            plugin.process_way(
                1, {"highway": "primary", "name": "Main St", "maxspeed": "45 mph"}, coords
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            props = geojson["features"][0]["properties"]
            assert props["road_class"] == "primary"
            assert props["maxspeed"] == 72  # 45 mph → ~72 km/h
            assert props["length_km"] > 0

    def test_ignores_non_highway(self):
        from ..plugins.road_plugin import RoadPlugin

        plugin = RoadPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_way(1, {"building": "yes"}, [(0, 0), (1, 1)])
            assert plugin._writer.feature_count == 0

    def test_requires_min_coords(self):
        from ..plugins.road_plugin import RoadPlugin

        plugin = RoadPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_way(1, {"highway": "primary"}, [(0, 0)])
            assert plugin._writer.feature_count == 0


# ── Park plugin unit tests ───────────────────────────────────────────


class TestParkPlugin:
    def test_processes_national_park(self):
        from ..plugins.park_plugin import ParkPlugin

        plugin = ParkPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(
                1,
                {"boundary": "national_park", "name": "Yellowstone"},
                None,  # no geometry in unit test
                100,
                False,
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["park_type"] == "national"

    def test_ignores_non_park(self):
        from ..plugins.park_plugin import ParkPlugin

        plugin = ParkPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(1, {"building": "house"}, None, 100, True)
            assert plugin._writer.feature_count == 0


# ── Building plugin unit tests ───────────────────────────────────────


class TestBuildingPlugin:
    def test_processes_building(self):
        from ..plugins.building_plugin import BuildingPlugin

        plugin = BuildingPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(
                1,
                {"building": "residential", "name": "Home", "building:levels": "2"},
                None,
                100,
                True,
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["building_type"] == "residential"
            assert geojson["features"][0]["properties"]["levels"] == 2

    def test_ignores_non_building(self):
        from ..plugins.building_plugin import BuildingPlugin

        plugin = BuildingPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(1, {"leisure": "park"}, None, 100, True)
            assert plugin._writer.feature_count == 0


# ── Boundary plugin unit tests ───────────────────────────────────────


class TestBoundaryPlugin:
    def test_processes_admin_boundary(self):
        from ..plugins.boundary_plugin import BoundaryPlugin

        plugin = BoundaryPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(
                1,
                {"boundary": "administrative", "admin_level": "4", "name": "Alabama"},
                None,
                100,
                False,
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["admin_type"] == "state"

    def test_ignores_non_admin_levels(self):
        from ..plugins.boundary_plugin import BoundaryPlugin

        plugin = BoundaryPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            # admin_level 10 is not in ADMIN_LEVELS
            plugin.process_area(
                1,
                {"boundary": "administrative", "admin_level": "10"},
                None,
                100,
                False,
            )
            assert plugin._writer.feature_count == 0

    def test_processes_water(self):
        from ..plugins.boundary_plugin import BoundaryPlugin

        plugin = BoundaryPlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_area(
                1,
                {"natural": "water", "name": "Lake Test"},
                None,
                100,
                True,
            )
            assert plugin._writer.feature_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert geojson["features"][0]["properties"]["natural_type"] == "water"


# ── Route plugin unit tests ──────────────────────────────────────────


class TestRoutePlugin:
    def test_processes_cycleway(self):
        from ..plugins.route_plugin import RoutePlugin

        plugin = RoutePlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            coords = [(-86.5, 34.7), (-86.6, 34.8)]
            plugin.process_way(1, {"highway": "cycleway", "name": "Bike Path"}, coords)
            assert plugin._way_count == 1
            result = plugin.finalize("test-pbf", tmpdir)
            with open(result.output_path) as f:
                geojson = json.load(f)
            assert "bicycle" in geojson["features"][0]["properties"]["route_types"]

    def test_processes_route_relation(self):
        from ..plugins.route_plugin import RoutePlugin

        plugin = RoutePlugin()
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin.begin("test-pbf", tmpdir)
            plugin.process_relation(
                1,
                {"route": "hiking", "name": "Appalachian Trail"},
                [{"type": "w", "ref": 100, "role": ""}],
            )
            assert plugin._relation_count == 1


# ── Combined handler dispatch tests ──────────────────────────────────


class TestCombinedHandler:
    def test_handler_creation(self):
        """Test that _CombinedHandler can be created with plugins."""
        from ..plugins.amenity_plugin import AmenityPlugin
        from ..plugins.road_plugin import RoadPlugin

        try:
            from ..combined_handler import _CombinedHandler

            handler = _CombinedHandler([AmenityPlugin(), RoadPlugin()])
            assert len(handler._node_plugins) == 1  # amenity
            assert len(handler._way_plugins) == 1  # road
        except TypeError:
            # osmium not available — handler is plain object
            pytest.skip("pyosmium not available")


# ── Handler registration tests ───────────────────────────────────────


class TestHandlerRegistration:
    def test_register_combined_handlers(self):
        from ..combined_handlers import register_combined_handlers

        poller = MagicMock()
        register_combined_handlers(poller)
        # Should register both CombinedScan and ExtractCategoryResult
        if poller.register.called:
            registered = [call[0][0] for call in poller.register.call_args_list]
            assert "osm.Combined.CombinedScan" in registered
            assert "osm.Combined.ExtractCategoryResult" in registered

    def test_handler_empty_result(self):
        from ..combined_handlers import _handler

        result = _handler({"cache": {}, "categories": []})
        assert result["total_features"] == 0
        assert result["results"] == "{}"

    def test_handler_string_categories(self):
        """Categories can be comma-separated string."""
        from ..combined_handlers import _handler

        result = _handler({"cache": {}, "categories": "roads,parks"})
        assert result["total_features"] == 0

    def test_dispatch_both_facets(self):
        """handle() dispatches to correct handler based on _facet_name."""
        from ..combined_handlers import handle

        # CombinedScan dispatch
        result = handle({"_facet_name": "osm.Combined.CombinedScan", "cache": {}, "categories": []})
        assert result["total_features"] == 0

        # ExtractCategoryResult dispatch
        result = handle(
            {
                "_facet_name": "osm.Combined.ExtractCategoryResult",
                "results": "{}",
                "category": "roads",
            }
        )
        assert result["output_path"] == ""

    def test_dispatch_unknown_raises(self):
        from ..combined_handlers import handle

        with pytest.raises(ValueError, match="Unknown facet"):
            handle({"_facet_name": "osm.Combined.Fake"})

    def test_register_registry_handlers(self):
        from ..combined_handlers import register_handlers

        runner = MagicMock()
        register_handlers(runner)
        assert runner.register_handler.call_count == 2


# ── ExtractCategoryResult tests ──────────────────────────────────────


class TestExtractCategoryResult:
    def test_extracts_category(self):
        import json

        from ..combined_handlers import _extract_handler

        results = json.dumps(
            {
                "roads": {
                    "output_path": "/tmp/roads.geojson",
                    "feature_count": 42,
                    "metadata": {},
                    "error": None,
                },
                "parks": {
                    "output_path": "/tmp/parks.geojson",
                    "feature_count": 10,
                    "metadata": {},
                    "error": None,
                },
            }
        )
        result = _extract_handler({"results": results, "category": "roads"})
        assert result["output_path"] == "/tmp/roads.geojson"
        assert result["feature_count"] == 42

    def test_missing_category(self):
        from ..combined_handlers import _extract_handler

        result = _extract_handler({"results": "{}", "category": "nonexistent"})
        assert result["output_path"] == ""
        assert result["feature_count"] == 0

    def test_invalid_json(self):
        from ..combined_handlers import _extract_handler

        result = _extract_handler({"results": "not-json", "category": "roads"})
        assert result["output_path"] == ""
        assert result["feature_count"] == 0

    def test_dict_input(self):
        """Results can be passed as dict (not just string)."""
        from ..combined_handlers import _extract_handler

        results = {"parks": {"output_path": "/tmp/p.geojson", "feature_count": 5}}
        result = _extract_handler({"results": results, "category": "parks"})
        assert result["output_path"] == "/tmp/p.geojson"
        assert result["feature_count"] == 5

    def test_empty_results(self):
        from ..combined_handlers import _extract_handler

        result = _extract_handler({"results": "", "category": "roads"})
        assert result["output_path"] == ""


# ── CombinedScanResult tests ────────────────────────────────────────


class TestCombinedScanResult:
    def test_dataclass(self):
        from ..combined_handler import CombinedScanResult

        r = CombinedScanResult(
            pbf_path="/tmp/test.pbf",
            categories=["roads"],
            total_features=42,
            scan_duration_seconds=1.5,
        )
        assert r.total_features == 42
        assert r.categories == ["roads"]

    def test_unknown_category_raises(self):
        from .. import combined_handler
        from ..combined_handler import combined_scan

        # Ensure HAS_OSMIUM is True so we get past the guard
        orig = combined_handler.HAS_OSMIUM
        combined_handler.HAS_OSMIUM = True
        try:
            with pytest.raises(ValueError, match="Unknown categories"):
                combined_scan("/tmp/fake.pbf", ["nonexistent_plugin"])
        finally:
            combined_handler.HAS_OSMIUM = orig


# ── GeoJSON streaming writer tests ──────────────────────────────────


class TestGeoJSONStreamWriter:
    def test_empty_collection(self):
        from ...shared.geojson_writer import GeoJSONStreamWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/empty.geojson"
            with GeoJSONStreamWriter(path) as w:
                pass
            assert w.feature_count == 0
            with open(path) as f:
                geojson = json.load(f)
            assert geojson == {"type": "FeatureCollection", "features": []}

    def test_single_feature(self):
        from ...shared.geojson_writer import GeoJSONStreamWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/one.geojson"
            feature = {"type": "Feature", "properties": {"a": 1}, "geometry": None}
            with GeoJSONStreamWriter(path) as w:
                w.write_feature(feature)
            assert w.feature_count == 1
            with open(path) as f:
                geojson = json.load(f)
            assert len(geojson["features"]) == 1
            assert geojson["features"][0]["properties"]["a"] == 1

    def test_multiple_features(self):
        from ...shared.geojson_writer import GeoJSONStreamWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/multi.geojson"
            with GeoJSONStreamWriter(path) as w:
                for i in range(100):
                    w.write_feature({"type": "Feature", "properties": {"idx": i}, "geometry": None})
            assert w.feature_count == 100
            with open(path) as f:
                geojson = json.load(f)
            assert len(geojson["features"]) == 100
            assert geojson["features"][99]["properties"]["idx"] == 99

    def test_write_after_close_raises(self):
        from ...shared.geojson_writer import GeoJSONStreamWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/closed.geojson"
            w = GeoJSONStreamWriter(path)
            w.close()
            with pytest.raises(RuntimeError, match="closed"):
                w.write_feature({"type": "Feature"})

    def test_double_close_safe(self):
        from ...shared.geojson_writer import GeoJSONStreamWriter

        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/double.geojson"
            w = GeoJSONStreamWriter(path)
            w.close()
            w.close()  # should not raise
