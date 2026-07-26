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
from .boundary_gen import _strip_admin_type  # drop County/Parish/Borough… so TIGER
#                                              slugs match self-generated county slugs

TIGER_YEAR = os.environ.get("FW_TIGER_YEAR", "2023")
TIGER_STATE_URL = os.environ.get(
    "FW_TIGER_STATE_URL",
    f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/STATE/tl_{TIGER_YEAR}_us_state.zip",
)
TIGER_COUNTY_URL = os.environ.get(
    "FW_TIGER_COUNTY_URL",
    f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/COUNTY/tl_{TIGER_YEAR}_us_county.zip",
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


def _download_shp(url: str, dest_p: "Path", tag: str, log) -> "shapefile.Reader":
    import shapefile  # pyshp
    log(f"downloading TIGER {tag}: {url}")
    try:
        data = urllib.request.urlopen(url, timeout=180).read()
    except Exception as exc:
        raise TigerError(f"TIGER {tag} download failed: {exc}") from exc
    tmp = dest_p / f"_tiger_{tag}_shp"
    tmp.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
    shps = list(tmp.glob("*.shp"))
    if not shps:
        raise TigerError(f"no .shp in the TIGER {tag} archive")
    return shapefile.Reader(str(shps[0]))


def _statefp_slugs(dest_p: "Path", log) -> dict:
    """{STATEFP: state-slug} from the TIGER state shapefile — so counties can be
    keyed under their parent state (north-america/us/<state>/<county>)."""
    reader = _download_shp(TIGER_STATE_URL, dest_p, "state", log)
    return {rec["STATEFP"]: _slug(rec["NAME"])
            for rec in reader.records() if rec["STATEFP"] not in _SKIP_STATEFP}


def fetch_tiger_counties(dest: str, *, only_state: str | None = None,
                         url: str = TIGER_COUNTY_URL,
                         on_log: Callable[[str], None] | None = None) -> list[Region]:
    """Download the TIGER county shapefile and write one GeoJSON polygon per county,
    keyed **nested** ``north-america/us/<state-slug>/<county-slug>``.

    The nesting is essential: ~30 states each have a "Washington County", so a flat
    key would collide. ``only_state`` (a state slug, e.g. ``"california"``) restricts
    the output to that state's counties — the per-state fan-out unit. Returns Regions
    ready for ``planet_bootstrap`` (GeoJSON file type inferred from the extension).
    """
    log = on_log or (lambda _m: None)
    try:
        import shapefile  # noqa: F401 - surface a clear error before the download
    except ImportError as exc:  # pragma: no cover
        raise TigerError("fetch_tiger_counties needs pyshp (pip install pyshp)") from exc

    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    fp2slug = _statefp_slugs(dest_p, log)
    reader = _download_shp(url, dest_p, "county", log)

    regions: list[Region] = []
    for rec, shape in zip(reader.records(), reader.shapes()):
        state_slug = fp2slug.get(rec["STATEFP"])
        if not state_slug:                       # territory or unknown state — skip
            continue
        if only_state and state_slug != only_state:
            continue
        # NAME is usually bare ("Alachua") but sometimes carries the type (Alaska
        # "Aleutians East Borough"). Strip it so TIGER matches self-gen's bare slug.
        county_slug = _slug(_strip_admin_type(rec["NAME"]))
        key = f"north-america/us/{state_slug}/{county_slug}"
        gj = dest_p / f"{key.replace('/', '__')}.geojson"
        gj.write_text(json.dumps({
            "type": "Feature",
            "properties": {"name": rec["NAME"], "state": state_slug},
            "geometry": shape.__geo_interface__,
        }))
        regions.append(Region(key, str(gj.resolve())))

    scope = f" for {only_state}" if only_state else ""
    log(f"fetched {len(regions)} US county polygons{scope} from TIGER {TIGER_YEAR}")
    return regions
