"""Category-based feature extraction from cached OSM PBFs.

Produces one pre-filtered GeoJSON cache per *category* (water, parks,
forests, etc.) so downstream consumers read a small, already-filtered
file instead of re-parsing the full PBF every time.

Each category is its own cache_type under the ``osm`` namespace. Cache
paths mirror the Geofabrik hierarchy of the source PBF::

    cache/osm/water/north-america/us/california-latest.geojsonseq
    cache/osm/water/north-america/us/california-latest.geojsonseq.meta.json

Two osmium passes with a local staging dir:

1. ``osmium tags-filter`` — produces a filtered ``.osm.pbf`` with only
   entities matching the category's tag expression, plus referenced
   nodes/ways for geometry assembly.
2. ``osmium export -f geojsonseq`` — converts the filtered PBF to
   streaming GeoJSON. Multipolygon relations assemble into
   ``MultiPolygon`` geometries.

Cache validity requires:

- The source PBF's SHA-256 still matches what the sidecar recorded, AND
- The category definition's ``filter_version`` still matches. Bumping
  ``filter_version`` invalidates all cache entries for that category.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _lib import sidecar
from _lib.storage import LocalStorage

NAMESPACE = "osm"
SOURCE_CACHE_TYPE = "pbf"
DEFAULT_FORMAT = "geojsonseq"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class CategoryDef:
    """Definition of an extractable feature category.

    - ``name``: cache_type identifier, CLI identifier, FFL reference.
    - ``facet_name``: FFL event facet name (e.g. ``ExtractWater``).
    - ``return_param``: FFL return parameter name (e.g. ``water``).
    - ``description``: one-line summary.
    - ``filter_expression``: osmium ``tags-filter`` argument list.
    - ``filter_version``: bump to invalidate existing entries.
    """

    name: str
    facet_name: str
    return_param: str
    description: str
    filter_expression: str
    filter_version: int = 1


CATEGORIES: dict[str, CategoryDef] = {
    "water": CategoryDef(
        name="water",
        facet_name="ExtractWater",
        return_param="water",
        description="Lakes, ponds, reservoirs, rivers, streams, canals.",
        filter_expression=(
            "nwr/natural=water "
            "nwr/waterway=river,stream,canal,drain,ditch "
            "nwr/water"
        ),
        filter_version=1,
    ),
    "protected_areas": CategoryDef(
        name="protected_areas",
        facet_name="ExtractProtectedAreas",
        return_param="protectedAreas",
        description="National parks, state parks, wilderness areas, nature reserves.",
        filter_expression=(
            "r/boundary=national_park,protected_area "
            "nwr/leisure=nature_reserve"
        ),
        filter_version=1,
    ),
    "parks": CategoryDef(
        name="parks",
        facet_name="ExtractParks",
        return_param="parks",
        description=(
            "City-level parks, playgrounds, sports pitches, stadiums, gardens."
        ),
        filter_expression=(
            "nwr/leisure=park,playground,pitch,sports_centre,stadium,garden"
        ),
        filter_version=1,
    ),
    "forests": CategoryDef(
        name="forests",
        facet_name="ExtractForests",
        return_param="forests",
        description="Forests and wood-covered land.",
        filter_expression="nwr/natural=wood nwr/landuse=forest",
        filter_version=1,
    ),
    "roads_routable": CategoryDef(
        name="roads_routable",
        facet_name="ExtractRoadsRoutable",
        return_param="roadsRoutable",
        description=(
            "Full road network for routing — every highway=* way plus "
            "tagged junction/crossing nodes."
        ),
        filter_expression="nwr/highway",
        filter_version=1,
    ),
    "turn_restrictions": CategoryDef(
        name="turn_restrictions",
        facet_name="ExtractTurnRestrictions",
        return_param="turnRestrictions",
        description="OSM turn-restriction relations (type=restriction).",
        filter_expression="r/type=restriction",
        filter_version=1,
    ),
    "railways_routable": CategoryDef(
        name="railways_routable",
        facet_name="ExtractRailwaysRoutable",
        return_param="railwaysRoutable",
        description=(
            "Active rail network: heavy rail, light rail, subway, tram, "
            "narrow gauge, funicular, monorail."
        ),
        filter_expression=(
            "nwr/railway=rail,light_rail,subway,tram,"
            "narrow_gauge,funicular,monorail"
        ),
        filter_version=1,
    ),
    "cycle_routes": CategoryDef(
        name="cycle_routes",
        facet_name="ExtractCycleRoutes",
        return_param="cycleRoutes",
        description="Cycling route relations — on-road bike routes and MTB trails.",
        filter_expression="r/route=bicycle,mtb",
        filter_version=1,
    ),
    "hiking_routes": CategoryDef(
        name="hiking_routes",
        facet_name="ExtractHikingRoutes",
        return_param="hikingRoutes",
        description="Hiking/walking route relations — long-distance trails and foot paths.",
        filter_expression="r/route=hiking,foot",
        filter_version=1,
    ),
    "food": CategoryDef(
        name="food",
        facet_name="ExtractFood",
        return_param="food",
        description="Restaurants, cafes, bars, pubs, fast food, biergartens, food courts, ice-cream parlours.",
        filter_expression=(
            "nwr/amenity=restaurant,cafe,bar,pub,fast_food,biergarten,"
            "food_court,ice_cream"
        ),
        filter_version=1,
    ),
    "healthcare": CategoryDef(
        name="healthcare",
        facet_name="ExtractHealthcare",
        return_param="healthcare",
        description=(
            "Hospitals, clinics, pharmacies, doctors, dentists, veterinaries. "
            "Plus the Healthcare 2.0 'healthcare=*' namespace."
        ),
        filter_expression=(
            "nwr/amenity=hospital,clinic,pharmacy,doctors,dentist,veterinary "
            "nwr/healthcare"
        ),
        filter_version=1,
    ),
    "education": CategoryDef(
        name="education",
        facet_name="ExtractEducation",
        return_param="education",
        description=(
            "Schools, universities, colleges, kindergartens, libraries, "
            "childcare, driving/music/language schools."
        ),
        filter_expression=(
            "nwr/amenity=school,university,college,kindergarten,library,"
            "childcare,music_school,driving_school,language_school"
        ),
        filter_version=1,
    ),
    "government": CategoryDef(
        name="government",
        facet_name="ExtractGovernment",
        return_param="government",
        description=(
            "Town halls, courthouses, police, fire stations, post offices, "
            "embassies. Also 'office=government' buildings."
        ),
        filter_expression=(
            "nwr/amenity=townhall,courthouse,police,fire_station,"
            "post_office,embassy,public_building "
            "nwr/office=government"
        ),
        filter_version=1,
    ),
    "public_transport": CategoryDef(
        name="public_transport",
        facet_name="ExtractPublicTransport",
        return_param="publicTransport",
        description=(
            "Bus stops, tram stops, train/subway stations, ferry terminals, "
            "platform/stop_position nodes."
        ),
        filter_expression=(
            "nwr/public_transport "
            "nwr/amenity=bus_station,ferry_terminal "
            "nwr/highway=bus_stop "
            "nwr/railway=station,halt,tram_stop,subway_entrance"
        ),
        filter_version=1,
    ),
    "culture": CategoryDef(
        name="culture",
        facet_name="ExtractCulture",
        return_param="culture",
        description=(
            "Museums, galleries, artworks, attractions, viewpoints, arts "
            "centres, theatres, planetariums, and all 'historic=*' features."
        ),
        filter_expression=(
            "nwr/tourism=museum,gallery,artwork,attraction,viewpoint "
            "nwr/amenity=arts_centre,theatre,community_centre,planetarium "
            "nwr/historic"
        ),
        filter_version=1,
    ),
    "religion": CategoryDef(
        name="religion",
        facet_name="ExtractReligion",
        return_param="religion",
        description=(
            "Places of worship — churches, mosques, temples, synagogues, etc."
        ),
        filter_expression="nwr/amenity=place_of_worship",
        filter_version=1,
    ),
    "sports": CategoryDef(
        name="sports",
        facet_name="ExtractSports",
        return_param="sports",
        description=(
            "Sports centres, stadiums, pitches, fitness centres, swimming "
            "pools, ice rinks, tracks, golf courses, sports halls."
        ),
        filter_expression=(
            "nwr/leisure=sports_centre,stadium,pitch,fitness_centre,"
            "swimming_pool,ice_rink,track,golf_course,sports_hall"
        ),
        filter_version=1,
    ),
    "shopping": CategoryDef(
        name="shopping",
        facet_name="ExtractShopping",
        return_param="shopping",
        description="All retail — 'shop=*' for any value.",
        filter_expression="nwr/shop",
        filter_version=1,
    ),
    "accommodation": CategoryDef(
        name="accommodation",
        facet_name="ExtractAccommodation",
        return_param="accommodation",
        description=(
            "Hotels, motels, hostels, guest houses, chalets, apartments, "
            "camp sites, caravan sites, alpine/wilderness huts."
        ),
        filter_expression=(
            "nwr/tourism=hotel,motel,hostel,guest_house,chalet,apartment,"
            "camp_site,caravan_site,alpine_hut,wilderness_hut"
        ),
        filter_version=1,
    ),
    "finance": CategoryDef(
        name="finance",
        facet_name="ExtractFinance",
        return_param="finance",
        description="Banks, ATMs, currency exchange bureaus.",
        filter_expression="nwr/amenity=bank,atm,bureau_de_change",
        filter_version=1,
    ),
    "fuel_charging": CategoryDef(
        name="fuel_charging",
        facet_name="ExtractFuelCharging",
        return_param="fuelCharging",
        description="Gas stations and EV charging stations.",
        filter_expression="nwr/amenity=fuel,charging_station",
        filter_version=1,
    ),
    "parking": CategoryDef(
        name="parking",
        facet_name="ExtractParking",
        return_param="parking",
        description="Car, bicycle, and motorcycle parking areas.",
        filter_expression=(
            "nwr/amenity=parking,bicycle_parking,motorcycle_parking"
        ),
        filter_version=1,
    ),
    "entertainment": CategoryDef(
        name="entertainment",
        facet_name="ExtractEntertainment",
        return_param="entertainment",
        description=(
            "Cinemas, theatres, nightclubs, casinos, event venues, escape "
            "rooms, amusement arcades, bowling alleys."
        ),
        filter_expression=(
            "nwr/amenity=cinema,theatre,nightclub,casino,events_venue "
            "nwr/leisure=escape_game,amusement_arcade,bowling_alley"
        ),
        filter_version=1,
    ),
    "toilets": CategoryDef(
        name="toilets",
        facet_name="ExtractToilets",
        return_param="toilets",
        description="Public toilets, showers, drinking-water fountains.",
        filter_expression="nwr/amenity=toilets,shower,drinking_water",
        filter_version=1,
    ),
    "emergency": CategoryDef(
        name="emergency",
        facet_name="ExtractEmergency",
        return_param="emergency",
        description=(
            "Fire stations, police stations, and everything tagged with "
            "the 'emergency=*' namespace."
        ),
        filter_expression=(
            "nwr/emergency "
            "nwr/amenity=fire_station,police"
        ),
        filter_version=1,
    ),
}


_extract_locks: dict[tuple[str, str], threading.Lock] = {}
_extract_locks_guard = threading.Lock()


def _extract_lock(region: str, category: str) -> threading.Lock:
    key = (region, category)
    with _extract_locks_guard:
        lock = _extract_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _extract_locks[key] = lock
        return lock


@dataclass
class ExtractResult:
    region: str
    category: str
    path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    filter_version: int
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class ExtractionError(RuntimeError):
    """Raised when extraction fails."""


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def extract_rel_path(region: str) -> str:
    return f"{region}-latest.{DEFAULT_FORMAT}"


def extract_abs_path(region: str, category: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, category, extract_rel_path(region), s))


def _staging_dir(region: str, category: str, storage: Any = None) -> Path:
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-extract-staging" / category / safe
    out = extract_abs_path(region, category, storage)
    return out.with_name(out.name + ".staging")


def _osmium_version(osmium_bin: str) -> str:
    try:
        result = subprocess.run(
            [osmium_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or "").splitlines()
        return first_line[0].strip() if first_line else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha256_file(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def _count_features_geojsonseq(path: Path) -> int:
    count = 0
    with path.open("rb") as f:
        for line in f:
            if line.strip(b"\x1e \t\r\n"):
                count += 1
    return count


def is_up_to_date(
    region: str,
    category: str,
    pbf_side: dict,
    out_abs: Path,
    storage: Any = None,
) -> bool:
    """True if the cached extract reflects both source PBF SHA and filter_version."""
    s = storage or LocalStorage()
    cat = CATEGORIES[category]
    out_rel = extract_rel_path(region)
    existing = sidecar.read_sidecar(NAMESPACE, category, out_rel, s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    extra = existing.get("extra") or {}
    filt = extra.get("filter") or {}
    if filt.get("version") != cat.filter_version:
        return False
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == existing.get("size_bytes")


def extract_region(
    region: str,
    category: str,
    *,
    force: bool = False,
    osmium_bin: str = "osmium",
    storage: Any = None,
) -> ExtractResult:
    """Extract one category's features from a region's cached PBF."""
    if category not in CATEGORIES:
        raise ExtractionError(
            f"unknown category: {category!r}. "
            f"Valid: {', '.join(sorted(CATEGORIES))}"
        )
    cat = CATEGORIES[category]
    s = storage or LocalStorage()

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise ExtractionError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )
    src_pbf = pbf_abs_path(region, s)
    if not src_pbf.exists():
        raise ExtractionError(f"pbf file missing on disk: {src_pbf}")
    source_url = pbf_side.get("source", {}).get("url", "")

    with _extract_lock(region, category):
        out_abs = extract_abs_path(region, category, s)
        out_rel = extract_rel_path(region)

        if not force and is_up_to_date(region, category, pbf_side, out_abs, s):
            existing = sidecar.read_sidecar(NAMESPACE, category, out_rel, s) or {}
            extra = existing.get("extra") or {}
            return ExtractResult(
                region=region,
                category=category,
                path=str(out_abs),
                relative_path=out_rel,
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                feature_count=extra.get("feature_count", 0),
                filter_version=cat.filter_version,
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        out_abs.parent.mkdir(parents=True, exist_ok=True)
        staging = _staging_dir(region, category, s)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        filtered_pbf = staging / "filtered.osm.pbf"
        extract_out = staging / extract_rel_path(region).replace("/", "_")

        start = time.monotonic()
        try:
            filter_cmd = [
                osmium_bin,
                "tags-filter",
                "--overwrite",
                "-o",
                str(filtered_pbf),
                str(src_pbf),
                *cat.filter_expression.split(),
            ]
            subprocess.run(filter_cmd, check=True, capture_output=True, text=True)

            export_cmd = [
                osmium_bin,
                "export",
                "-f",
                DEFAULT_FORMAT,
                "-o",
                str(extract_out),
                "--overwrite",
                str(filtered_pbf),
            ]
            subprocess.run(export_cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            stderr = (exc.stderr or "").strip()
            raise ExtractionError(f"osmium step failed: {stderr or exc}") from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(extract_out)
        feature_count = _count_features_geojsonseq(extract_out)

        s.finalize_from_local(str(extract_out), str(out_abs))
        shutil.rmtree(staging, ignore_errors=True)

        generated_at = sidecar.utcnow_iso()
        side = sidecar.write_sidecar(
            NAMESPACE,
            category,
            out_rel,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source={
                "namespace": NAMESPACE,
                "cache_type": SOURCE_CACHE_TYPE,
                "relative_path": pbf_rel,
                "sha256": pbf_side.get("sha256"),
                "size_bytes": pbf_side.get("size_bytes"),
                "source_checksum": pbf_side.get("source", {}).get("source_checksum"),
                "source_timestamp": pbf_side.get("source", {}).get("source_timestamp"),
                "downloaded_at": pbf_side.get("source", {}).get("downloaded_at"),
            },
            tool={
                "command": "osmium tags-filter | osmium export",
                "osmium_version": _osmium_version(osmium_bin),
            },
            extra={
                "region": region,
                "category": category,
                "format": DEFAULT_FORMAT,
                "feature_count": feature_count,
                "filter": {
                    "kind": "osmium-tags-filter",
                    "expression": cat.filter_expression,
                    "version": cat.filter_version,
                },
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return ExtractResult(
            region=region,
            category=category,
            path=str(out_abs),
            relative_path=out_rel,
            size_bytes=size,
            sha256=sha256_hex,
            feature_count=feature_count,
            filter_version=cat.filter_version,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_url=source_url,
            source_pbf_path=str(src_pbf),
            sidecar=side,
        )


def to_osm_cache(result: ExtractResult) -> dict[str, Any]:
    """Map an ``ExtractResult`` to the ``OSMCache`` dict FFL handlers return."""
    return {
        "url": result.source_url,
        "path": result.path,
        "date": result.generated_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "extract",
    }
