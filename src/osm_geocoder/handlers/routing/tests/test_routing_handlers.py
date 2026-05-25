"""Tests for routing adapter handler dispatch and registration."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


class TestAPIRouterDispatch:
    def test_dispatch_keys(self):
        from osm_geocoder.handlers.routing.api_router import API_DISPATCH, NAMESPACE

        assert len(API_DISPATCH) == 3
        for key in API_DISPATCH:
            assert key.startswith(NAMESPACE)

    def test_handle_unknown_facet(self):
        from osm_geocoder.handlers.routing.api_router import handle

        with pytest.raises(ValueError, match="Unknown API routing facet"):
            handle({"_facet_name": "osm.Routing.API.NonExistent"})

    def test_route_fallback(self, tmp_path):
        """Route handler should return estimate when APIs are unavailable."""
        from osm_geocoder.handlers.routing.api_router import _handle_route

        with patch("osm_geocoder.handlers.routing.api_router._output_dir", return_value=str(tmp_path)):
            result = _handle_route({
                "from_lat": 48.8566,
                "from_lon": 2.3522,
                "from_name": "Paris",
                "to_lat": 50.8503,
                "to_lon": 4.3517,
                "to_name": "Brussels",
                "profile": "car",
            })

        route = result["result"]["route"]
        assert route["from_name"] == "Paris"
        assert route["to_name"] == "Brussels"
        assert route["distance_km"] > 0
        assert route["duration_min"] > 0
        assert route["format"] == "GeoJSON"
        assert os.path.exists(route["output_path"])

        # Verify GeoJSON structure
        with open(route["output_path"]) as f:
            geojson = json.load(f)
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) == 1
        assert geojson["features"][0]["geometry"]["type"] == "LineString"

    def test_multi_stop_fallback(self, tmp_path):
        """MultiStopRoute should work with estimate fallback."""
        from osm_geocoder.handlers.routing.api_router import _handle_multi_stop

        waypoints = json.dumps([
            {"lon": 2.3522, "lat": 48.8566, "name": "Paris"},
            {"lon": 4.3517, "lat": 50.8503, "name": "Brussels"},
            {"lon": 4.9041, "lat": 52.3676, "name": "Amsterdam"},
        ])

        with patch("osm_geocoder.handlers.routing.api_router._output_dir", return_value=str(tmp_path)):
            result = _handle_multi_stop({
                "waypoints": waypoints,
                "profile": "car",
            })

        ms = result["result"]
        assert ms["leg_count"] == 2
        assert ms["total_distance_km"] > 0
        assert ms["total_duration_min"] > 0
        assert ms["format"] == "GeoJSON"

    def test_multi_stop_semicolon_format(self, tmp_path):
        """MultiStopRoute should parse 'lon,lat;lon,lat' format."""
        from osm_geocoder.handlers.routing.api_router import _handle_multi_stop

        with patch("osm_geocoder.handlers.routing.api_router._output_dir", return_value=str(tmp_path)):
            result = _handle_multi_stop({
                "waypoints": "2.3522,48.8566;4.3517,50.8503",
                "profile": "car",
            })

        assert result["result"]["leg_count"] == 1

    def test_multi_stop_too_few_waypoints(self, tmp_path):
        """MultiStopRoute with < 2 waypoints returns empty result."""
        from osm_geocoder.handlers.routing.api_router import _handle_multi_stop

        with patch("osm_geocoder.handlers.routing.api_router._output_dir", return_value=str(tmp_path)):
            result = _handle_multi_stop({
                "waypoints": json.dumps([{"lon": 0, "lat": 0}]),
                "profile": "car",
            })

        assert result["result"]["leg_count"] == 0

    def test_isochrone_estimate(self, tmp_path):
        """Isochrone should produce a polygon estimate when no API is available."""
        from osm_geocoder.handlers.routing.api_router import _handle_isochrone

        with patch("osm_geocoder.handlers.routing.api_router._output_dir", return_value=str(tmp_path)):
            result = _handle_isochrone({
                "center_lat": 48.8566,
                "center_lon": 2.3522,
                "center_name": "Paris",
                "time_minutes": 15,
                "profile": "car",
            })

        iso = result["result"]
        assert iso["time_minutes"] == 15
        assert iso["format"] == "GeoJSON"
        assert os.path.exists(iso["output_path"])

        with open(iso["output_path"]) as f:
            geojson = json.load(f)
        assert geojson["features"][0]["geometry"]["type"] == "Polygon"


class TestOSRMRouterDispatch:
    def test_dispatch_keys(self):
        from osm_geocoder.handlers.routing.osrm_router import NAMESPACE, OSRM_DISPATCH

        # Route, MultiStopRoute, Isochrone, Matrix, Nearest, MapMatch, Trip.
        assert len(OSRM_DISPATCH) == 7
        for key in OSRM_DISPATCH:
            assert key.startswith(NAMESPACE)

    def test_handle_unknown_facet(self):
        from osm_geocoder.handlers.routing.osrm_router import handle

        with pytest.raises(ValueError, match="Unknown OSRM routing facet"):
            handle({"_facet_name": "osm.Routing.OSRM.NonExistent"})

    def test_route_fallback(self, tmp_path):
        """OSRM route should fall back to estimate when server is unavailable."""
        from osm_geocoder.handlers.routing.osrm_router import _handle_route

        with patch("osm_geocoder.handlers.routing.osrm_router._output_dir", return_value=str(tmp_path)):
            result = _handle_route({
                "from_lat": 48.8566,
                "from_lon": 2.3522,
                "from_name": "Paris",
                "to_lat": 50.8503,
                "to_lon": 4.3517,
                "to_name": "Brussels",
                "profile": "car",
            })

        route = result["result"]["route"]
        assert route["distance_km"] > 0
        assert route["backend"] == "estimate"

    def test_multi_stop_fallback(self, tmp_path):
        from osm_geocoder.handlers.routing.osrm_router import _handle_multi_stop

        waypoints = json.dumps([
            {"lon": 2.3522, "lat": 48.8566, "name": "Paris"},
            {"lon": 4.3517, "lat": 50.8503, "name": "Brussels"},
        ])

        with patch("osm_geocoder.handlers.routing.osrm_router._output_dir", return_value=str(tmp_path)):
            result = _handle_multi_stop({
                "waypoints": waypoints,
                "profile": "car",
            })

        assert result["result"]["leg_count"] == 1
        assert result["result"]["backend"] == "estimate"


class TestRoutingAdapterRegistration:
    @staticmethod
    def _expected_count():
        # Derive from the dispatch tables so adding verbs/engines never makes
        # these registration tests stale (API + OSRM + Valhalla + GraphHopper).
        from osm_geocoder.handlers.routing.api_router import API_DISPATCH
        from osm_geocoder.handlers.routing.graphhopper_router import GRAPHHOPPER_DISPATCH
        from osm_geocoder.handlers.routing.osrm_router import OSRM_DISPATCH
        from osm_geocoder.handlers.routing.valhalla_router import VALHALLA_DISPATCH

        return len(API_DISPATCH) + len(OSRM_DISPATCH) + len(VALHALLA_DISPATCH) + len(GRAPHHOPPER_DISPATCH)

    def test_register_poller(self):
        from osm_geocoder.handlers.routing.routing_adapter_handlers import (
            register_routing_adapter_handlers,
        )

        poller = MagicMock()
        register_routing_adapter_handlers(poller)
        assert poller.register.call_count == self._expected_count()

    def test_register_runner(self):
        from osm_geocoder.handlers.routing.routing_adapter_handlers import register_handlers

        runner = MagicMock()
        register_handlers(runner)
        assert runner.register_handler.call_count == self._expected_count()
