"""Tier populated places by population into disjoint zoom bands.

Pure compute. Reads a GeoJSON FeatureCollection of populated places
(typically produced by ``osm.Population.AllPopulatedPlaces`` /
``Cities`` / etc), assigns each feature to the *highest* zoom tier
whose population threshold it meets, computes a population-scaled
bounding box, and emits a new FeatureCollection where every feature
carries the zoom, tier, name, country, lon/lat, and bbox.

Tiers are evaluated highest-population first, so each city is in
exactly one group.

No I/O outside the input/output paths it is given. Safe to call
from the CLI, from the handler, or from a test.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .storage import get_storage

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    """One zoom band: cities with population >= ``min_population`` land at ``zoom``."""

    zoom: int
    min_population: int


# Defaults match the user's request: 4 disjoint bands, evaluated top-down.
DEFAULT_TIERS: tuple[Tier, ...] = (
    Tier(zoom=3, min_population=5_000_000),
    Tier(zoom=6, min_population=1_000_000),
    Tier(zoom=8, min_population=500_000),
    Tier(zoom=10, min_population=10_000),
)


@dataclass
class TierResult:
    output_path: str
    total_count: int
    tier_counts: dict[int, int] = field(default_factory=dict)
    extraction_date: str = ""
    format: str = "GeoJSON"


def _parse_population(value) -> int | None:
    """Pull an int out of OSM population tag values that may be messy."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    digits = "".join(c for c in str(value) if c.isdigit())
    return int(digits) if digits else None


def assign_tier(population: int, tiers: tuple[Tier, ...]) -> Tier | None:
    """Pick the highest-population tier the city qualifies for, or None."""
    # tiers are passed already sorted descending by min_population
    for tier in tiers:
        if population >= tier.min_population:
            return tier
    return None


def city_bbox(lon: float, lat: float, population: int) -> tuple[float, float, float, float]:
    """Approximate (W, S, E, N) bbox around a city using a population-scaled radius.

    Assumes a coarse urban density of 5 000 people/km² (typical mid-density city).
    Radius is clamped to [0.5 km, 50 km] so a hamlet still has a usable footprint
    and a megacity does not balloon past metropolitan scale. Real city extents
    would come from the admin boundary polygon; this point-derived box is good
    enough to drive a zoom/pan to the city on a web map.
    """
    if population <= 0:
        radius_km = 1.0
    else:
        density_per_km2 = 5000.0
        area_km2 = population / density_per_km2
        radius_km = math.sqrt(area_km2 / math.pi)
    radius_km = max(0.5, min(50.0, radius_km))

    dlat = radius_km / 111.0
    # Avoid division-by-near-zero at the poles.
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    dlon = radius_km / (111.0 * cos_lat)
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


_COUNTRY_KEYS = ("addr:country", "is_in:country", "ISO3166-1", "country")


def _extract_country(props: dict) -> str:
    for key in _COUNTRY_KEYS:
        value = props.get(key)
        if value:
            return str(value)
    # "is_in" sometimes carries comma-separated parents — last token is usually the country
    is_in = props.get("is_in")
    if is_in:
        parts = [p.strip() for p in str(is_in).split(",") if p.strip()]
        if parts:
            return parts[-1]
    return ""


def _annotate(
    feature: dict, tier: Tier, population: int
) -> dict | None:
    """Return a new Feature with zoom/tier/bbox/name/country fields filled in."""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        return None

    props = dict(feature.get("properties") or {})
    name = (
        props.get("name")
        or props.get("name:en")
        or props.get("official_name")
        or props.get("loc_name")
        or ""
    )
    country = _extract_country(props)
    place = props.get("place", "")
    wikidata = props.get("wikidata", "")
    wikipedia = props.get("wikipedia", "")
    bbox = city_bbox(lon, lat, population)

    out_props = {
        # Tiering result
        "zoom": tier.zoom,
        "tier_min_population": tier.min_population,
        # Identity / location
        "name": name,
        "country": country,
        "place": place,
        "population": population,
        "lon": lon,
        "lat": lat,
        "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
        # Useful provenance carried through
        "osm_id": props.get("osm_id"),
        "wikidata": wikidata,
        "wikipedia": wikipedia,
    }
    # Pass through any other tag fields without overwriting the structured ones.
    for k, v in props.items():
        if k not in out_props:
            out_props[k] = v
    return {
        "type": "Feature",
        "properties": out_props,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "bbox": list(bbox),
    }


def tier_cities(
    input_path: str | Path,
    output_path: str | Path,
    tiers: tuple[Tier, ...] = DEFAULT_TIERS,
) -> TierResult:
    """Tier a GeoJSON FeatureCollection of populated places.

    Reads ``input_path``, assigns each feature with a parseable population
    to the highest qualifying tier, drops features that fall below the
    lowest threshold, writes the annotated FeatureCollection to
    ``output_path``, and returns the per-tier counts.
    """
    input_path = str(input_path)
    output_path = str(output_path)

    sorted_tiers = tuple(sorted(tiers, key=lambda t: t.min_population, reverse=True))

    s = get_storage()
    data = json.loads(s.read_text(input_path))

    features_in = data.get("features") or []
    features_out: list[dict] = []
    counts: dict[int, int] = {t.zoom: 0 for t in sorted_tiers}

    for feature in features_in:
        props = feature.get("properties") or {}
        pop = _parse_population(props.get("population_value"))
        if pop is None:
            pop = _parse_population(props.get("population"))
        if pop is None:
            continue
        tier = assign_tier(pop, sorted_tiers)
        if tier is None:
            continue
        annotated = _annotate(feature, tier, pop)
        if annotated is None:
            continue
        features_out.append(annotated)
        counts[tier.zoom] = counts.get(tier.zoom, 0) + 1

    payload = {
        "type": "FeatureCollection",
        "features": features_out,
        "tier_summary": {
            str(t.zoom): {
                "min_population": t.min_population,
                "count": counts.get(t.zoom, 0),
            }
            for t in sorted_tiers
        },
    }
    s.write_text_atomic(output_path, json.dumps(payload))

    log.info(
        "tier_cities: %d input features → %d tiered (counts=%s)",
        len(features_in),
        len(features_out),
        counts,
    )

    return TierResult(
        output_path=output_path,
        total_count=len(features_out),
        tier_counts=counts,
        extraction_date=datetime.now(UTC).isoformat(),
    )


@dataclass
class SplitResult:
    output_paths: dict[int, str]   # zoom → file path
    counts: dict[int, int]         # zoom → feature count
    format: str = "GeoJSON"
    extraction_date: str = ""


def split_by_tier(
    input_path: str | Path,
    output_dir: str | Path,
    zooms: tuple[int, ...],
    basename: str = "tier",
) -> SplitResult:
    """Split a tiered cities GeoJSON into one FeatureCollection per zoom.

    Reads ``input_path`` (produced by :func:`tier_cities`), bucketizes its
    features by the ``zoom`` property, and writes one GeoJSON per zoom in
    ``zooms`` to ``output_dir`` as ``{basename}_z{zoom}.geojson``. Zooms
    listed but absent from the input still get a written file with an
    empty feature collection (so downstream tilers don't trip on missing
    paths).
    """
    input_path = str(input_path)
    output_dir = Path(output_dir)

    s = get_storage()
    data = json.loads(s.read_text(input_path))

    buckets: dict[int, list[dict]] = {z: [] for z in zooms}
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        z = props.get("zoom")
        if z is None:
            continue
        try:
            z = int(z)
        except (TypeError, ValueError):
            continue
        if z in buckets:
            buckets[z].append(feature)

    paths: dict[int, str] = {}
    counts: dict[int, int] = {}
    for zoom, features in buckets.items():
        path = str(output_dir / f"{basename}_z{zoom}.geojson")
        s.write_text_atomic(
            path,
            json.dumps({"type": "FeatureCollection", "features": features}),
        )
        paths[zoom] = path
        counts[zoom] = len(features)

    log.info(
        "split_by_tier: %d features → %s",
        sum(counts.values()),
        {z: counts[z] for z in zooms},
    )

    return SplitResult(
        output_paths=paths,
        counts=counts,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def parse_tier_spec(spec: str) -> tuple[Tier, ...]:
    """Parse a CLI-style tier spec ``zoom:min_pop,zoom:min_pop,...`` into a tuple."""
    out: list[Tier] = []
    for raw in spec.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            zoom_str, pop_str = chunk.split(":", 1)
            zoom = int(zoom_str.strip())
            pop = int(pop_str.strip())
        except ValueError as exc:
            raise ValueError(
                f"Invalid tier spec '{chunk}' — expected 'zoom:min_population'"
            ) from exc
        out.append(Tier(zoom=zoom, min_population=pop))
    if not out:
        raise ValueError("Empty tier spec")
    return tuple(out)
