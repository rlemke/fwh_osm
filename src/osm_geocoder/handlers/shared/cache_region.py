"""Derive the region path (``north-america/us/california``) from an ``OSMCache``.

Handlers that consume a cache (GraphHopper, Valhalla, the HTML map renderer)
used to recover the region by regex-parsing ``cache.url`` for
``download.geofabrik.de``. That silently broke the moment the fleet started
fetching from its own mirror (``FW_GEOFABRIK_BASE_URL=http://afl-minio:9000/
osm-extracts``): the regex no longer matched, the WHOLE URL fell through as the
"region", and it was then used as an S3 key — MinIO answered ``HeadObject 400
Bad Request`` five times and the task dead-lettered. Measured 2026-09-22 on a
California build.

The cache record already carries the typed region (``cache.region.canonical``),
so prefer that; parse the URL only for legacy records, and when parsing, accept
whatever base the downloader is configured with rather than one hostname.
"""

from __future__ import annotations

import os
import re

_GEOFABRIK_REGION_RE = re.compile(r"https?://download\.geofabrik\.de/(.+)-latest\.[^/]+$")
_LATEST_RE = re.compile(r"(.+)-latest\.[^/]+$")


def _configured_base() -> str:
    """The extract base the downloader uses (read per call so tests can set it)."""
    return os.environ.get("FW_GEOFABRIK_BASE_URL", "https://download.geofabrik.de").rstrip("/")


def region_from_url(url: str) -> str:
    """Region path from an extract URL, or the raw URL when nothing matches.

    Order: the configured base (self-hosted mirror or Geofabrik), then the
    historical Geofabrik pattern, then the raw string so the library can raise
    its own "no pbf manifest entry" error naming what it was given.
    """
    url = (url or "").strip()
    if not url:
        return ""
    base = _configured_base()
    if url.startswith(base + "/"):
        m = _LATEST_RE.match(url[len(base) + 1 :])
        if m:
            return m.group(1).strip("/")
    m = _GEOFABRIK_REGION_RE.match(url)
    if m:
        return m.group(1).strip("/")
    return url


def region_from_cache(cache: dict | None) -> str:
    """Region path for an ``OSMCache`` payload (typed region first, URL second)."""
    cache = cache or {}
    region = cache.get("region")
    if isinstance(region, dict):
        for key in ("canonical", "geofabrik_path"):
            value = region.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().strip("/")
    return region_from_url(cache.get("url", ""))
