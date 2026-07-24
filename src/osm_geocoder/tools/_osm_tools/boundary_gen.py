"""Generate region polygons from OSM admin boundaries — no external poly source.

The self-contained polygon source for the ``osm.planet`` pipeline. OSM carries
``boundary=administrative`` relations for essentially every admin unit on Earth
(``admin_level`` 2=country, 4=state/province, 6=county…), tagged with names and
ISO 3166 codes. This filters them out of a PBF (the planet, or a continent extract)
and assembles them into polygons via ``osmium export`` — so boundaries come from
the SAME OSM snapshot as the data being clipped, with universal coverage, and zero
dependency on osmfr's ``/polygons/`` or Census TIGER.

Relations that fail geometry assembly (broken/unclosed OSM boundaries) are silently
dropped by ``osmium export`` and simply don't appear — a graceful skip, not a crash.

Keys are the OSM name slug for now (+ ISO recorded); the Geofabrik continent prefix
(``europe/…``) is a thin mapping layer applied at integration time, since "continent"
is a Geofabrik convention, not an OSM admin concept.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class BoundaryRegion:
    key: str            # OSM name slug (e.g. "germany", "california")
    poly: str           # absolute path to the GeoJSON polygon
    name: str
    admin_level: int
    iso: str | None


class BoundaryError(RuntimeError):
    """A boundary generation step failed (osmium filter/export)."""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise BoundaryError(f"required binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BoundaryError(f"command failed ({exc.returncode}): {' '.join(cmd[:3])}…") from exc


def generate_polygons(source: str, admin_level: int, dest: str, *,
                      on_log: Callable[[str], None] | None = None) -> list[BoundaryRegion]:
    """Extract + assemble ``admin_level`` boundary polygons from ``source`` into ``dest``.

    Returns one :class:`BoundaryRegion` per successfully-assembled admin unit, each
    with a GeoJSON polygon that ``planet_bootstrap`` can extract with (it infers the
    GeoJSON file type from the extension).
    """
    log = on_log or (lambda _m: None)
    dest_p = Path(dest)
    dest_p.mkdir(parents=True, exist_ok=True)
    admin_pbf = dest_p / f"_admin{admin_level}.osm.pbf"
    seq = dest_p / f"_admin{admin_level}.geojsonseq"

    # 1. Filter boundary relations at this level (reference-complete: tags-filter
    #    pulls in the member ways + their nodes so geometries can be built).
    log(f"filtering admin_level={admin_level} boundaries from {source}")
    _run(["osmium", "tags-filter", source, f"r/admin_level={admin_level}",
          "-o", str(admin_pbf), "--overwrite"])

    # 2. Assemble to polygons. osmium export builds the multipolygon geometry for
    #    each boundary relation and DROPS ones it can't assemble (graceful skip).
    log("assembling boundary polygons (osmium export)")
    _run(["osmium", "export", str(admin_pbf), "-o", str(seq), "-f", "geojsonseq",
          "--geometry-types=polygon", "-a", "type,id", "--overwrite"])

    # 3. Parse the GeoJSONSeq: keep boundary=administrative at this level, dedupe,
    #    write one GeoJSON polygon per region.
    regions: list[BoundaryRegion] = []
    seen: set[str] = set()
    with open(seq, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")  # RFC 8142 record separator
            if not line:
                continue
            feat = json.loads(line)
            props = feat.get("properties", {})
            if props.get("boundary") != "administrative":
                continue
            if str(props.get("admin_level")) != str(admin_level):
                continue
            name = props.get("name:en") or props.get("name")
            if not name:
                continue
            key = _slug(name)
            if key in seen:
                continue
            seen.add(key)
            iso = (props.get("ISO3166-1:alpha2") or props.get("ISO3166-2")
                   or props.get("ISO3166-1"))
            gj = dest_p / f"{key}.geojson"
            gj.write_text(json.dumps({
                "type": "Feature",
                "properties": {"name": name, "iso": iso, "admin_level": admin_level},
                "geometry": feat["geometry"],
            }))
            regions.append(BoundaryRegion(key, str(gj.resolve()), name, int(admin_level), iso))

    log(f"generated {len(regions)} admin_level={admin_level} polygons")
    return regions
