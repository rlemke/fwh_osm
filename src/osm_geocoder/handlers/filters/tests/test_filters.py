#!/usr/bin/env python3
"""Unit tests for the OSM radius filter module.

Run from the repo root:
    pytest examples/osm-geocoder/tests/mocked/py/test_filters.py -v
"""

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from osm_geocoder.handlers.filters.osm_type_filter import (
    HAS_OSMIUM,
    OSMElement,
    OSMType,
    _describe_osm_filter,
    filter_geojson_by_osm_type,
)
from osm_geocoder.handlers.filters.radius_filter import (
    HAS_SHAPELY,
    Operator,
    RadiusCriteria,
    Unit,
    _describe_filter,
    convert_from_meters,
    convert_to_meters,
    filter_geojson,
    matches_criteria,
    parse_criteria,
)

# Skip markers for optional dependencies
requires_shapely = pytest.mark.skipif(not HAS_SHAPELY, reason="shapely not installed")
requires_osmium = pytest.mark.skipif(not HAS_OSMIUM, reason="pyosmium not installed")


class TestUnitConversion:
    """Tests for unit conversion functions."""

    def test_meters_to_meters(self):
        assert convert_to_meters(1000, Unit.METERS) == 1000

    def test_kilometers_to_meters(self):
        assert convert_to_meters(1, Unit.KILOMETERS) == 1000
        assert convert_to_meters(5.5, Unit.KILOMETERS) == 5500

    def test_miles_to_meters(self):
        assert convert_to_meters(1, Unit.MILES) == pytest.approx(1609.344, rel=1e-6)
        assert convert_to_meters(10, Unit.MILES) == pytest.approx(16093.44, rel=1e-6)

    def test_from_meters_to_kilometers(self):
        assert convert_from_meters(1000, Unit.KILOMETERS) == 1
        assert convert_from_meters(5500, Unit.KILOMETERS) == 5.5

    def test_from_meters_to_miles(self):
        assert convert_from_meters(1609.344, Unit.MILES) == pytest.approx(1.0, rel=1e-6)

    def test_round_trip_conversion(self):
        original = 42.5
        for unit in Unit:
            meters = convert_to_meters(original, unit)
            back = convert_from_meters(meters, unit)
            assert back == pytest.approx(original, rel=1e-9)


class TestUnitParsing:
    """Tests for Unit.from_string()."""

    def test_parse_meters(self):
        assert Unit.from_string("meters") == Unit.METERS
        assert Unit.from_string("METERS") == Unit.METERS
        assert Unit.from_string("m") == Unit.METERS
        assert Unit.from_string("meter") == Unit.METERS

    def test_parse_kilometers(self):
        assert Unit.from_string("kilometers") == Unit.KILOMETERS
        assert Unit.from_string("km") == Unit.KILOMETERS
        assert Unit.from_string("kilometer") == Unit.KILOMETERS

    def test_parse_miles(self):
        assert Unit.from_string("miles") == Unit.MILES
        assert Unit.from_string("mi") == Unit.MILES
        assert Unit.from_string("mile") == Unit.MILES

    def test_parse_invalid(self):
        with pytest.raises(ValueError, match="Unknown unit"):
            Unit.from_string("furlongs")


class TestOperatorParsing:
    """Tests for Operator.from_string()."""

    def test_parse_operators(self):
        assert Operator.from_string("gt") == Operator.GT
        assert Operator.from_string(">") == Operator.GT
        assert Operator.from_string("gte") == Operator.GTE
        assert Operator.from_string(">=") == Operator.GTE
        assert Operator.from_string("lt") == Operator.LT
        assert Operator.from_string("<") == Operator.LT
        assert Operator.from_string("lte") == Operator.LTE
        assert Operator.from_string("<=") == Operator.LTE
        assert Operator.from_string("eq") == Operator.EQ
        assert Operator.from_string("=") == Operator.EQ
        assert Operator.from_string("==") == Operator.EQ
        assert Operator.from_string("ne") == Operator.NE
        assert Operator.from_string("!=") == Operator.NE
        assert Operator.from_string("<>") == Operator.NE
        assert Operator.from_string("between") == Operator.BETWEEN

    def test_parse_case_insensitive(self):
        assert Operator.from_string("GT") == Operator.GT
        assert Operator.from_string("GTE") == Operator.GTE
        assert Operator.from_string("BETWEEN") == Operator.BETWEEN

    def test_parse_invalid(self):
        with pytest.raises(ValueError, match="Unknown operator"):
            Operator.from_string("approx")


class TestMatchesCriteria:
    """Tests for the matches_criteria() function."""

    def test_greater_than(self):
        criteria = RadiusCriteria(threshold=10, unit=Unit.KILOMETERS, operator=Operator.GT)
        assert matches_criteria(15000, criteria) is True  # 15km > 10km
        assert matches_criteria(10000, criteria) is False  # 10km not > 10km
        assert matches_criteria(5000, criteria) is False  # 5km not > 10km

    def test_greater_than_or_equal(self):
        criteria = RadiusCriteria(threshold=10, unit=Unit.KILOMETERS, operator=Operator.GTE)
        assert matches_criteria(15000, criteria) is True  # 15km >= 10km
        assert matches_criteria(10000, criteria) is True  # 10km >= 10km
        assert matches_criteria(5000, criteria) is False  # 5km not >= 10km

    def test_less_than(self):
        criteria = RadiusCriteria(threshold=10, unit=Unit.KILOMETERS, operator=Operator.LT)
        assert matches_criteria(5000, criteria) is True  # 5km < 10km
        assert matches_criteria(10000, criteria) is False  # 10km not < 10km
        assert matches_criteria(15000, criteria) is False  # 15km not < 10km

    def test_less_than_or_equal(self):
        criteria = RadiusCriteria(threshold=10, unit=Unit.KILOMETERS, operator=Operator.LTE)
        assert matches_criteria(5000, criteria) is True  # 5km <= 10km
        assert matches_criteria(10000, criteria) is True  # 10km <= 10km
        assert matches_criteria(15000, criteria) is False  # 15km not <= 10km

    def test_equal_within_tolerance(self):
        criteria = RadiusCriteria(
            threshold=10, unit=Unit.KILOMETERS, operator=Operator.EQ, tolerance=0.01
        )
        assert matches_criteria(10000, criteria) is True  # Exactly 10km
        assert matches_criteria(10050, criteria) is True  # Within 1% (100m tolerance)
        assert matches_criteria(9950, criteria) is True  # Within 1%
        assert matches_criteria(10200, criteria) is False  # Outside 1%

    def test_not_equal(self):
        criteria = RadiusCriteria(
            threshold=10, unit=Unit.KILOMETERS, operator=Operator.NE, tolerance=0.01
        )
        assert matches_criteria(15000, criteria) is True  # 15km != 10km
        assert matches_criteria(5000, criteria) is True  # 5km != 10km
        assert matches_criteria(10000, criteria) is False  # Exactly 10km
        assert matches_criteria(10050, criteria) is False  # Within 1%

    def test_between_inclusive(self):
        criteria = RadiusCriteria(
            threshold=5, unit=Unit.KILOMETERS, operator=Operator.BETWEEN, max_threshold=20
        )
        assert matches_criteria(5000, criteria) is True  # Lower bound inclusive
        assert matches_criteria(20000, criteria) is True  # Upper bound inclusive
        assert matches_criteria(10000, criteria) is True  # In range
        assert matches_criteria(4999, criteria) is False  # Below range
        assert matches_criteria(20001, criteria) is False  # Above range

    def test_between_requires_max_threshold(self):
        criteria = RadiusCriteria(threshold=5, unit=Unit.KILOMETERS, operator=Operator.BETWEEN)
        with pytest.raises(ValueError, match="BETWEEN operator requires max_threshold"):
            matches_criteria(10000, criteria)

    def test_miles_conversion(self):
        criteria = RadiusCriteria(threshold=1, unit=Unit.MILES, operator=Operator.GTE)
        # 1 mile = 1609.344 meters
        assert matches_criteria(1609.344, criteria) is True
        assert matches_criteria(2000, criteria) is True
        assert matches_criteria(1000, criteria) is False


class TestParseCriteria:
    """Tests for the parse_criteria() function."""

    def test_parse_defaults(self):
        criteria = parse_criteria(radius=10)
        assert criteria.threshold == 10
        assert criteria.unit == Unit.KILOMETERS
        assert criteria.operator == Operator.GTE
        assert criteria.max_threshold is None

    def test_parse_with_all_options(self):
        criteria = parse_criteria(
            radius=5,
            unit="miles",
            operator="lt",
            max_radius=None,
            tolerance=0.05,
        )
        assert criteria.threshold == 5
        assert criteria.unit == Unit.MILES
        assert criteria.operator == Operator.LT
        assert criteria.tolerance == 0.05

    def test_parse_between(self):
        criteria = parse_criteria(
            radius=10,
            unit="km",
            operator="between",
            max_radius=50,
        )
        assert criteria.operator == Operator.BETWEEN
        assert criteria.threshold == 10
        assert criteria.max_threshold == 50


class TestDescribeFilter:
    """Tests for the _describe_filter() function."""

    def test_basic_filter(self):
        criteria = RadiusCriteria(threshold=10, unit=Unit.KILOMETERS, operator=Operator.GTE)
        desc = _describe_filter(criteria, None)
        assert desc == "radius >= 10 kilometers"

    def test_filter_with_type(self):
        criteria = RadiusCriteria(threshold=5, unit=Unit.MILES, operator=Operator.LT)
        desc = _describe_filter(criteria, "water")
        assert desc == "type=water, radius < 5 miles"

    def test_between_filter(self):
        criteria = RadiusCriteria(
            threshold=5, unit=Unit.KILOMETERS, operator=Operator.BETWEEN, max_threshold=20
        )
        desc = _describe_filter(criteria, None)
        assert desc == "radius 5-20 kilometers"


@requires_shapely
class TestFilterGeoJSON:
    """Tests for the filter_geojson() function."""

    @pytest.fixture
    def sample_geojson(self, tmp_path):
        """Create a sample GeoJSON file with mock polygons."""
        # Create simple square polygons of different sizes
        # Using simple coordinate squares for testing (actual geodesic area will vary)
        features = [
            {
                "type": "Feature",
                "properties": {"name": "small", "boundary_type": "park"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"name": "medium", "boundary_type": "water"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [0.1, 0], [0.1, 0.1], [0, 0.1], [0, 0]]],
                },
            },
            {
                "type": "Feature",
                "properties": {"name": "large", "boundary_type": "forest"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            },
        ]
        geojson = {"type": "FeatureCollection", "features": features}

        path = tmp_path / "test.geojson"
        with open(path, "w") as f:
            json.dump(geojson, f)
        return path

    def test_filter_by_radius(self, sample_geojson, tmp_path):
        """Test basic radius filtering."""
        output_path = tmp_path / "filtered.geojson"

        # Filter for features with radius >= 1 km
        criteria = parse_criteria(radius=1, unit="km", operator="gte")
        result = filter_geojson(sample_geojson, criteria, output_path)

        assert result.original_count == 3
        assert result.feature_count >= 1  # At least the large polygon should pass
        assert Path(result.output_path).exists()

        # Verify output has equivalent_radius properties
        with open(result.output_path) as f:
            output = json.load(f)
        for feature in output["features"]:
            assert "equivalent_radius_m" in feature["properties"]
            assert "equivalent_radius_km" in feature["properties"]

    def test_filter_by_type(self, sample_geojson, tmp_path):
        """Test filtering by boundary type."""
        output_path = tmp_path / "filtered.geojson"

        # Filter for water boundaries only, with a very small radius threshold
        criteria = parse_criteria(radius=0.001, unit="km", operator="gte")
        result = filter_geojson(sample_geojson, criteria, output_path, boundary_type="water")

        assert result.original_count == 3
        assert result.feature_count == 1  # Only the water polygon
        assert result.boundary_type == "water"

        with open(result.output_path) as f:
            output = json.load(f)
        assert len(output["features"]) == 1
        assert output["features"][0]["properties"]["name"] == "medium"

    def test_filter_no_matches(self, sample_geojson, tmp_path):
        """Test filtering with criteria that matches nothing."""
        output_path = tmp_path / "filtered.geojson"

        # Filter for radius >= 1000 km (nothing should match)
        criteria = parse_criteria(radius=1000, unit="km", operator="gte")
        result = filter_geojson(sample_geojson, criteria, output_path)

        assert result.original_count == 3
        assert result.feature_count == 0

    def test_default_output_path(self, sample_geojson):
        """Test that default output path adds _filtered suffix."""
        criteria = parse_criteria(radius=0.001, unit="km", operator="gte")
        result = filter_geojson(sample_geojson, criteria)

        expected_path = sample_geojson.with_stem(f"{sample_geojson.stem}_filtered")
        assert result.output_path == str(expected_path)

        # Clean up
        Path(result.output_path).unlink()


class TestEquivalentRadius:
    """Tests for equivalent radius calculation."""

    def test_equivalent_radius_formula(self):
        """Verify the equivalent radius formula: r = sqrt(A / π)."""
        # A circle with radius 1000m has area π * 1000^2 ≈ 3,141,592.65 m²
        # The equivalent radius of that area should be 1000m
        area = math.pi * 1000 * 1000
        expected_radius = 1000
        actual_radius = math.sqrt(area / math.pi)
        assert actual_radius == pytest.approx(expected_radius, rel=1e-9)


class TestFilterHandlers:
    """Tests for the filter handler functions."""

    def test_radius_filter_handler_no_shapely(self):
        """Test handler gracefully handles missing shapely."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_radius_filter_handler

        handler = _make_radius_filter_handler("FilterByRadius")

        # Patch HAS_SHAPELY to False
        with patch("handlers.filter_handlers.HAS_SHAPELY", False):
            result = handler(
                {
                    "input_path": "/some/file.geojson",
                    "radius": 10,
                    "unit": "km",
                    "operator": "gte",
                }
            )

        assert result["result"]["feature_count"] == 0
        assert result["result"]["original_count"] == 0
        assert "radius gte 10 km" in result["result"]["filter_applied"]

    def test_radius_range_handler_no_shapely(self):
        """Test range handler gracefully handles missing shapely."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_radius_range_handler

        handler = _make_radius_range_handler("FilterByRadiusRange")

        with patch("handlers.filter_handlers.HAS_SHAPELY", False):
            result = handler(
                {
                    "input_path": "/some/file.geojson",
                    "min_radius": 5,
                    "max_radius": 20,
                    "unit": "km",
                }
            )

        assert result["result"]["feature_count"] == 0
        assert "5-20 km" in result["result"]["filter_applied"]

    def test_type_and_radius_handler_no_shapely(self):
        """Test type+radius handler gracefully handles missing shapely."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_type_and_radius_handler

        handler = _make_type_and_radius_handler("FilterByTypeAndRadius")

        with patch("handlers.filter_handlers.HAS_SHAPELY", False):
            result = handler(
                {
                    "input_path": "/some/file.geojson",
                    "boundary_type": "water",
                    "radius": 10,
                    "unit": "km",
                    "operator": "gte",
                }
            )

        assert result["result"]["feature_count"] == 0
        assert result["result"]["boundary_type"] == "water"
        assert "type=water" in result["result"]["filter_applied"]


class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_register_filter_handlers(self):
        """Test that all filter handlers are registered."""
        from osm_geocoder.handlers.filters.filter_handlers import NAMESPACE, register_filter_handlers

        mock_poller = MagicMock()
        register_filter_handlers(mock_poller)

        # Verify all handlers were registered
        registered_names = [call[0][0] for call in mock_poller.register.call_args_list]
        # Radius filters
        assert f"{NAMESPACE}.FilterByRadius" in registered_names
        assert f"{NAMESPACE}.FilterByRadiusRange" in registered_names
        assert f"{NAMESPACE}.FilterByTypeAndRadius" in registered_names
        assert f"{NAMESPACE}.ExtractAndFilterByRadius" in registered_names
        # OSM type/tag filters
        assert f"{NAMESPACE}.FilterByOSMType" in registered_names
        assert f"{NAMESPACE}.FilterByOSMTag" in registered_names
        assert f"{NAMESPACE}.FilterGeoJSONByOSMType" in registered_names


# --- OSM Type Filter Tests ---


class TestOSMTypeParsing:
    """Tests for OSMType.from_string()."""

    def test_parse_node(self):
        assert OSMType.from_string("node") == OSMType.NODE
        assert OSMType.from_string("NODE") == OSMType.NODE
        assert OSMType.from_string("n") == OSMType.NODE

    def test_parse_way(self):
        assert OSMType.from_string("way") == OSMType.WAY
        assert OSMType.from_string("WAY") == OSMType.WAY
        assert OSMType.from_string("w") == OSMType.WAY

    def test_parse_relation(self):
        assert OSMType.from_string("relation") == OSMType.RELATION
        assert OSMType.from_string("rel") == OSMType.RELATION
        assert OSMType.from_string("r") == OSMType.RELATION

    def test_parse_all(self):
        assert OSMType.from_string("*") == OSMType.ALL
        assert OSMType.from_string("all") == OSMType.ALL
        assert OSMType.from_string("any") == OSMType.ALL

    def test_parse_invalid(self):
        with pytest.raises(ValueError, match="Unknown OSM type"):
            OSMType.from_string("polygon")


class TestDescribeOSMFilter:
    """Tests for the _describe_osm_filter() function."""

    def test_type_only(self):
        desc = _describe_osm_filter(OSMType.WAY, None, None, False)
        assert desc == "type=way"

    def test_tag_key_only(self):
        desc = _describe_osm_filter(OSMType.ALL, "amenity", None, False)
        assert desc == "amenity=*"

    def test_tag_key_and_value(self):
        desc = _describe_osm_filter(OSMType.ALL, "amenity", "restaurant", False)
        assert desc == "amenity=restaurant"

    def test_type_and_tag(self):
        desc = _describe_osm_filter(OSMType.NODE, "amenity", "cafe", False)
        assert desc == "type=node, amenity=cafe"

    def test_with_dependencies(self):
        desc = _describe_osm_filter(OSMType.WAY, "highway", None, True)
        assert desc == "type=way, highway=*, +deps"

    def test_all_elements(self):
        desc = _describe_osm_filter(OSMType.ALL, None, None, False)
        assert desc == "all elements"


class TestOSMElement:
    """Tests for OSMElement.to_geojson_feature()."""

    def test_node_to_geojson(self):
        element = OSMElement(
            id=123,
            osm_type=OSMType.NODE,
            tags={"name": "Test", "amenity": "cafe"},
            lat=48.8566,
            lon=2.3522,
        )
        feature = element.to_geojson_feature()

        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Point"
        assert feature["geometry"]["coordinates"] == [2.3522, 48.8566]
        assert feature["properties"]["osm_id"] == 123
        assert feature["properties"]["osm_type"] == "node"
        assert feature["properties"]["name"] == "Test"
        assert feature["properties"]["amenity"] == "cafe"

    def test_node_missing_coords(self):
        element = OSMElement(
            id=123,
            osm_type=OSMType.NODE,
            tags={},
        )
        assert element.to_geojson_feature() is None

    def test_way_with_coords(self):
        element = OSMElement(
            id=456,
            osm_type=OSMType.WAY,
            tags={"highway": "residential"},
            node_refs=[1, 2, 3],
        )
        node_coords = {
            1: (0.0, 0.0),
            2: (1.0, 0.0),
            3: (1.0, 1.0),
        }
        feature = element.to_geojson_feature(node_coords)

        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "LineString"
        assert feature["geometry"]["coordinates"] == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
        assert feature["properties"]["osm_type"] == "way"

    def test_way_without_coords(self):
        element = OSMElement(
            id=456,
            osm_type=OSMType.WAY,
            tags={"highway": "residential"},
            node_refs=[1, 2, 3],
        )
        feature = element.to_geojson_feature()

        assert feature["geometry"] is None

    def test_closed_way_as_polygon(self):
        element = OSMElement(
            id=789,
            osm_type=OSMType.WAY,
            tags={"building": "yes"},
            node_refs=[1, 2, 3, 4, 1],
        )
        node_coords = {
            1: (0.0, 0.0),
            2: (1.0, 0.0),
            3: (1.0, 1.0),
            4: (0.0, 1.0),
        }
        feature = element.to_geojson_feature(node_coords)

        assert feature["geometry"]["type"] == "Polygon"

    def test_relation_to_geojson(self):
        element = OSMElement(
            id=999,
            osm_type=OSMType.RELATION,
            tags={"type": "multipolygon"},
            members=[{"type": "way", "ref": 123, "role": "outer"}],
        )
        feature = element.to_geojson_feature()

        assert feature["properties"]["osm_type"] == "relation"
        assert feature["properties"]["members"] == [{"type": "way", "ref": 123, "role": "outer"}]
        assert feature["geometry"] is None


class TestFilterGeoJSONByOSMType:
    """Tests for filter_geojson_by_osm_type()."""

    @pytest.fixture
    def sample_osm_geojson(self, tmp_path):
        """Create a sample GeoJSON file with OSM-like properties."""
        features = [
            {
                "type": "Feature",
                "properties": {"osm_id": 1, "osm_type": "node", "amenity": "cafe"},
                "geometry": {"type": "Point", "coordinates": [0, 0]},
            },
            {
                "type": "Feature",
                "properties": {"osm_id": 2, "osm_type": "node", "amenity": "restaurant"},
                "geometry": {"type": "Point", "coordinates": [1, 1]},
            },
            {
                "type": "Feature",
                "properties": {"osm_id": 3, "osm_type": "way", "highway": "residential"},
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 0]]},
            },
            {
                "type": "Feature",
                "properties": {"osm_id": 4, "osm_type": "way", "building": "yes"},
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            },
        ]
        geojson = {"type": "FeatureCollection", "features": features}

        path = tmp_path / "osm_test.geojson"
        with open(path, "w") as f:
            json.dump(geojson, f)
        return path

    def test_filter_by_type_node(self, sample_osm_geojson, tmp_path):
        output_path = tmp_path / "filtered.geojson"
        result = filter_geojson_by_osm_type(
            sample_osm_geojson, osm_type="node", output_path=output_path
        )

        assert result.feature_count == 2
        assert result.original_count == 4
        assert result.osm_type == "node"

        with open(output_path) as f:
            output = json.load(f)
        assert len(output["features"]) == 2
        assert all(f["properties"]["osm_type"] == "node" for f in output["features"])

    def test_filter_by_type_way(self, sample_osm_geojson, tmp_path):
        output_path = tmp_path / "filtered.geojson"
        result = filter_geojson_by_osm_type(
            sample_osm_geojson, osm_type="way", output_path=output_path
        )

        assert result.feature_count == 2
        assert result.osm_type == "way"

    def test_filter_by_tag(self, sample_osm_geojson, tmp_path):
        output_path = tmp_path / "filtered.geojson"
        result = filter_geojson_by_osm_type(
            sample_osm_geojson,
            osm_type="*",
            tag_key="amenity",
            tag_value="cafe",
            output_path=output_path,
        )

        assert result.feature_count == 1

        with open(output_path) as f:
            output = json.load(f)
        assert output["features"][0]["properties"]["amenity"] == "cafe"

    def test_filter_by_tag_any_value(self, sample_osm_geojson, tmp_path):
        output_path = tmp_path / "filtered.geojson"
        result = filter_geojson_by_osm_type(
            sample_osm_geojson,
            osm_type="*",
            tag_key="amenity",
            tag_value="*",
            output_path=output_path,
        )

        assert result.feature_count == 2  # cafe and restaurant

    def test_filter_by_type_and_tag(self, sample_osm_geojson, tmp_path):
        output_path = tmp_path / "filtered.geojson"
        result = filter_geojson_by_osm_type(
            sample_osm_geojson,
            osm_type="way",
            tag_key="highway",
            output_path=output_path,
        )

        assert result.feature_count == 1

        with open(output_path) as f:
            output = json.load(f)
        assert output["features"][0]["properties"]["highway"] == "residential"


class TestOSMTypeFilterHandlers:
    """Tests for the OSM type/tag filter handlers."""

    def test_osm_type_handler_no_osmium(self):
        """Test handler gracefully handles missing pyosmium."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_osm_type_filter_handler

        handler = _make_osm_type_filter_handler("FilterByOSMType")

        with patch("handlers.filter_handlers.HAS_OSMIUM_TYPE", False):
            result = handler(
                {
                    "input_path": "/some/file.pbf",
                    "osm_type": "way",
                    "include_dependencies": True,
                }
            )

        assert result["result"]["feature_count"] == 0
        assert result["result"]["osm_type"] == "way"
        assert result["result"]["dependencies_included"] is True

    def test_osm_tag_handler_no_osmium(self):
        """Test tag handler gracefully handles missing pyosmium."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_osm_tag_filter_handler

        handler = _make_osm_tag_filter_handler("FilterByOSMTag")

        with patch("handlers.filter_handlers.HAS_OSMIUM_TYPE", False):
            result = handler(
                {
                    "input_path": "/some/file.pbf",
                    "tag_key": "amenity",
                    "tag_value": "restaurant",
                    "osm_type": "node",
                    "include_dependencies": False,
                }
            )

        assert result["result"]["feature_count"] == 0
        assert "amenity=restaurant" in result["result"]["filter_applied"]

    def test_geojson_osm_type_handler_empty_path(self):
        """Test GeoJSON handler with empty input path."""
        from osm_geocoder.handlers.filters.filter_handlers import _make_geojson_osm_type_filter_handler

        handler = _make_geojson_osm_type_filter_handler("FilterGeoJSONByOSMType")

        result = handler(
            {
                "input_path": "",
                "osm_type": "node",
            }
        )

        assert result["result"]["feature_count"] == 0
        assert result["result"]["osm_type"] == "node"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
