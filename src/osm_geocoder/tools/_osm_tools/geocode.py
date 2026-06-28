"""Nominatim geocoding client — forward (address → coords) and reverse
(coords → address).

The tool layer behind the ``osm.geocode`` facets. Talks to a Nominatim instance
over HTTP (the public ``nominatim.openstreetmap.org`` by default; point
``FW_NOMINATIM_URL`` at your own instance for volume). Forward and reverse are
the two geocoding primitives that recur across Nominatim / Photon / Pelias.

Public-instance etiquette is built in: a required ``User-Agent`` and a polite
minimum interval between calls (Nominatim's usage policy is ~1 request/second).
Both are configurable; a self-hosted instance can set the interval to 0.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

NOMINATIM_URL = os.environ.get("FW_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = os.environ.get(
    "FW_NOMINATIM_USER_AGENT", "facetwork-osm-geocoder/1.0 (+https://github.com/rlemke/fwh_osm)"
)
# Nominatim's public usage policy is at most ~1 request/second. Self-hosted
# instances can set FW_NOMINATIM_MIN_INTERVAL=0.
_MIN_INTERVAL = float(os.environ.get("FW_NOMINATIM_MIN_INTERVAL", "1.0"))
DEFAULT_TIMEOUT = 15

_throttle_lock = threading.Lock()
_last_call_at = [0.0]


class GeocodeError(RuntimeError):
    """Raised when a geocode request fails or yields no usable result."""


@dataclass
class GeoResult:
    """A single geocoding result (mirrors the FFL GeoCoordinate, plus extras)."""

    lat: str
    lon: str
    display_name: str
    osm_type: str = ""
    place_type: str = ""

    @classmethod
    def from_nominatim(cls, d: dict) -> GeoResult:
        return cls(
            lat=str(d.get("lat", "")),
            lon=str(d.get("lon", "")),
            display_name=d.get("display_name", ""),
            osm_type=d.get("osm_type", ""),
            place_type=d.get("type", ""),
        )


def _throttle() -> None:
    """Block until at least ``_MIN_INTERVAL`` has elapsed since the last call."""
    if _MIN_INTERVAL <= 0:
        return
    with _throttle_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call_at[0])
        if wait > 0:
            time.sleep(wait)
        _last_call_at[0] = time.monotonic()


def _get(path: str, params: dict, timeout: int):
    """GET ``{NOMINATIM_URL}{path}?{params}`` as JSON, throttled + UA'd."""
    _throttle()
    url = f"{NOMINATIM_URL.rstrip('/')}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as exc:  # network / HTTP / JSON — surface explicitly
        raise GeocodeError(f"Nominatim request failed ({path}): {exc}") from exc


def forward(query: str, limit: int = 1, countrycodes: str = "", timeout: int = DEFAULT_TIMEOUT) -> list[GeoResult]:
    """Forward geocode: address / place name → ranked coordinate results.

    ``countrycodes`` (comma-separated ISO codes) optionally constrains results.
    Returns up to ``limit`` results, best-ranked first (may be empty).
    """
    if not query or not query.strip():
        raise GeocodeError("forward geocode requires a non-empty query")
    params = {"q": query, "format": "json", "limit": str(max(1, limit))}
    if countrycodes:
        params["countrycodes"] = countrycodes
    data = _get("/search", params, timeout)
    if not isinstance(data, list):
        raise GeocodeError(f"unexpected Nominatim /search response: {data!r:.120}")
    return [GeoResult.from_nominatim(d) for d in data]


def reverse(lat: float, lon: float, zoom: int = 18, timeout: int = DEFAULT_TIMEOUT) -> GeoResult:
    """Reverse geocode: coordinate → the nearest address / place.

    ``zoom`` (0–18) controls address granularity (18 ≈ building, 10 ≈ city).
    Raises :class:`GeocodeError` if the point resolves to nothing.
    """
    params = {"lat": str(lat), "lon": str(lon), "zoom": str(zoom), "format": "json"}
    data = _get("/reverse", params, timeout)
    if not isinstance(data, dict) or data.get("error") or "lat" not in data:
        raise GeocodeError(f"no reverse-geocode result for ({lat}, {lon})")
    return GeoResult.from_nominatim(data)
