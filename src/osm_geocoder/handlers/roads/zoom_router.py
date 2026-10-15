"""Find the GraphHopper router serving a region.

A router serves ONE graph. The fleet runs one router per region, placed by the
fleet-agents (facetwork ``fw fleet router``), and records each replica in the
``fleet_router_slots`` collection: ``{region, profile, state, url,
lease_expires}``. A handler knows its region from its graph path
(``…/cache/osm/graphhopper/<region>-latest/<profile>``), so it asks for a READY
slot whose lease is fresh and routes there.

With no such slot -- no registry, no Mongo, a region nobody runs -- it falls
back to ``GRAPHHOPPER_API_URL``, the single fleet-wide router, so nothing
changes for a deployment that never adds a per-region one. The router probe
(``zoom_sbs.probe_router``) still decides whether that fallback can serve the
region, exactly as before.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time

log = logging.getLogger(__name__)

SLOTS = "fleet_router_slots"  # facetwork scripts/lib/_helpers/_routers.py
CACHE_S = 30.0
REACH_TIMEOUT_S = 2.0

_GRAPH_RE = re.compile(r"/graphhopper/(?P<region>.+?)-latest/(?P<profile>[^/]+)/?$")
_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
_client = None


def default_url() -> str:
    return os.environ.get("GRAPHHOPPER_API_URL", "http://localhost:8989")


def region_of(graph_dir: str) -> tuple[str, str] | None:
    """(region, profile) from a graph path, or None if it is not one."""
    m = _GRAPH_RE.search(graph_dir or "")
    return (m["region"], m["profile"]) if m else None


def _slots_collection():
    global _client
    url = os.environ.get("FW_MONGODB_URL")
    if not url:
        return None
    if _client is None:
        import pymongo

        _client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
    return _client[os.environ.get("FW_MONGODB_DATABASE", "facetwork")][SLOTS]


def reachable(url: str) -> bool:
    """Whether THIS process can reach ``url`` (GET /health).

    "Ready" in the registry means the router's own host could reach it. That
    is not the same thing: measured 2026-10-14, a router published by Docker
    Desktop on a Mac answered 200 on the Mac and reset every connection from
    the other hosts (so did that Mac's fleet-wide router on 8989). A replica a
    runner cannot reach is skipped rather than failing every request."""
    try:
        import requests

        return requests.get(f"{url}/health", timeout=REACH_TIMEOUT_S).status_code == 200
    except Exception:  # noqa: BLE001 - unreachable for any reason is unreachable
        return False


def ready_urls(region: str, profile: str) -> list[str]:
    """URLs of the ready replicas for ``region``/``profile`` that this process
    can reach (cached CACHE_S)."""
    key = (region, profile)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    urls: list[str] = []
    try:
        coll = _slots_collection()
        if coll is not None:
            urls = [s["url"] for s in coll.find(
                {"region": region, "profile": profile, "state": "ready",
                 "lease_expires": {"$gt": now}, "url": {"$ne": None}}, {"url": 1})]
    except Exception as exc:  # noqa: BLE001 - registry unreachable: use the fallback
        log.warning("router registry unreadable (%s); using %s", exc, default_url())
    if urls:
        ok = [u for u in urls if reachable(u)]
        if len(ok) < len(urls):
            log.warning("router for %s/%s: %d of %d ready replica(s) unreachable from here: %s",
                        region, profile, len(urls) - len(ok), len(urls),
                        ", ".join(u for u in urls if u not in ok))
        urls = ok
    with _lock:
        _cache[key] = (now, urls)
    return urls


def router_url(graph_dir: str = "", profile: str = "") -> str:
    """The router to send this graph's requests to.

    A random ready replica (spreading load when there are several), else the
    fleet-wide ``GRAPHHOPPER_API_URL``."""
    rp = region_of(graph_dir)
    if rp:
        urls = ready_urls(rp[0], profile or rp[1])
        if urls:
            return random.choice(urls)
    return default_url()


def describe(graph_dir: str = "", profile: str = "") -> str:
    """For logs and errors: which router, and why that one."""
    rp = region_of(graph_dir)
    url = router_url(graph_dir, profile)
    if rp and url != default_url():
        return f"{url} (region router for {rp[0]})"
    if rp:
        return f"{url} (fleet-wide router; no ready region router for {rp[0]})"
    return url


def forget() -> None:
    """Drop cached lookups (tests; a caller that just saw a router fail)."""
    with _lock:
        _cache.clear()
