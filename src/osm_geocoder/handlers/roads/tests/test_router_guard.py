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


OOB = "Point 0 is out of bounds: 45.5152,-122.6784, the bounds are: -148.6745"
NO_ROUTE = ('{"message":"Connection between locations not found","hints":'
            '[{"details":"com.graphhopper.util.exceptions.ConnectionNotFoundException"}]}')


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
    def test_out_of_bounds_raises(self, monkeypatch):
        """The measured failure: Oregon coordinates against the Washington graph."""
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(_Resp(400, text=OOB)))

        with pytest.raises(PermanentError) as e:
            probe_router(COORDS, PAIRS, "car")
        assert "oob=" in str(e.value), "the outcome tally names what actually happened"
        assert "out of bounds" in str(e.value), "and the server's own words say why"
        assert "serves ONE region" in str(e.value)

    def test_a_pair_with_no_route_PASSES(self, monkeypatch):
        """⚠️ The Hawaii false positive, 2026-09-25.

        Zoom 2 samples pairs at least 300 km apart, which for Hawaii is
        inter-island, and no car route exists between islands. GraphHopper answers
        400 for that exactly as it does for a wrong region — but to report
        ConnectionNotFound it had to snap BOTH points into the loaded graph, so it
        is positive proof the right region is served. Conflating the two failed a
        state whose data was fine.
        """
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(_Resp(400, text=NO_ROUTE)))
        probe_router(COORDS, PAIRS, "car")  # must not raise

    def test_200_with_no_paths_also_passes(self, monkeypatch):
        """Same reasoning: the server answered about this graph."""
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(_Resp(200, {"paths": []})))
        probe_router(COORDS, PAIRS, "car")

    def test_unreachable_server_raises(self, monkeypatch):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)

        def _boom(url, params=None, timeout=None):
            raise OSError("Connection refused")

        monkeypatch.setattr(zoom_sbs.requests, "get", _boom)
        with pytest.raises(PermanentError) as e:
            probe_router(COORDS, PAIRS, "car")
        # The tally says WHAT, the server's own words say WHY: "Connection refused"
        # and "timeout" call for different actions.
        assert "transport=" in str(e.value)
        assert "Connection refused" in str(e.value)

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


class TestServerFaultFloor:
    """The probe proves the server CAN serve the region, not that it kept doing so."""

    def _run(self, monkeypatch, response):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        monkeypatch.setattr(zoom_sbs.requests, "get", _server(response))
        return route_and_accumulate(
            [(1, 2)] * 100, COORDS, "/graph", "car", _NullIndex(), max_concurrent=2
        )

    def test_the_server_going_out_of_bounds_mid_run_raises(self, monkeypatch):
        with pytest.raises(PermanentError, match="stopped serving this region"):
            self._run(monkeypatch, _Resp(400, text=OOB))

    def test_a_dead_server_mid_run_raises(self, monkeypatch):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)

        def _boom(url, params=None, timeout=None):
            raise OSError("Connection refused")

        monkeypatch.setattr(zoom_sbs.requests, "get", _boom)
        with pytest.raises(PermanentError, match="stopped serving this region"):
            route_and_accumulate(
                [(1, 2)] * 100, COORDS, "/graph", "car", _NullIndex(), max_concurrent=2
            )

    def test_every_pair_unroutable_is_NOT_a_failure(self, monkeypatch):
        """Hawaii: an archipelago's long pairs are genuinely unroutable by car."""
        votes, routed = self._run(monkeypatch, _Resp(400, text=NO_ROUTE))
        assert routed == 0
        assert votes == {}

    def test_a_sparse_but_real_result_is_kept(self, monkeypatch):
        monkeypatch.setattr(zoom_sbs, "HAS_REQUESTS", True)
        calls = {"n": 0}
        ok = _Resp(200, {"paths": [{"points": {"coordinates": [[-122.68, 45.52]]}}]})

        def _sometimes(url, params=None, timeout=None):
            calls["n"] += 1
            return ok if calls["n"] % 2 else _Resp(400, text=NO_ROUTE)

        monkeypatch.setattr(zoom_sbs.requests, "get", _sometimes)
        _votes, routed = route_and_accumulate(
            [(1, 2)] * 100, COORDS, "/graph", "car", _NullIndex(), max_concurrent=2
        )
        assert routed == 50
