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

from .cancellation import run_cancellable
import unicodedata
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
    """Geofabrik-style slug: German umlauts transliterated (ü→ue, ß→ss …), other
    diacritics stripped (é→e), then lowercased + hyphenated."""
    s = s.lower()
    for a, b in (("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# Trailing admin-type words to drop so a self-generated county slug matches Census
# TIGER's BARE county name (OSM "Alachua County" → "Alachua"). Without this, the
# TIGER county fallback can't dedupe and publishes BOTH "<x>-county" (self-gen from
# the OSM name) and "<x>" (TIGER) — a ~2× duplicate per county. US county types:
# County (most), Parish (LA), Borough / Census Area / Municipality / City and
# Borough (AK). Harmless elsewhere — no other country's L6 name ends in these.
_ADMIN_TYPE_SUFFIX = re.compile(
    r"\s+(county|parish|census area|city and borough|borough|municipality)$", re.I)


def _strip_admin_type(name: str) -> str:
    return _ADMIN_TYPE_SUFFIX.sub("", name).strip() or name


# --- Geofabrik-style keying -------------------------------------------------
# "Continent" is a Geofabrik grouping, not an OSM admin concept, so it comes from
# a static country→continent map keyed by ISO 3166-1 alpha-2. Geofabrik's grouping
# is idiosyncratic: Mexico + the Caribbean sit under central-america, Russia is its
# own top level, Turkey under europe. Countries absent here fall back to a flat key.
_ISO_CONTINENT: dict[str, str] = {}


def _reg(continent: str, codes: str) -> None:
    _ISO_CONTINENT.update({c: continent for c in codes.split()})


_reg("europe", "AL AD AT BY BE BA BG HR CY CZ DK EE FO FI FR DE GI GR GG HU IS IE "
               "IM IT JE XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO SM RS SK SI "
               "ES SE CH TR UA GB VA")
_reg("russia", "RU")
_reg("north-america", "CA US GL BM PM")
_reg("central-america", "AI AG AW BS BB BZ VG KY CR CU CW DM DO SV GD GP GT HT HN "
                        "JM MQ MX MS NI PA PR BL KN LC MF VC SX TT TC VI")
_reg("south-america", "AR BO BR CL CO EC FK GF GY PY PE SR UY VE")
_reg("africa", "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM "
               "GH GN GW KE LS LR LY MG MW ML MR MU MA MZ NA NE NG RW ST SN SC SL "
               "SO ZA SS SD TZ TG TN UG EH ZM ZW")
_reg("asia", "AF AM AZ BH BD BT BN KH CN GE HK IN ID IR IQ IL JP JO KZ KW KG LA LB "
             "MO MY MV MN MM NP KP OM PK PS PH QA SA SG KR LK SY TW TJ TH TL TM AE "
             "UZ VN YE")
_reg("australia-oceania", "AS AU CK FJ PF GU KI MH FM NR NC NZ NU NF MP PW PG PN WS "
                          "SB TK TO TV VU WF")

# Geofabrik country-PATH slug where it differs from the English name slug, and the
# country prefix for sub-national keys (the federal/large countries that have
# admin_level>=4 extracts worth pre-generating). Others fall back to a flat key.
_ISO_COUNTRY_SLUG: dict[str, str] = {
    "US": "us", "GB": "great-britain", "RU": "russia",
    "CA": "canada", "MX": "mexico", "BR": "brazil", "AR": "argentina",
    "DE": "germany", "FR": "france", "IT": "italy", "ES": "spain", "PL": "poland",
    "NL": "netherlands", "AT": "austria", "CH": "switzerland", "BE": "belgium",
    "AU": "australia", "IN": "india", "JP": "japan", "CN": "china",
}


def _geofabrik_key(name_local: str, name_en: str | None, iso: str | None,
                   admin_level: int) -> str:
    """Best-effort Geofabrik-exact key. Countries (level 2) use the ENGLISH name
    (germany); sub-regions (level 4+) use the LOCAL name (bayern, not bavaria) —
    matching Geofabrik's own inconsistent convention."""
    iso = (iso or "").upper()
    if admin_level == 2 and iso:
        cont = _ISO_CONTINENT.get(iso)
        if cont:
            return f"{cont}/{_ISO_COUNTRY_SLUG.get(iso, _slug(name_en or name_local))}"
    if admin_level >= 4 and "-" in iso:                 # ISO 3166-2, e.g. US-CA / DE-BY
        country_iso = iso.split("-", 1)[0]
        cont = _ISO_CONTINENT.get(country_iso)
        country = _ISO_COUNTRY_SLUG.get(country_iso)
        if cont and country:
            return f"{cont}/{country}/{_slug(name_local)}"
    return _slug(name_local)                             # flat fallback


def _run(cmd: list[str]) -> None:
    try:
        # Cancellable: this is the pass that kept running after a terminate.
        run_cancellable(cmd)
    except FileNotFoundError as exc:
        raise BoundaryError(f"required binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BoundaryError(f"command failed ({exc.returncode}): {' '.join(cmd[:3])}…") from exc


def generate_polygons(source: str, admin_level: int, dest: str, *,
                      country_prefix: str | None = None,
                      on_log: Callable[[str], None] | None = None) -> list[BoundaryRegion]:
    """Extract + assemble ``admin_level`` boundary polygons from ``source`` into ``dest``.

    Returns one :class:`BoundaryRegion` per successfully-assembled admin unit, each
    with a GeoJSON polygon that ``planet_bootstrap`` can extract with (it infers the
    GeoJSON file type from the extension).

    ``country_prefix`` (e.g. ``"north-america/mexico"``): when the caller already
    knows the source country (BuildAdminSet passes ``source_region``), key every
    sub-country unit as ``<country_prefix>/<local-name-slug>`` instead of via the
    ISO→continent lookup. That (a) keeps the prefix CONSISTENT with the source
    extract's own key — the ISO map is idiosyncratic (Mexico sits under
    ``central-america`` there but the extract lives at ``north-america/mexico``) —
    and (b) lets **county-level** units through: German *Kreise* (admin_level 6)
    carry no ISO 3166-2, so the ISO-hierarchy filter would drop all of them. At
    level ≤ 4 a real state still must carry ISO 3166-2 (else it's island noise
    mistagged at that level → dropped); at level ≥ 6 that requirement is lifted.
    Without ``country_prefix`` the standalone ISO→continent behaviour is unchanged.
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
    dropped = 0  # sub-national relations mistagged at this level (islands/reserves) with no
                 # resolvable ISO 3166-2 → they'd pollute the bucket root with flat keys.
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
            name_local = props.get("name")
            name_en = props.get("name:en")
            name = name_en or name_local
            if not name:
                continue
            iso = (props.get("ISO3166-1:alpha2") or props.get("ISO3166-2")
                   or props.get("ISO3166-1"))
            if country_prefix:
                # Known source country → key under it (consistent with the source
                # extract's key). Level ≤ 4: a real state carries ISO 3166-2; without
                # it, drop as island noise. Level ≥ 6 (counties): no ISO 3166-2 exists
                # for those, so keep them.
                has_iso2 = "-" in (iso or "").upper()
                if int(admin_level) <= 4 and not has_iso2:
                    dropped += 1
                    continue
                unit = name_local or name
                if int(admin_level) >= 6:
                    unit = _strip_admin_type(unit)   # "Alachua County" → "Alachua" (match TIGER)
                key = f"{country_prefix.strip('/')}/{_slug(unit)}"
            else:
                # Standalone: derive the continent/country prefix from ISO 3166-2.
                # A sub-national unit (level >= 4) that only produces a flat slug
                # lacks a country ISO — island/reserve noise mistagged at this level.
                key = _geofabrik_key(name_local or name, name_en, iso, int(admin_level))
                if int(admin_level) >= 4 and "/" not in key:
                    dropped += 1
                    continue
            if key in seen:
                continue
            seen.add(key)
            gj = dest_p / f"{key.replace('/', '__')}.geojson"
            gj.write_text(json.dumps({
                "type": "Feature",
                "properties": {"name": name, "iso": iso, "admin_level": admin_level},
                "geometry": feat["geometry"],
            }))
            regions.append(BoundaryRegion(key, str(gj.resolve()), name, int(admin_level), iso))

    suffix = f" (dropped {dropped} without a country ISO 3166-2)" if dropped else ""
    log(f"generated {len(regions)} admin_level={admin_level} polygons{suffix}")
    return regions
