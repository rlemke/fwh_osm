"""A handler routes to its REGION's router, found by graph path.

One fleet-wide router meant one region at a time: a build of another region
first had to repoint every host's router and wait. With per-region routers the
registry names a ready replica per region, and anything it cannot answer falls
back to the fleet-wide GRAPHHOPPER_API_URL, unchanged.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.roads import zoom_router as zr  # noqa: E402

GRAPH = "s3://afl-cache/cache/osm/graphhopper/north-america/us-major-roads-latest/car"


class FakeSlots:
    def __init__(self, docs):
        self.docs = docs
        self.queries = 0

    def find(self, q, _proj=None):
        self.queries += 1
        now = q["lease_expires"]["$gt"]
        return [d for d in self.docs
                if d["region"] == q["region"] and d["profile"] == q["profile"]
                and d["state"] == "ready" and d["lease_expires"] > now and d.get("url")]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    zr.forget()
    monkeypatch.setattr(zr, "reachable", lambda url: True)
    monkeypatch.setenv("GRAPHHOPPER_API_URL", "http://fleet-wide:8989")
    yield
    zr.forget()


def _slots(monkeypatch, docs):
    fake = FakeSlots(docs)
    monkeypatch.setattr(zr, "_slots_collection", lambda: fake)
    return fake


def _slot(region="north-america/us-major-roads", state="ready", url="http://r1:9001", ttl=100):
    return {"region": region, "profile": "car", "state": state, "url": url,
            "lease_expires": time.time() + ttl}


def test_region_comes_from_the_graph_path():
    assert zr.region_of(GRAPH) == ("north-america/us-major-roads", "car")
    assert zr.region_of("/scratch/graphhopper/europe/germany-latest/car/") == ("europe/germany", "car")
    assert zr.region_of("") is None and zr.region_of("/some/other/path") is None


def test_a_ready_region_router_wins(monkeypatch):
    _slots(monkeypatch, [_slot()])
    assert zr.router_url(GRAPH, "car") == "http://r1:9001"


@pytest.mark.parametrize("doc", [
    _slot(state="starting"),
    _slot(state="failed"),
    _slot(ttl=-1),                       # lease lapsed: its host stopped renewing
    _slot(region="europe/germany"),      # another region's router
])
def test_anything_but_a_fresh_ready_router_falls_back(monkeypatch, doc):
    _slots(monkeypatch, [doc])
    assert zr.router_url(GRAPH, "car") == "http://fleet-wide:8989"


def test_no_registry_falls_back(monkeypatch):
    monkeypatch.setattr(zr, "_slots_collection", lambda: None)
    assert zr.router_url(GRAPH, "car") == "http://fleet-wide:8989"


def test_an_unreadable_registry_falls_back(monkeypatch):
    def boom():
        raise RuntimeError("no server")

    monkeypatch.setattr(zr, "_slots_collection", boom)
    assert zr.router_url(GRAPH, "car") == "http://fleet-wide:8989"


def test_replicas_share_the_load(monkeypatch):
    _slots(monkeypatch, [_slot(url="http://r1:9001"), _slot(url="http://r2:9001")])
    assert {zr.router_url(GRAPH, "car") for _ in range(60)} == {"http://r1:9001", "http://r2:9001"}


def test_lookups_are_cached(monkeypatch):
    fake = _slots(monkeypatch, [_slot()])
    for _ in range(50):
        zr.router_url(GRAPH, "car")
    assert fake.queries == 1


def test_route_pair_sends_to_the_region_router(monkeypatch):
    from osm_geocoder.handlers.roads import zoom_sbs

    _slots(monkeypatch, [_slot(url="http://region:9002")])
    seen = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"paths": [{"points": {"coordinates": [[0, 0], [1, 1]]}}]}

    monkeypatch.setattr(zoom_sbs.requests, "get", lambda url, **k: seen.append(url) or Resp())
    coords = {1: (0.0, 0.0), 2: (1.0, 1.0)}
    assert zoom_sbs._route_pair(1, 2, coords, GRAPH, "car") == [[0, 0], [1, 1]]
    assert seen == ["http://region:9002/route"]


def test_an_unreachable_replica_is_skipped(monkeypatch):
    """Ready in the registry means reachable from ITS host, not from this one."""
    _slots(monkeypatch, [_slot(url="http://mac:18990"), _slot(url="http://linux:18990")])
    monkeypatch.setattr(zr, "reachable", lambda url: "linux" in url)
    assert {zr.router_url(GRAPH, "car") for _ in range(20)} == {"http://linux:18990"}


def test_no_reachable_replica_falls_back(monkeypatch):
    _slots(monkeypatch, [_slot(url="http://mac:18990")])
    monkeypatch.setattr(zr, "reachable", lambda url: False)
    assert zr.router_url(GRAPH, "car") == "http://fleet-wide:8989"
