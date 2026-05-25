"""Deterministic tests for osm.geocode — no network.

The Nominatim HTTP layer is stubbed (urlopen for the client tests, the client
functions for the handler tests), so these check our marshalling, error
semantics, and throttle wiring rather than the live service (which the live
Nominatim proof covers).
"""

from __future__ import annotations

import io
import json

import pytest

from osm_geocoder.handlers.geocoding import geocoding_handlers as H

# Reference the SAME geocode module the handler uses (imported via the shim as
# `_osm_tools.geocode`). Importing `osm_geocoder.tools._osm_tools.geocode`
# directly would be a *different* module object (dual import path), so its
# GeocodeError class would not match the one the handler raises.
geocode = H.geocode_tool


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch):
    monkeypatch.setattr(H, "cached_result", lambda *a, **k: None)
    monkeypatch.setattr(H, "save_result_meta", lambda *a, **k: None)


# --- handler layer (stub the geocode client) ----------------------------------


def test_geocode_marshals_first_result(monkeypatch):
    monkeypatch.setattr(H.geocode_tool, "forward",
                        lambda q, **k: [geocode.GeoResult("37.82", "-122.48", "Golden Gate Bridge")])
    rv = H.handle({"_facet_name": "osm.geocode.Geocode", "address": "Golden Gate Bridge"})
    assert rv["result"] == {"lat": "37.82", "lon": "-122.48", "display_name": "Golden Gate Bridge"}


def test_geocode_no_result_raises(monkeypatch):
    monkeypatch.setattr(H.geocode_tool, "forward", lambda q, **k: [])
    with pytest.raises(geocode.GeocodeError, match="no geocode result"):
        H.handle({"_facet_name": "osm.geocode.Geocode", "address": "Nowheresville XYZZY 99999"})


def test_geocode_empty_address_raises():
    with pytest.raises(ValueError, match="address is required"):
        H.handle({"_facet_name": "osm.geocode.Geocode", "address": "   "})


def test_reverse_geocode_marshals(monkeypatch):
    monkeypatch.setattr(H.geocode_tool, "reverse",
                        lambda lat, lon, **k: geocode.GeoResult("37.82", "-122.48", "Some Bridge, SF"))
    rv = H.handle({"_facet_name": "osm.geocode.ReverseGeocode", "lat": 37.82, "lon": -122.48})
    assert rv["result"]["display_name"] == "Some Bridge, SF"


def test_reverse_geocode_requires_coords():
    with pytest.raises(ValueError, match="lat and lon are required"):
        H.handle({"_facet_name": "osm.geocode.ReverseGeocode", "lat": None, "lon": None})


# --- client layer (stub urlopen) ----------------------------------------------


def _fake_urlopen(payload):
    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): self.close()
    def _open(req, timeout=None):
        return _Resp(json.dumps(payload).encode())
    return _open


def test_client_forward_parses(monkeypatch):
    monkeypatch.setattr(geocode, "_MIN_INTERVAL", 0.0)   # no throttle in tests
    monkeypatch.setattr(geocode, "urlopen", _fake_urlopen(
        [{"lat": "1.0", "lon": "2.0", "display_name": "X", "osm_type": "node", "type": "peak"}]))
    res = geocode.forward("anywhere")
    assert len(res) == 1 and res[0].lat == "1.0" and res[0].place_type == "peak"


def test_client_reverse_error_raises(monkeypatch):
    monkeypatch.setattr(geocode, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(geocode, "urlopen", _fake_urlopen({"error": "Unable to geocode"}))
    with pytest.raises(geocode.GeocodeError, match="no reverse-geocode result"):
        geocode.reverse(0.0, 0.0)


def test_client_forward_empty_query_raises():
    with pytest.raises(geocode.GeocodeError, match="non-empty query"):
        geocode.forward("")
