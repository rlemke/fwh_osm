"""Tests for route extraction handlers."""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest
from osm_geocoder.handlers.routes.route_extractor import (
    HAS_OSMIUM,
    RouteFeatures,
    RouteStats,
    RouteType,
    _haversine_distance,
    calculate_route_stats,
)
from osm_geocoder.handlers.routes.route_handlers import (
    NAMESPACE,
    ROUTE_FACETS,
    _empty_result,
    _empty_stats,
    _make_filter_routes_handler,
    _make_route_stats_handler,
    _result_to_dict,
    _stats_to_dict,
    register_route_handlers,
)

requires_osmium = pytest.mark.skipif(not HAS_OSMIUM, reason="pyosmium not installed")


class TestRouteType:
    """Tests for RouteType enum."""

    def test_from_string_bicycle(self):
        """Test parsing bicycle variants."""
        assert RouteType.from_string("bicycle") == RouteType.BICYCLE
        assert RouteType.from_string("bike") == RouteType.BICYCLE
        assert RouteType.from_string("cycling") == RouteType.BICYCLE
        assert RouteType.from_string("cycle") == RouteType.BICYCLE
        assert RouteType.from_string("BICYCLE") == RouteType.BICYCLE

    def test_from_string_hiking(self):
        """Test parsing hiking variants."""
        assert RouteType.from_string("hiking") == RouteType.HIKING
        assert RouteType.from_string("hike") == RouteType.HIKING
        assert RouteType.from_string("walking") == RouteType.HIKING
        assert RouteType.from_string("foot") == RouteType.HIKING
        assert RouteType.from_string("trail") == RouteType.HIKING

    def test_from_string_train(self):
        """Test parsing train variants."""
        assert RouteType.from_string("train") == RouteType.TRAIN
        assert RouteType.from_string("rail") == RouteType.TRAIN
        assert RouteType.from_string("railway") == RouteType.TRAIN

    def test_from_string_bus(self):
        """Test parsing bus type."""
        assert RouteType.from_string("bus") == RouteType.BUS

    def test_from_string_public_transport(self):
        """Test parsing public transport variants."""
        assert RouteType.from_string("public_transport") == RouteType.PUBLIC_TRANSPORT
        assert RouteType.from_string("transit") == RouteType.PUBLIC_TRANSPORT
        assert RouteType.from_string("pt") == RouteType.PUBLIC_TRANSPORT

    def test_from_string_invalid(self):
        """Test parsing invalid type raises error."""
        with pytest.raises(ValueError, match="Unknown route type"):
            RouteType.from_string("invalid")

    def test_enum_values(self):
        """Test enum string values."""
        assert RouteType.BICYCLE.value == "bicycle"
        assert RouteType.HIKING.value == "hiking"
        assert RouteType.TRAIN.value == "train"
        assert RouteType.BUS.value == "bus"
        assert RouteType.PUBLIC_TRANSPORT.value == "public_transport"


class TestHaversineDistance:
    """Tests for haversine distance calculation."""

    def test_same_point(self):
        """Test distance between same point is zero."""
        dist = _haversine_distance(40.0, -74.0, 40.0, -74.0)
        assert dist == 0.0

    def test_new_york_to_los_angeles(self):
        """Test approximate distance NYC to LA (~3940 km)."""
        dist = _haversine_distance(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3900 < dist < 4000

    def test_london_to_paris(self):
        """Test approximate distance London to Paris (~344 km)."""
        dist = _haversine_distance(51.5074, -0.1278, 48.8566, 2.3522)
        assert 340 < dist < 350


class TestEmptyResults:
    """Tests for empty result helpers."""

    def test_empty_result(self):
        """Test empty result has correct structure."""
        result = _empty_result("bicycle", "ncn", True)
        assert result["output_path"] == ""
        assert result["feature_count"] == 0
        assert result["route_type"] == "bicycle"
        assert result["network_level"] == "ncn"
        assert result["include_infrastructure"] is True
        assert result["format"] == "GeoJSON"
        assert "extraction_date" in result

    def test_empty_stats(self):
        """Test empty stats has correct structure."""
        stats = _empty_stats()
        assert stats["route_count"] == 0
        assert stats["total_length_km"] == 0.0
        assert stats["infrastructure_count"] == 0
        assert stats["route_type"] == ""


class TestResultConversions:
    """Tests for result conversion functions."""

    def test_result_to_dict(self):
        """Test RouteFeatures to dict conversion."""
        result = RouteFeatures(
            output_path="/tmp/routes.geojson",
            feature_count=42,
            route_type="bicycle",
            network_level="ncn",
            include_infrastructure=True,
            format="GeoJSON",
            extraction_date="2024-01-15T10:30:00+00:00",
        )
        d = _result_to_dict(result)
        assert d["output_path"] == "/tmp/routes.geojson"
        assert d["feature_count"] == 42
        assert d["route_type"] == "bicycle"
        assert d["network_level"] == "ncn"
        assert d["include_infrastructure"] is True
        assert d["format"] == "GeoJSON"
        assert d["extraction_date"] == "2024-01-15T10:30:00+00:00"

    def test_stats_to_dict(self):
        """Test RouteStats to dict conversion."""
        stats = RouteStats(
            route_count=10,
            total_length_km=123.45,
            infrastructure_count=25,
            route_type="hiking",
        )
        d = _stats_to_dict(stats)
        assert d["route_count"] == 10
        assert d["total_length_km"] == 123.45
        assert d["infrastructure_count"] == 25
        assert d["route_type"] == "hiking"


class TestHandlerFactories:
    """Tests for handler factory functions."""

    def test_filter_routes_handler_no_path(self):
        """Test filter routes handler returns empty when no path."""
        handler = _make_filter_routes_handler("FilterRoutesByType")
        result = handler({})
        assert result["result"]["feature_count"] == 0

    def test_route_stats_handler_no_path(self):
        """Test route stats handler returns empty when no path."""
        handler = _make_route_stats_handler("RouteStatistics")
        result = handler({})
        assert result["stats"]["route_count"] == 0


class TestRouteStats:
    """Tests for route statistics calculation."""

    def test_calculate_stats_with_features(self):
        """Test calculating stats from GeoJSON with features."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "feature_type": "route",
                        "route_types": ["bicycle"],
                    },
                    "geometry": None,
                },
                {
                    "type": "Feature",
                    "properties": {
                        "feature_type": "infrastructure",
                        "route_types": ["bicycle"],
                    },
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                },
                {
                    "type": "Feature",
                    "properties": {
                        "feature_type": "way",
                        "route_types": ["bicycle"],
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[0, 0], [0, 1]],
                    },
                },
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_route_stats(temp_path)
            assert stats.route_count == 1
            assert stats.infrastructure_count == 1
            assert stats.route_type == "bicycle"
            # One LineString from (0,0) to (0,1) should be ~111 km
            assert stats.total_length_km > 100
        finally:
            os.unlink(temp_path)

    def test_calculate_stats_empty(self):
        """Test calculating stats from empty GeoJSON."""
        geojson = {"type": "FeatureCollection", "features": []}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
            json.dump(geojson, f)
            temp_path = f.name

        try:
            stats = calculate_route_stats(temp_path)
            assert stats.route_count == 0
            assert stats.infrastructure_count == 0
            assert stats.total_length_km == 0.0
        finally:
            os.unlink(temp_path)


class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_route_facets_count(self):
        """Test expected number of route facets."""
        assert len(ROUTE_FACETS) == 2

    def test_route_facets_names(self):
        """Test route facet names."""
        names = [name for name, _ in ROUTE_FACETS]
        assert "FilterRoutesByType" in names
        assert "RouteStatistics" in names

    def test_register_route_handlers(self):
        """Test handler registration with mock poller."""
        poller = MagicMock()
        register_route_handlers(poller)

        # Should register 2 handlers
        assert poller.register.call_count == 2

        # Verify qualified names are used
        call_args = [call[0][0] for call in poller.register.call_args_list]
        assert f"{NAMESPACE}.FilterRoutesByType" in call_args
        assert f"{NAMESPACE}.RouteStatistics" in call_args

    def test_namespace_value(self):
        """Test namespace is correct."""
        assert NAMESPACE == "osm.Routes"
