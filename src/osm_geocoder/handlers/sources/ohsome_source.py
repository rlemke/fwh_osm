"""ohsome-planet source adapter — OSM **history** from local/remote GeoParquet.

Provides a unified source interface over `ohsome-planet
<https://github.com/GIScience/ohsome-planet>`_ output (HeiGIT's conversion of the
full OSM history PBF to GeoParquet), mapping each *contribution* row into the SAME
unified ``osm.*`` feature schemas as the PBF / PostGIS / GeoJSON / Overture
adapters. Any downstream analysis facet works unchanged.

Why this adapter exists at all
------------------------------
The other four adapters see a SNAPSHOT. ohsome-planet rows are contributions: each
carries ``valid_from`` / ``valid_to``, so the same dataset answers "what did this
region look like on 2019-06-01" and "what changed between two dates". That is the
one capability none of the existing sources can offer, and it is why ``as_of`` and
``since``/``until`` live on the SOURCE schema rather than on a separate facet —
every category extractor becomes time-travelable for free instead of growing a
parallel set of temporal twins.

Mapping is unusually direct, and deliberately so
------------------------------------------------
ohsome rows carry OSM's own ``tags`` map, so the unified property dict is the tags
themselves plus contribution metadata under stable ``osm_*`` keys. Contrast the
Overture adapter, which must translate Overture's category vocabulary back into OSM
tag values and can only approximate. Here ``amenity=cafe`` is literally
``tags["amenity"] == "cafe"``, so a category filter is exact rather than a mapping
guess.

Design — thin, swappable reader vs. handler logic
-------------------------------------------------
The read is isolated behind ONE function, mirroring ``overture_source``:

    _read_ohsome_records(source: dict, tag_filter: dict | None) -> Iterable[dict]

Each yielded record is ``{"geometry": <GeoJSON geom>, "properties": {...}}``.
Everything else — category filtering, staging, result-dict construction, caching,
dispatch — is reader-agnostic and fully exercised offline. Tests monkeypatch the
reader with synthetic ohsome-shaped records, so no optional dependency is needed.

Real reads need pyarrow (Parquet) and shapely (WKB → GeoJSON); that lives in the
``[ohsome]`` extra. When missing, the reader raises ``OhsomeDependencyError``
rather than returning empty — a caller must never read "no deps installed" as
"no features found". That failure mode is exactly the one this repo keeps finding
in other people's pipelines; it is not going to be introduced here.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ..shared._output import finalize_output_file
from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

NAMESPACE = "osm.Source.OhsomePlanet"

_LOCAL_OUTPUT = os.environ.get("FW_LOCAL_OUTPUT_DIR", "/tmp")

#: Where the converted parquet lives. A local directory or an ``s3://`` prefix;
#: ohsome-planet writes a partitioned dataset either way.
DEFAULT_DATASET = os.environ.get("FW_OHSOME_PARQUET", "")


class OhsomeDependencyError(RuntimeError):
    """Raised when a real ohsome-planet read is attempted without pyarrow/shapely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Source payload
# ---------------------------------------------------------------------------


def _source_from_payload(payload: dict) -> dict:
    src = dict(payload.get("source") or {})
    src.setdefault("dataset", DEFAULT_DATASET)
    src.setdefault("region", "unknown")
    src.setdefault("min_lon", -180.0)
    src.setdefault("min_lat", -90.0)
    src.setdefault("max_lon", 180.0)
    src.setdefault("max_lat", 90.0)
    # "" means "current state" — the `latest` rows only.
    src.setdefault("as_of", "")
    src.setdefault("since", "")
    src.setdefault("until", "")
    return src


def _bbox(source: dict) -> tuple[float, float, float, float]:
    return (
        float(source["min_lon"]),
        float(source["min_lat"]),
        float(source["max_lon"]),
        float(source["max_lat"]),
    )


def _temporal_key(source: dict) -> dict:
    """The part of the cache key that makes a time-travelled read distinct.

    Without this an ``as_of=2019`` read would serve a cached ``latest`` result —
    the same output path, silently wrong data. Time is part of the identity of
    the answer here, not a display option.
    """
    return {
        "as_of": source.get("as_of", ""),
        "since": source.get("since", ""),
        "until": source.get("until", ""),
    }


def _output_path(category: str, subcategory: str, source: dict) -> str:
    region = source.get("region", "unknown")
    stamp = source.get("as_of") or source.get("since") or "latest"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(stamp))
    base = os.environ.get("FW_OSM_OUTPUT_BASE", _LOCAL_OUTPUT)
    return f"{base}/ohsome/{region}/{category}_{subcategory}_{safe}.geojson"


# ---------------------------------------------------------------------------
# Swappable reader — the ONLY part that touches pyarrow/shapely + the dataset.
# ---------------------------------------------------------------------------


def _has_reader_dep() -> bool:
    try:
        import pyarrow.dataset  # noqa: F401
        import shapely  # noqa: F401

        return True
    except ImportError:
        return False


def _read_ohsome_records(source: dict, tag_filter: dict | None = None) -> Iterable[dict]:
    """Read ohsome-planet contribution rows as GeoJSON-shaped records.

    Yields ``{"geometry": <GeoJSON geom>, "properties": {...}}``. Properties are
    the row's OSM ``tags`` plus contribution metadata under ``osm_*`` keys.

    The single swappable seam: the real implementation streams GeoParquet via
    pyarrow and converts WKB with shapely; tests monkeypatch it. Raises
    :class:`OhsomeDependencyError` when the optional backend is missing — never
    returns empty silently.
    """
    if not _has_reader_dep():
        raise OhsomeDependencyError(
            "Reading ohsome-planet GeoParquet requires the 'ohsome' extra "
            "(pyarrow + shapely). Install with: pip install 'osm-geocoder[ohsome]'. "
            "Refusing to return an empty result so this is not mistaken for "
            "'no features found'."
        )
    return _read_ohsome_records_pyarrow(source, tag_filter)


def _read_ohsome_records_pyarrow(
    source: dict, tag_filter: dict | None
) -> Iterable[dict]:  # pragma: no cover - requires pyarrow + shapely + a dataset
    """Stream ohsome-planet GeoParquet, projecting each row to GeoJSON shape."""
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    from shapely import from_wkb
    from shapely.geometry import mapping

    dataset_uri = source.get("dataset") or ""
    if not dataset_uri:
        raise OhsomeDependencyError(
            "No ohsome-planet dataset configured. Set FW_OHSOME_PARQUET or pass "
            "source.dataset (a directory or s3:// prefix of ohsome-planet output)."
        )

    dataset = ds.dataset(dataset_uri, format="parquet")
    min_lon, min_lat, max_lon, max_lat = _bbox(source)

    # bbox columns are a cheap pre-filter pushed into the scan; the row's own
    # bbox is the element's extent, so this is an INTERSECTS test, not contains.
    expr = (
        (pc.field("bbox", "xmax") >= min_lon)
        & (pc.field("bbox", "xmin") <= max_lon)
        & (pc.field("bbox", "ymax") >= min_lat)
        & (pc.field("bbox", "ymin") <= max_lat)
    )

    as_of, since, until = source.get("as_of"), source.get("since"), source.get("until")
    if as_of:
        # A version is live at T when valid_from <= T < valid_to. valid_to is
        # null for the currently-visible version, so that arm must be kept.
        ts = _to_ts(as_of)
        expr = expr & (pc.field("valid_from") <= ts)
        expr = expr & ((pc.field("valid_to") > ts) | pc.field("valid_to").is_null())
    elif since or until:
        if since:
            expr = expr & (pc.field("valid_from") >= _to_ts(since))
        if until:
            expr = expr & (pc.field("valid_from") < _to_ts(until))
    else:
        expr = expr & (pc.field("status") == "latest")

    for batch in dataset.to_batches(filter=expr):
        for row in batch.to_pylist():
            wkb = row.get("geometry")
            if not wkb:
                continue
            try:
                geom = mapping(from_wkb(wkb))
            except Exception:  # a single unreadable geometry must not kill the scan
                continue
            props = _row_properties(row)
            if tag_filter and not _matches(props, tag_filter):
                continue
            yield {"geometry": geom, "properties": props}


def _to_ts(value: str):  # pragma: no cover - trivial, exercised via the real path
    """ISO-8601 → datetime for a pyarrow timestamp comparison."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _row_properties(row: dict) -> dict:
    """OSM tags plus contribution metadata, flattened to GeoJSON properties.

    Tags come FIRST and metadata second, under reserved ``osm_``/``changeset_``
    prefixes, so a downstream facet filtering on ``amenity`` or ``highway`` sees
    exactly what the PBF adapter would give it. Metadata cannot shadow a tag.
    """
    props: dict = dict(row.get("tags") or {})
    user = row.get("user") or {}
    changeset = row.get("changeset") or {}
    props.update(
        {
            "osm_type": row.get("osm_type"),
            "osm_id": row.get("osm_id"),
            "osm_version": row.get("osm_version"),
            "osm_status": row.get("status"),
            "osm_contrib_type": row.get("contrib_type"),
            "osm_valid_from": _iso(row.get("valid_from")),
            "osm_valid_to": _iso(row.get("valid_to")),
            "osm_user": user.get("name"),
            "osm_user_id": user.get("id"),
            "changeset_id": changeset.get("id"),
            "changeset_editor": changeset.get("editor"),
            "changeset_hashtags": ",".join(changeset.get("hashtags") or []),
        }
    )
    for key in ("area", "length", "area_delta", "length_delta"):
        if row.get(key) is not None:
            props[f"osm_{key}"] = row[key]
    countries = row.get("countries")
    if countries:
        props["osm_countries"] = ",".join(countries)
    return {k: v for k, v in props.items() if v is not None}


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _matches(props: dict, tag_filter: dict) -> bool:
    for key, allowed in tag_filter.items():
        val = props.get(key)
        if val is None:
            return False
        if allowed and val not in allowed:
            return False
    return True


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _stage_features(
    records: Iterable[dict],
    output_path: str,
    map_props: Callable[[dict], dict | None],
    *,
    heartbeat=None,
) -> int:
    """Map records to unified GeoJSON features and stage to ``output_path``.

    Streams to a LOCAL temp then finalizes, so an ``s3://`` destination never
    sees a partial object (object stores have no partial writes).
    """
    fd, tmp = tempfile.mkstemp(suffix=".geojson")
    os.close(fd)
    count = 0
    with open(tmp, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for rec in records:
            if heartbeat:
                heartbeat()
            mapped = map_props(rec.get("properties", {}))
            if mapped is None:
                continue
            if count > 0:
                f.write(",\n")
            json.dump(
                {"type": "Feature", "geometry": rec.get("geometry"), "properties": mapped}, f
            )
            count += 1
        f.write("\n]}\n")
    finalize_output_file(tmp, output_path)
    return count


# ---------------------------------------------------------------------------
# Category extractors
#
# Unlike the Overture adapter — which needs a bespoke function per theme because
# Overture's vocabulary differs from OSM's — ohsome rows carry OSM tags verbatim.
# So every category is the same operation with a different tag key, and the
# per-category code is a table, not eight near-copies.
# ---------------------------------------------------------------------------

_AMENITY_CATEGORIES = {
    "food": ["restaurant", "cafe", "fast_food", "bar", "pub"],
    "health": ["hospital", "clinic", "doctors", "pharmacy", "dentist"],
    "education": ["school", "university", "college", "kindergarten", "library"],
    "transport": ["bus_station", "fuel", "parking", "charging_station"],
    "finance": ["bank", "atm", "bureau_de_change"],
}

_ROAD_CLASSES = {
    "motorway": ["motorway", "motorway_link"],
    "trunk": ["trunk", "trunk_link"],
    "primary": ["primary", "primary_link"],
    "secondary": ["secondary", "secondary_link"],
    "residential": ["residential", "living_street", "unclassified"],
}

_PARK_TAGS = {"park": ("leisure", ["park", "garden", "nature_reserve"]),
              "protected": ("boundary", ["protected_area", "national_park"])}

_PLACE_TYPES = {
    "city": ["city"],
    "town": ["town"],
    "village": ["village", "hamlet"],
    "all": [],
}

_ADMIN_LEVELS = {"country": "2", "state": "4", "county": "6", "city": "8"}


def _tag_extract(
    payload: dict,
    *,
    facet: str,
    tag_key: str,
    allowed: list[str] | None,
    category: str,
    out_kind: str,
    extra_result: Callable[[list[dict]], dict] | None = None,
    result_fields: dict | None = None,
) -> dict:
    """One category extraction: filter by an OSM tag, stage GeoJSON, cache, report."""
    source = _source_from_payload(payload)
    step_log = payload.get("_step_log")
    qualified = f"{NAMESPACE}.{facet}"

    cache_key = {
        "dataset": source["dataset"],
        "bbox": list(_bbox(source)),
        "region": source["region"],
        **_temporal_key(source),
    }
    dyn = {"category": category, "tag": tag_key}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    when = source.get("as_of") or (
        f"{source.get('since')}..{source.get('until')}"
        if source.get("since") or source.get("until")
        else "latest"
    )
    if step_log:
        step_log(f"OhsomePlanet.{facet}: reading {category} for {source['region']} @ {when}")

    allow = set(allowed) if allowed else None
    kept: list[dict] = []

    def map_props(props: dict) -> dict | None:
        val = props.get(tag_key)
        if not val:
            return None
        if allow is not None and val not in allow:
            return None
        kept.append(props)
        return dict(props)

    records = _read_ohsome_records(source, {tag_key: list(allow) if allow else []})
    out = _output_path(out_kind, category, source)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

    result = {
        "output_path": out,
        "feature_count": count,
        "format": "GeoJSON",
        "extraction_date": _now(),
        # The temporal position this answer is FOR. Two runs of the same facet
        # over the same region legitimately differ; without this the output is
        # undated and a stale file is indistinguishable from a fresh one.
        "as_of": source.get("as_of") or "",
        "since": source.get("since") or "",
        "until": source.get("until") or "",
    }
    if result_fields:
        result.update(result_fields)
    if extra_result:
        result.update(extra_result(kept))

    rv = {"result": result}
    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"OhsomePlanet.{facet}: {count} features extracted", level="success")
    return rv


def _num(props: dict, key: str) -> float:
    try:
        return float(props.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_amenities(payload: dict) -> dict:
    category = payload.get("category", "all")
    return _tag_extract(
        payload,
        facet="ExtractAmenities",
        tag_key="amenity",
        allowed=_AMENITY_CATEGORIES.get(category) if category != "all" else None,
        category=category,
        out_kind="amenities",
        result_fields={
            "amenity_category": category,
            "amenity_types": ",".join(_AMENITY_CATEGORIES.get(category, [])),
        },
    )


def _extract_buildings(payload: dict) -> dict:
    building_type = payload.get("building_type", "all")
    return _tag_extract(
        payload,
        facet="ExtractBuildings",
        tag_key="building",
        allowed=None if building_type == "all" else [building_type],
        category=building_type,
        out_kind="buildings",
        result_fields={"building_type": building_type},
        extra_result=lambda kept: {
            "total_area_km2": round(sum(_num(p, "osm_area") for p in kept) / 1e6, 4),
            "with_height_data": sum(1 for p in kept if p.get("height")),
        },
    )


def _extract_roads(payload: dict) -> dict:
    road_class = payload.get("road_class", "all")
    return _tag_extract(
        payload,
        facet="ExtractRoads",
        tag_key="highway",
        allowed=_ROAD_CLASSES.get(road_class) if road_class != "all" else None,
        category=road_class,
        out_kind="roads",
        result_fields={"road_class": road_class},
        extra_result=lambda kept: {
            "total_length_km": round(sum(_num(p, "osm_length") for p in kept) / 1000, 3),
            "with_speed_limit": sum(1 for p in kept if p.get("maxspeed")),
        },
    )


def _extract_parks(payload: dict) -> dict:
    park_type = payload.get("park_type", "park")
    tag_key, allowed = _PARK_TAGS.get(park_type, _PARK_TAGS["park"])
    return _tag_extract(
        payload,
        facet="ExtractParks",
        tag_key=tag_key,
        allowed=allowed,
        category=park_type,
        out_kind="parks",
        result_fields={
            "park_type": park_type,
            "protect_classes": payload.get("protect_classes", "*"),
        },
        extra_result=lambda kept: {
            "total_area_km2": round(sum(_num(p, "osm_area") for p in kept) / 1e6, 4)
        },
    )


def _extract_boundaries(payload: dict) -> dict:
    boundary_type = payload.get("boundary_type", "country")
    level = _ADMIN_LEVELS.get(boundary_type)
    return _tag_extract(
        payload,
        facet="ExtractBoundaries",
        tag_key="admin_level",
        allowed=[level] if level else None,
        category=boundary_type,
        out_kind="boundaries",
        result_fields={
            "boundary_type": boundary_type,
            "admin_levels": level or "*",
        },
    )


def _extract_population(payload: dict) -> dict:
    place_type = payload.get("place_type", "all")
    return _tag_extract(
        payload,
        facet="ExtractPopulation",
        tag_key="place",
        allowed=_PLACE_TYPES.get(place_type) or None,
        category=place_type,
        out_kind="population",
        result_fields={
            "place_type": place_type,
            "min_population": payload.get("min_population", 0),
            "max_population": payload.get("max_population", 0),
            "filter_applied": bool(payload.get("min_population") or payload.get("max_population")),
        },
        extra_result=lambda kept: {"original_count": len(kept)},
    )


def _extract_routes(payload: dict) -> dict:
    route_type = payload.get("route_type", "all")
    return _tag_extract(
        payload,
        facet="ExtractRoutes",
        tag_key="route",
        allowed=None if route_type == "all" else [route_type],
        category=route_type,
        out_kind="routes",
        result_fields={
            "route_type": route_type,
            "network_level": payload.get("network", "*"),
            "include_infrastructure": payload.get("include_infrastructure", False),
        },
    )


def _extract_pois(payload: dict) -> dict:
    return _tag_extract(
        payload,
        facet="ExtractPOIs",
        tag_key="amenity",
        allowed=None,
        category="all",
        out_kind="pois",
    )


# ---------------------------------------------------------------------------
# ExtractChanges — the facet no snapshot source can offer
# ---------------------------------------------------------------------------


_CONTRIB_TYPES = ("CREATION", "DELETION", "TAG", "GEOMETRY", "TAG_GEOMETRY")


def _extract_changes(payload: dict) -> dict:
    """Contributions in a time window, as GeoJSON features.

    The PBF / PostGIS / GeoJSON / Overture adapters cannot express this at all:
    they see one state of the world. Here every row already IS an edit, carrying
    who made it, in which changeset, with which editor, and what kind of change
    (``contrib_type``). That is the raw material for the change-detection,
    corporate-editing and vandalism questions the OSM research community is
    currently active on — and it needs no extra pipeline, only a filter.

    ``since``/``until`` come off the SOURCE (see the module docstring), so the
    same window semantics apply here as to a time-travelled category extract.
    """
    source = _source_from_payload(payload)
    step_log = payload.get("_step_log")
    qualified = f"{NAMESPACE}.ExtractChanges"

    kinds = [k.strip().upper() for k in str(payload.get("contrib_types", "")).split(",") if k.strip()]
    unknown = [k for k in kinds if k not in _CONTRIB_TYPES]
    if unknown:
        # Fail loudly: a typo'd contrib_type would otherwise filter everything
        # out and read as "no edits in this window", which is a wrong answer
        # rather than an empty one.
        raise ValueError(
            f"unknown contrib_types {unknown}; expected any of {list(_CONTRIB_TYPES)}"
        )

    if not (source.get("since") or source.get("until")):
        raise ValueError(
            "ExtractChanges needs source.since and/or source.until — without a "
            "window this would stage every contribution in the dataset."
        )

    cache_key = {
        "dataset": source["dataset"],
        "bbox": list(_bbox(source)),
        "region": source["region"],
        **_temporal_key(source),
    }
    dyn = {"contrib_types": ",".join(kinds) or "all", "tag": payload.get("tag_key", "")}
    hit = cached_result(qualified, cache_key, dyn, step_log)
    if hit is not None:
        return hit

    if step_log:
        step_log(
            f"OhsomePlanet.ExtractChanges: {source['region']} "
            f"{source.get('since') or '-'}..{source.get('until') or 'now'}"
        )

    tag_key = payload.get("tag_key") or ""
    wanted = set(kinds) if kinds else None
    seen: dict[str, int] = {}
    editors: set[str] = set()
    users: set[str] = set()

    def map_props(props: dict) -> dict | None:
        kind = props.get("osm_contrib_type")
        if wanted is not None and kind not in wanted:
            return None
        if tag_key and not props.get(tag_key):
            return None
        seen[kind] = seen.get(kind, 0) + 1
        if props.get("changeset_editor"):
            editors.add(props["changeset_editor"])
        if props.get("osm_user"):
            users.add(props["osm_user"])
        return dict(props)

    records = _read_ohsome_records(source, {tag_key: []} if tag_key else None)
    out = _output_path("changes", ",".join(kinds) or "all", source)
    count = _stage_features(records, out, map_props, heartbeat=payload.get("_task_heartbeat"))

    rv = {
        "result": {
            "output_path": out,
            "feature_count": count,
            "since": source.get("since") or "",
            "until": source.get("until") or "",
            "contrib_types": ",".join(kinds) or "all",
            "creations": seen.get("CREATION", 0),
            "deletions": seen.get("DELETION", 0),
            "tag_changes": seen.get("TAG", 0),
            "geometry_changes": seen.get("GEOMETRY", 0),
            "tag_geometry_changes": seen.get("TAG_GEOMETRY", 0),
            "distinct_editors": len(editors),
            "distinct_users": len(users),
            "format": "GeoJSON",
            "extraction_date": _now(),
        }
    }
    save_result_meta(qualified, cache_key, dyn, rv)
    if step_log:
        step_log(f"OhsomePlanet.ExtractChanges: {count} contributions", level="success")
    return rv


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

OHSOME_DISPATCH: dict[str, Callable] = {
    f"{NAMESPACE}.ExtractRoutes": _extract_routes,
    f"{NAMESPACE}.ExtractAmenities": _extract_amenities,
    f"{NAMESPACE}.ExtractRoads": _extract_roads,
    f"{NAMESPACE}.ExtractParks": _extract_parks,
    f"{NAMESPACE}.ExtractBuildings": _extract_buildings,
    f"{NAMESPACE}.ExtractBoundaries": _extract_boundaries,
    f"{NAMESPACE}.ExtractPopulation": _extract_population,
    f"{NAMESPACE}.ExtractPOIs": _extract_pois,
    f"{NAMESPACE}.ExtractChanges": _extract_changes,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = OHSOME_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown ohsome-planet source facet: {facet_name}")
    return handler(payload)
