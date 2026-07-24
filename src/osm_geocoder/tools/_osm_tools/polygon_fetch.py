"""Download OSM boundary polygons from OSM France for planet extraction.

Shared library behind ``download_polygons.py`` and the ``osm.planet`` handlers.
Enumerates OSM France's ``/polygons/`` tree (Geofabrik's ``.poly`` host is
IP-banned; osmfr's is not) and downloads the continent- and/or country-level
``.poly`` files, returning ``(region_key, poly_path)`` pairs ready for
``planet_bootstrap``.

Region keys are normalised to **Geofabrik style** so the pipeline's requests
resolve against the resulting extracts: osmfr's ``europe/czech_republic`` →
``europe/czech-republic``; the ``oceania`` top-level → ``australia-oceania``.
Continents keep their bare name (``europe``, ``africa`` … ).
"""
from __future__ import annotations

import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OSMFR_POLYGONS = os.environ.get(
    "FW_OSMFR_POLYGONS_URL", "https://download.openstreetmap.fr/polygons"
).rstrip("/")

# osmfr top-level names → Geofabrik continent keys (only where they differ).
_CONTINENT_KEY = {"oceania": "australia-oceania"}
# The 8 osmfr top-level regions that hold country polys.
CONTINENTS = ("africa", "asia", "central-america", "europe",
              "north-america", "oceania", "russia", "south-america")

SCOPES = ("continents", "countries", "all", "subnational")


class PolygonError(RuntimeError):
    """A polygon enumeration/download step failed."""


@dataclass
class Region:
    key: str        # Geofabrik-style region key, e.g. "europe/czech-republic"
    poly: str       # absolute path to the downloaded .poly


def _geofabrik_key(continent: str, country: str | None) -> str:
    cont = _CONTINENT_KEY.get(continent, continent)
    if country is None:
        return cont
    return f"{cont}/{country.replace('_', '-')}"


def _list_polys(url: str) -> list[str]:
    """Return the ``.poly`` basenames (sans extension) listed at a polygons dir."""
    html = urllib.request.urlopen(url, timeout=30).read().decode()
    return re.findall(r'href="([^"/]+)\.poly"', html)


def fetch_polygons(dest: str, *, scope: str = "all",
                   on_log: Callable[[str], None] | None = None) -> list[Region]:
    """Download osmfr polygons into ``dest`` and return the region list.

    ``scope``: ``continents`` (8 continent polys), ``countries`` (all ~199 country
    polys), or ``all`` (both). Existing files are reused (idempotent).
    """
    if scope not in SCOPES:
        raise PolygonError(f"scope must be one of {SCOPES}, got {scope!r}")
    log = on_log or (lambda _m: None)
    if scope == "subnational":
        # US states from Census TIGER (osmfr has no per-state polys; Geofabrik banned).
        from . import tiger_fetch  # lazy: tiger_fetch imports Region from here
        return tiger_fetch.fetch_tiger_states(dest, on_log=on_log)
    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    regions: list[Region] = []

    def _download(url: str, key: str) -> None:
        out = dest_p / f"{key.replace('/', '__')}.poly"
        if not out.exists():
            out.write_bytes(urllib.request.urlopen(url, timeout=30).read())
        regions.append(Region(key, str(out.resolve())))

    for cont in CONTINENTS:
        if scope in ("continents", "all"):
            _download(f"{OSMFR_POLYGONS}/{cont}.poly", _geofabrik_key(cont, None))
        if scope in ("countries", "all"):
            try:
                countries = _list_polys(f"{OSMFR_POLYGONS}/{cont}/")
            except Exception as exc:
                raise PolygonError(f"listing {cont} countries failed: {exc}") from exc
            for c in countries:
                _download(f"{OSMFR_POLYGONS}/{cont}/{c}.poly", _geofabrik_key(cont, c))
        log(f"{cont}: {len([r for r in regions if r.key.split('/')[0] == _CONTINENT_KEY.get(cont, cont)])} polys")

    log(f"fetched {len(regions)} polygons (scope={scope})")
    return regions
