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


def latency(url: str) -> float | None:
    """Seconds for the faster of two /health round trips, or None if unreachable."""
    best = None
    for _ in range(2):
        t = time.perf_counter()
        if not reachable(url):
            return None
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


def _info_bbox(url: str) -> list | None:
    """The bbox a router reports on /info (identifies the graph it serves)."""
    try:
        import requests

        r = requests.get(f"{url}/info", timeout=REACH_TIMEOUT_S)
        return r.json().get("bbox") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def _same_bbox(a, b) -> bool:
    return bool(a and b) and len(a) == len(b) and all(abs(x - y) < 1e-6 for x, y in zip(a, b))


def _this_host() -> str:
    import socket

    return os.environ.get("HOSTNAME") or socket.gethostname()


def _candidates(slots: list[dict]) -> list[str]:
    """URLs that may serve these slots' region, nearest-first by construction.

    * each ready replica's published URL;
    * a replica on THIS host also through ``host.docker.internal``: a Docker
      Desktop Mac cannot reach its own published port by its LAN address from
      a container, so without this its runners always went to another host;
    * the fleet-wide ``GRAPHHOPPER_API_URL`` when it serves the same graph
      (same /info bbox) -- usually a router on this host's own container
      network."""
    out: list[str] = []
    me = _this_host()
    for s in slots:
        if s.get("host") == me and s.get("port"):
            out.append(f"http://host.docker.internal:{s['port']}")
        out.append(s["url"])
    boxes = [s.get("bbox") for s in slots if s.get("bbox")]
    if boxes and any(_same_bbox(_info_bbox(default_url()), b) for b in boxes):
        out.append(default_url())
    return list(dict.fromkeys(out))


# A replica counts as "near" when its round trip is within this of the
# fastest; requests spread across the near ones and skip the far ones.
NEAR_FACTOR = 2.0
NEAR_SLACK_S = 0.005


def ready_urls(region: str, profile: str) -> list[str]:
    """The NEAREST routers this process can reach for ``region``/``profile``
    (cached CACHE_S): every reachable candidate within NEAR_FACTOR of the
    fastest round trip.

    ⚠️ Nearest, not any. Bypass and ring detection route one request after
    another, so latency is their whole cost: measured 2026-10-15 on the US
    build, a replica over WiFi took 27 ms a route against 8 ms for the router
    on the builder's own host, and detection ran 3.4x slower than the night
    before."""
    key = (region, profile)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    slots: list[dict] = []
    try:
        coll = _slots_collection()
        if coll is not None:
            slots = list(coll.find(
                {"region": region, "profile": profile, "state": "ready",
                 "lease_expires": {"$gt": now}, "url": {"$ne": None}},
                {"url": 1, "host": 1, "port": 1, "bbox": 1}))
    except Exception as exc:  # noqa: BLE001 - registry unreachable: use the fallback
        log.warning("router registry unreadable (%s); using %s", exc, default_url())
    urls: list[str] = []
    if slots:
        cands = _candidates(slots)
        timed = {u: latency(u) for u in cands}
        ok = {u: t for u, t in timed.items() if t is not None}
        dead = [s["url"] for s in slots if s["url"] not in ok]
        if dead:
            log.warning("router for %s/%s: %d of %d ready replica(s) unreachable from here: %s",
                        region, profile, len(dead), len(slots), ", ".join(dead))
        if ok:
            best = min(ok.values())
            limit = max(best * NEAR_FACTOR, best + NEAR_SLACK_S)
            urls = [u for u in cands if u in ok and ok[u] <= limit]
            log.info("router for %s/%s: using %s (fastest %.0f ms)", region, profile,
                     ", ".join(urls), best * 1000)
    with _lock:
        _cache[key] = (now, urls)
    return urls


def router_url(graph_dir: str = "", profile: str = "") -> str:
    """The router to send this graph's requests to.

    A random near replica (spreading load when there are several), else the
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
