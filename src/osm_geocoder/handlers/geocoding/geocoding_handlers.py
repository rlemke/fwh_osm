"""Event-facet handlers for the ``osm.geocode`` namespace.

Implements the previously-orphaned ``osm.geocode.Geocode`` facet (it was
declared in geocoder.ffl but had no handler) and adds ``ReverseGeocode``,
both backed by the Nominatim client in ``_osm_tools.geocode``. Geocoding is a
scalar service (query → coordinate), not a layer transform, so results are the
typed ``GeoCoordinate`` rather than a GeoJSON path; the output cache keys on a
synthetic ``geocode:…`` path so repeats (e.g. a foreach over addresses) are free.

Failures are explicit: an unresolvable address / point raises (the step errors)
rather than silently returning empty coordinates.
"""

from __future__ import annotations

import logging
import os

from ..shared.output_cache import cached_result, save_result_meta
from ..shared.pbf_convert import geocode as geocode_tool

log = logging.getLogger(__name__)

NAMESPACE = "osm.geocode"


def _make_geocode_handler(facet_name: str):
    """Forward geocode: address → GeoCoordinate (first/best Nominatim result)."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        address = (payload.get("address") or "").strip()
        step_log = payload.get("_step_log")
        if not address:
            raise ValueError("Geocode: address is required")

        cache = {"path": f"geocode:fwd:{address}", "size": 0}
        cp = {"q": address}
        hit = cached_result(qualified, cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: geocoding {address!r}")
        results = geocode_tool.forward(address, limit=1)
        if not results:
            raise geocode_tool.GeocodeError(f"no geocode result for {address!r}")
        r = results[0]

        if step_log:
            step_log(f"{facet_name}: {address!r} -> ({r.lat}, {r.lon}) {r.display_name}",
                     level="success")
        rv = {"result": {"lat": r.lat, "lon": r.lon, "display_name": r.display_name}}
        save_result_meta(qualified, cache, cp, rv)
        return rv

    return handler


def _make_reverse_geocode_handler(facet_name: str):
    """Reverse geocode: (lat, lon) → GeoCoordinate (nearest address/place)."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        lat = payload.get("lat")
        lon = payload.get("lon")
        zoom = int(payload.get("zoom", 18))
        step_log = payload.get("_step_log")
        if lat is None or lon is None:
            raise ValueError("ReverseGeocode: lat and lon are required")

        cache = {"path": f"geocode:rev:{lat},{lon},{zoom}", "size": 0}
        cp = {"lat": lat, "lon": lon, "zoom": zoom}
        hit = cached_result(qualified, cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: reverse geocoding ({lat}, {lon}) z{zoom}")
        r = geocode_tool.reverse(float(lat), float(lon), zoom=zoom)

        if step_log:
            step_log(f"{facet_name}: ({lat}, {lon}) -> {r.display_name}", level="success")
        rv = {"result": {"lat": r.lat, "lon": r.lon, "display_name": r.display_name}}
        save_result_meta(qualified, cache, cp, rv)
        return rv

    return handler


GEOCODE_FACETS = [
    ("Geocode", _make_geocode_handler),
    ("ReverseGeocode", _make_reverse_geocode_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in GEOCODE_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.geocode facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_geocoding_handlers(poller) -> None:
    """Register all osm.geocode event facet handlers with an AgentPoller."""
    for facet_name, factory in GEOCODE_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered geocoding handler: %s", qualified_name)
