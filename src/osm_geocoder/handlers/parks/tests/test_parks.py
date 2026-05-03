"""Tests for park extraction handlers."""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest
from osm_geocoder.handlers.parks.park_extractor import (
    HAS_OSMIUM,
    HAS_SHAPELY,
    PROTECT_CLASS_ALL,
    ParkFeatures,
    ParkStats,
    ParkType,
    calculate_area_km2,
    calculate_park_stats,
    classify_park,
    filter_parks_by_type,
    matches_park_type,
    parse_protect_classes,
)
from osm_geocoder.handlers.parks.park_handlers import (
    NAMESPACE,
    PARK_FACETS,
    _empty_result,
    _empty_stats,
    _make_filter_parks_handler,
    _make_park_stats_handler,
    _result_to_dict,
    _stats_to_dict,
    register_park_handlers,
)

requires_osmium = pytest.mark.skipif(not HAS_OSMIUM, reason="pyosmium not installed")
requires_shapely = pytest.mark.skipif(not HAS_SHAPELY, reason="shapely not installed")


class TestParkType:
    """Tests for ParkType enum."""

    def test_from_string_national(self):
        """Test parsing national park variants."""
        assert ParkType.from_string("national") == ParkType.NATIONAL
        assert ParkType.from_string("national_park") == ParkType.NATIONAL
        assert ParkType.from_string("national_parks") == ParkType.NATIONAL
        assert ParkType.from_string("NATIONAL") == ParkType.NATIONAL

    def test_from_string_state(self):
        """Test parsing state park variants."""
        assert ParkType.from_string("state") == ParkType.STATE
        assert ParkType.from_string("state_park") == ParkType.STATE
        assert ParkType.from_string("regional") == ParkType.STATE
        assert ParkType.from_string("regional_park") == ParkType.STATE

    def test_from_string_nature_reserve(self):
        """Test parsing nature reserve variants."""
        assert ParkType.from_string("nature_reserve") == ParkType.NATURE_RESERVE
        assert ParkType.from_string("nature_reserves") == ParkType.NATURE_RESERVE
        assert ParkType.from_string("reserve") == ParkType.NATURE_RESERVE

    def test_from_string_protected_area(self):
        """Test parsing protected area variants."""
        assert ParkType.from_string("protected_area") == ParkType.PROTECTED_AREA
        assert ParkType.from_string("protected_areas") == ParkType.PROTECTED_AREA
        assert ParkType.from_string("protected") == ParkType.PROTECTED_AREA

    def test_from_string_all(self):
        """Test parsing all/wildcard."""
        assert ParkType.from_string("all") == ParkType.ALL
        assert ParkType.from_string("*") == ParkType.ALL

    def test_from_string_invalid(self):
        """Test parsing invalid type raises error."""
        with pytest.raises(ValueError, match="Unknown park type"):
            ParkType.from_string("invalid")

    def test_enum_values(self):
        """Test enum string values."""
        assert ParkType.NATIONAL.value == "national"
        assert ParkType.STATE.value == "state"
        assert ParkType.NATURE_RESERVE.value == "nature_reserve"
        assert ParkType.PROTECTED_AREA.value == "protected_area"
        assert ParkType.ALL.value == "all"


class TestParseProtectClasses:
    """Tests for protect class parsing."""

    def test_parse_wildcard(self):
        """Test parsing wildcard."""
        classes = parse_protect_classes("*")
        assert classes == PROTECT_CLASS_ALL

    def test_parse_all(self):
        """Test parsing 'all'."""
        classes = parse_protect_classes("all")
        assert classes == PROTECT_CLASS_ALL

    def test_parse_single(self):
        """Test parsing single class."""
        classes = parse_protect_classes("2")
        assert classes == {"2"}

    def test_parse_multiple(self):
        """Test parsing multiple classes."""
        classes = parse_protect_classes("1a,1b,2")
        assert classes == {"1a", "1b", "2"}

    def test_parse_with_spaces(self):
        """Test parsing with spaces."""
        classes = parse_protect_classes("1a, 1b, 2")
        assert classes == {"1a", "1b", "2"}

    def test_parse_empty(self):
        """Test parsing empty string."""
        classes = parse_protect_classes("")
        assert classes == set()


class TestClassifyPark:
    """Tests for park classification."""

    def test_classify_national_by_boundary(self):
        """Test classifying national park by boundary tag."""
        tags = {"boundary": "national_park", "name": "Yellowstone"}
        assert classify_park(tags) == "national"

    def test_classify_national_by_protect_class(self):
        """Test classifying national park by protect_class."""
        tags = {"boundary": "protected_area", "protect_class": "2"}
        assert classify_park(tags) == "national"

    def test_classify_national_by_designation(self):
        """Test classifying national park by designation."""
        tags = {"boundary": "protected_area", "designation": "national_park"}
        assert classify_park(tags) == "national"

    def test_classify_state_by_protect_class(self):
        """Test classifying state park by protect_class."""
        tags = {"boundary": "protected_area", "protect_class": "5"}
        assert classify_park(tags) == "state"

    def test_classify_state_by_designation(self):
        """Test classifying state park by designation."""
        tags = {"boundary": "protected_area", "designation": "state_park"}
        assert classify_park(tags) == "state"

    def test_classify_nature_reserve_by_leisure(self):
        """Test classifying nature reserve by leisure tag."""
        tags = {"leisure": "nature_reserve"}
        assert classify_park(tags) == "nature_reserve"

    def test_classify_nature_reserve_by_protect_class(self):
        """Test classifying nature reserve by protect_class 1a/1b."""
        tags = {"boundary": "protected_area", "protect_class": "1a"}
        assert classify_park(tags) == "nature_reserve"

    def test_classify_generic_protected_area(self):
        """Test classifying generic protected area."""
        tags = {"boundary": "protected_area", "protect_class": "4"}
        assert classify_park(tags) == "protected_area"

    def test_classify_generic_park(self):
        """Test classifying generic park."""
        tags = {"leisure": "park"}
        assert classify_park(tags) == "park"


class TestMatchesParkType:
    """Tests for park type matching."""

    def test_matches_national_park(self):
        """Test matching national park."""
        tags = {"boundary": "national_park"}
        assert matches_park_type(tags, ParkType.NATIONAL)
        assert not matches_park_type(tags, ParkType.STATE)

    def test_matches_state_park(self):
        """Test matching state park."""
        tags = {"boundary": "protected_area", "protect_class": "5"}
        assert matches_park_type(tags, ParkType.STATE)
        assert not matches_park_type(tags, ParkType.NATIONAL)

    def test_matches_nature_reserve(self):
        """Test matching nature reserve."""
        tags = {"leisure": "nature_reserve"}
        assert matches_park_type(tags, ParkType.NATURE_RESERVE)

    def test_matches_all(self):
        """Test matching all park types."""
        tags = {"boundary": "national_park"}
        assert matches_park_type(tags, ParkType.ALL)

    def test_matches_with_protect_class_filter(self):
        """Test matching with protect_class filter."""
        tags = {"boundary": "protected_area", "protect_class": "2"}
        assert matches_park_type(tags, ParkType.ALL, {"2"})
        assert not matches_park_type(tags, ParkType.ALL, {"5"})

    def test_non_park_returns_false(self):
        """Test that non-parks don't match."""
        tags = {"highway": "primary"}
        assert not matches_park_type(tags, ParkType.ALL)


class TestCalculateAreaKm2:
    """Tests for area calculation."""

    @requires_shapely
    def test_calculate_polygon_area(self):
        """Test calculating area of a polygon."""
        # Simple 1x1 degree square near equator
        geometry = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
        area = calculate_area_km2(geometry)
        # Should be roughly 12,300 km² (varies with latitude)
        assert 10000 < area < 15000

    def test_calculate_none_geometry(self):
        """Test calculating area of None returns 0."""
        assert calculate_area_km2(None) == 0.0

    def test_calculate_empty_geometry(self):
        """Test calculating area of empty geometry."""
        assert calculate_area_km2({}) == 0.0


class TestFilterParksByType:
    """Tests for filtering parks from GeoJSON."""

    def test_filter_national_parks(self):
        """Test filtering national parks."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "name": "Yellowstone",
                        "boundary": "national_park",
                        "park_type": "national",
                        "area_km2": 8983,
                    },
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {
                        "name": "Some State Park",
                        "boundary": "protected_area",
                        "protect_class": "5",
                        "park_type": "state",
                        "area_km2": 100,
                    },
                    "geometry": None,
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            result = filter_parks_by_type(temp_path, park_type="national")
            assert result.feature_count == 1
            assert result.park_type == "national"
            assert result.total_area_km2 == 8983

            os.unlink(result.output_path)
        finally:
            os.unlink(temp_path)

    def test_filter_by_protect_class(self):
        """Test filtering by protect_class."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "boundary": "protected_area",
                        "protect_class": "2",
                        "park_type": "national",
                        "area_km2": 1000,
                    },
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {
                        "boundary": "protected_area",
                        "protect_class": "5",
                        "park_type": "state",
                        "area_km2": 500,
                    },
                    "geometry": None,
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            result = filter_parks_by_type(temp_path, park_type="all", protect_classes="2")
            assert result.feature_count == 1
            assert result.total_area_km2 == 1000

            os.unlink(result.output_path)
        finally:
            os.unlink(temp_path)


class TestParkStats:
    """Tests for park statistics calculation."""

    def test_calculate_stats(self):
        """Test calculating park statistics."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"park_type": "national", "area_km2": 5000},
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {"park_type": "national", "area_km2": 3000},
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {"park_type": "state", "area_km2": 500},
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {"park_type": "nature_reserve", "area_km2": 200},
                    "geometry": None,
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_park_stats(temp_path)
            assert stats.total_parks == 4
            assert stats.total_area_km2 == 8700
            assert stats.national_parks == 2
            assert stats.state_parks == 1
            assert stats.nature_reserves == 1
            assert stats.other_protected == 0
        finally:
            os.unlink(temp_path)

    def test_calculate_stats_empty(self):
        """Test calculating stats for empty GeoJSON."""
        geojson = {"type": "FeatureCollection", "features": []}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_park_stats(temp_path)
            assert stats.total_parks == 0
            assert stats.total_area_km2 == 0.0
        finally:
            os.unlink(temp_path)


class TestEmptyResults:
    """Tests for empty result helpers."""

    def test_empty_result(self):
        """Test empty result has correct structure."""
        result = _empty_result("national", "*")
        assert result["output_path"] == ""
        assert result["feature_count"] == 0
        assert result["park_type"] == "national"
        assert result["protect_classes"] == "*"
        assert result["total_area_km2"] == 0.0
        assert "extraction_date" in result

    def test_empty_stats(self):
        """Test empty stats has correct structure."""
        stats = _empty_stats()
        assert stats["total_parks"] == 0
        assert stats["total_area_km2"] == 0.0
        assert stats["national_parks"] == 0
        assert stats["state_parks"] == 0
        assert stats["nature_reserves"] == 0


class TestResultConversions:
    """Tests for result conversion functions."""

    def test_result_to_dict(self):
        """Test ParkFeatures to dict conversion."""
        result = ParkFeatures(
            output_path="/tmp/parks.geojson",
            feature_count=42,
            park_type="national",
            protect_classes="2",
            total_area_km2=50000.0,
            format="GeoJSON",
            extraction_date="2024-01-15T10:30:00+00:00",
        )
        d = _result_to_dict(result)
        assert d["output_path"] == "/tmp/parks.geojson"
        assert d["feature_count"] == 42
        assert d["park_type"] == "national"
        assert d["total_area_km2"] == 50000.0

    def test_stats_to_dict(self):
        """Test ParkStats to dict conversion."""
        stats = ParkStats(
            total_parks=10,
            total_area_km2=100000.0,
            national_parks=5,
            state_parks=3,
            nature_reserves=2,
            other_protected=0,
            park_type="mixed",
        )
        d = _stats_to_dict(stats)
        assert d["total_parks"] == 10
        assert d["total_area_km2"] == 100000.0
        assert d["national_parks"] == 5


class TestHandlerFactories:
    """Tests for handler factory functions."""

    def test_filter_parks_handler_no_path(self):
        """Test filter parks handler returns empty when no path."""
        handler = _make_filter_parks_handler("FilterParksByType")
        result = handler({})
        assert result["result"]["feature_count"] == 0

    def test_park_stats_handler_no_path(self):
        """Test park stats handler returns empty when no path."""
        handler = _make_park_stats_handler("ParkStatistics")
        result = handler({})
        assert result["stats"]["total_parks"] == 0


class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_park_facets_count(self):
        """Test expected number of park facets."""
        assert len(PARK_FACETS) == 2

    def test_park_facets_names(self):
        """Test park facet names."""
        names = [name for name, _ in PARK_FACETS]
        assert "FilterParksByType" in names
        assert "ParkStatistics" in names

    def test_register_park_handlers(self):
        """Test handler registration with mock poller."""
        poller = MagicMock()
        register_park_handlers(poller)

        # Should register 2 handlers
        assert poller.register.call_count == 2

        # Verify qualified names are used
        call_args = [call[0][0] for call in poller.register.call_args_list]
        assert f"{NAMESPACE}.FilterParksByType" in call_args
        assert f"{NAMESPACE}.ParkStatistics" in call_args

    def test_namespace_value(self):
        """Test namespace is correct."""
        assert NAMESPACE == "osm.Parks"
