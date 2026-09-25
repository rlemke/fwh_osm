"""The routing server must fail loudly, never silently produce a hollow map.

graphhopper-web serves exactly ONE region. ``_route_pair`` sends only lon/lat +
profile, so a server holding another state answers HTTP 400 "Point 0 is out of
bounds" for every pair and returns ``None`` exactly like an ordinary miss. Every
zoom then scores 0 betweenness and the step reports success. These tests pin the
two guards that stop that: a probe before the loop, and a floor after it.
"""

import pytest

from osm_geocoder.handlers.roads import zoom_sbs
from osm_geocoder.handlers.roads.zoom_sbs import (
    PermanentError,
    probe_router,
    route_and_accumulate,
)

COORDS = {1: (-122.68, 45.52), 2: (-123.09, 44.05)}
PAIRS = [(1, 2)]


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _server(resp):
    def _get(url, params=None, timeout=None):
        return resp

    return _get


class TestProbeRouter:
    def test_out_of_bounds_raises_with_the_servers_own_message(self, monkeypatch):
        """The measured failure: Oregon coordinates against the Washington graph."""
        body = "Point 0 is out of bounds: 45.5152,-122.6784, the bounds are: -148.6745"
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(_Resp(400, text=body)))

        with pytest.raises(PermanentError) as e:
            probe_router(COORDS, PAIRS, "car")
        # The server's own words, so the operator sees WHY, not just "probe failed".
        assert "out of bounds" in str(e.value)
        assert "graphhopper-web serves ONE region" in str(e.value)

    def test_unreachable_server_raises(self, monkeypatch):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)

        def _boom(url, params=None, timeout=None):
            raise OSError("Connection refused")

        monkeypatch.setattr(zoom_sbs.requests, "get", _boom)
        with pytest.raises(PermanentError, match="Connection refused"):
            probe_router(COORDS, PAIRS, "car")

    def test_missing_requests_raises_rather_than_routing_nothing(self, monkeypatch):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", False)
        with pytest.raises(PermanentError, match="requests"):
            probe_router(COORDS, PAIRS, "car")

    def test_a_served_region_passes(self, monkeypatch):
        ok = _Resp(200, {"paths": [{"points": {"coordinates": [[-122.68, 45.52]]}}]})
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(ok))
        probe_router(COORDS, PAIRS, "car")  # no raise

    def test_no_pairs_is_not_a_failure(self, monkeypatch):
        """An empty sample means nothing to prove, not a broken router."""
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        probe_router(COORDS, [], "car")


class _NullIndex:
    snap_hits = 0
    snap_misses = 0

    def snap_route(self, coords):
        return set()


class TestCollapseFloor:
    def test_every_route_failing_raises(self, monkeypatch):
        """The probe can pass and the server still die mid-loop."""
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs, "_route_pair", lambda *a, **k: None)
        with pytest.raises(PermanentError, match="routing collapsed"):
            route_and_accumulate(
                [(1, 2)] * 100, COORDS, "/graph", "car", _NullIndex(), max_concurrent=2
            )

    def test_a_sparse_but_real_result_is_kept(self, monkeypatch):
        """Some pairs legitimately have no path; that is data, not a failure."""
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        calls = {"n": 0}

        def _sometimes(*a, **k):
            calls["n"] += 1
            return [[-122.68, 45.52]] if calls["n"] % 2 else None

        monkeypatch.setattr(zoom_sbs, "_route_pair", _sometimes)
        _votes, routed = route_and_accumulate(
            [(1, 2)] * 100, COORDS, "/graph", "car", _NullIndex(), max_concurrent=2
        )
        assert routed == 50
