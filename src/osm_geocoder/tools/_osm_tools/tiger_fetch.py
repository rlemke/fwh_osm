"""Download US state boundaries from Census TIGER as osm extraction polygons.

The sub-national poly source for the ``osm.planet`` pipeline. osmfr's ``/polygons/``
tree has NO per-US-state polygons (it splits the US into four macro-regions) and
Geofabrik's ``.poly`` host is IP-banned, so US state boundaries come from the
Census TIGER/Line ``STATE`` shapefile. Each state is written as a **GeoJSON**
polygon (osmium extract reads GeoJSON as well as ``.poly``) keyed Geofabrik-style
(``north-america/us/<state-slug>`` — e.g. ``north-america/us/california``) so the
resulting extracts resolve against the workflows' region requests.

Extraction source is the caller's choice (``ExtractRegions.planet_path``): the
planet, or the ``north-america`` continent extract for a cheaper pass.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .polygon_fetch import Region  # reuse the (key, poly-path) pair

TIGER_YEAR = os.environ.get("FW_TIGER_YEAR", "2023")
TIGER_STATE_URL = os.environ.get(
    "FW_TIGER_STATE_URL",
    f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/STATE/tl_{TIGER_YEAR}_us_state.zip",
)
# Territories to skip — Geofabrik's us/ set is the 50 states + DC.
# (AS=60, GU=66, MP=69, PR=72, VI=78)
_SKIP_STATEFP = {"60", "66", "69", "72", "78"}


class TigerError(RuntimeError):
    """A TIGER fetch/parse step failed."""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fetch_tiger_states(dest: str, *, url: str = TIGER_STATE_URL,
                       on_log: Callable[[str], None] | None = None) -> list[Region]:
    """Download the TIGER state shapefile and write one GeoJSON polygon per state.

    Returns ``[Region(key='north-america/us/<slug>', poly='<...>.geojson')]`` ready
    for ``planet_bootstrap`` (which infers the GeoJSON file type from the extension).
    """
    log = on_log or (lambda _m: None)
    try:
        import shapefile  # pyshp
    except ImportError as exc:  # pragma: no cover
        raise TigerError("fetch_tiger_states needs pyshp (pip install pyshp)") from exc

    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    log(f"downloading TIGER states: {url}")
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
    except Exception as exc:
        raise TigerError(f"TIGER download failed: {exc}") from exc

    tmp = dest_p / "_tiger_shp"
    tmp.mkdir(exist_ok=True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
    shps = list(tmp.glob("*.shp"))
    if not shps:
        raise TigerError("no .shp in the TIGER state archive")

    reader = shapefile.Reader(str(shps[0]))
    regions: list[Region] = []
    for rec, shape in zip(reader.records(), reader.shapes()):
        if rec["STATEFP"] in _SKIP_STATEFP:
            continue
        name = rec["NAME"]
        slug = _slug(name)
        key = f"north-america/us/{slug}"
        gj = dest_p / f"{slug}.geojson"
        gj.write_text(json.dumps({
            "type": "Feature",
            "properties": {"name": name},
            "geometry": shape.__geo_interface__,   # pyshp -> GeoJSON geometry
        }))
        regions.append(Region(key, str(gj.resolve())))

    log(f"fetched {len(regions)} US state polygons from TIGER {TIGER_YEAR}")
    return regions
