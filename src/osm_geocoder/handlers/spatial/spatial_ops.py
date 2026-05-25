"""Spatial distance relations between two GeoJSON layers.

The compute core behind the ``osm.Spatial`` facets — the toolchain's "universal
verb" (Overpass ``around``, routing tables/isochrones, turf, PostGIS
``ST_DWithin`` / ``ST_Distance`` / KNN). Each operation relates a SUBJECT layer
to a REFERENCE layer by distance:

* :func:`within_distance` — keep subject features within ``distance`` of *any*
  reference feature (annotated with the nearest distance).
* :func:`beyond_distance` — keep subject features beyond ``distance`` from
  *every* reference feature (the "food desert" complement).
* :func:`nearest` — annotate every subject feature with the distance to its
  nearest reference feature (and that feature's ``name``); keep all.

Distances are metric, computed in a local **azimuthal-equidistant** projection
centered on the reference layer's centroid. AEQD preserves distances from the
projection center exactly and stays accurate over regional (state / metro)
extents — the scale these primitives compose at. For continental inputs, clip
first. The reference layer is indexed with a shapely ``STRtree`` so a query is
O(log n) per subject feature; subject features are streamed, so memory stays
proportional to the reference layer plus one subject feature.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ..shared._output import derive_output_path, ensure_dir, uri_stem
from ..shared.geojson_writer import GeoJSONStreamWriter, iter_geojson_features

log = logging.getLogger(__name__)

# Geometry stack. shapely>=2.0 (STRtree.query_nearest) and pyproj>=3.0 are
# declared dependencies; the guards keep import-time failures graceful so a
# runner missing the extras degrades to an explicit error instead of ImportError
# at module load (matching the radius_filter / osm_type_filter pattern).
try:
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as shapely_transform
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    HAS_SHAPELY = True
except ImportError:  # pragma: no cover - exercised only without the geo extras
    HAS_SHAPELY = False

try:
    from pyproj import CRS, Transformer

    HAS_PYPROJ = True
except ImportError:  # pragma: no cover
    HAS_PYPROJ = False


class Unit(Enum):
    """Distance units for spatial relations."""

    METERS = "meters"
    KILOMETERS = "kilometers"
    MILES = "miles"

    @classmethod
    def from_string(cls, value: str) -> Unit:
        """Parse a unit string (case-insensitive), accepting common aliases."""
        normalized = (value or "").lower().strip()
        aliases = {
            "m": cls.METERS,
            "meter": cls.METERS,
            "meters": cls.METERS,
            "km": cls.KILOMETERS,
            "kilometer": cls.KILOMETERS,
            "kilometers": cls.KILOMETERS,
            "mi": cls.MILES,
            "mile": cls.MILES,
            "miles": cls.MILES,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown unit: {value!r}")


_TO_METERS = {
    Unit.METERS: 1.0,
    Unit.KILOMETERS: 1000.0,
    Unit.MILES: 1609.344,
}


def to_meters(value: float, unit: Unit) -> float:
    """Convert a distance ``value`` in ``unit`` to meters."""
    return value * _TO_METERS[unit]


def from_meters(value: float, unit: Unit) -> float:
    """Convert a distance ``value`` in meters to ``unit``."""
    return value / _TO_METERS[unit]


@dataclass
class SpatialResult:
    """Result of a spatial-distance operation (mirrors the FFL SpatialResult)."""

    output_path: str
    feature_count: int
    original_count: int
    reference_count: int
    operation: str
    distance: float
    unit: str
    format: str = "GeoJSON"
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the handler return payload."""
        return {
            "output_path": self.output_path,
            "feature_count": self.feature_count,
            "original_count": self.original_count,
            "reference_count": self.reference_count,
            "operation": self.operation,
            "distance": self.distance,
            "unit": self.unit,
            "format": self.format,
            "extraction_date": self.extraction_date,
        }


def _local_metric_transformer(lon: float, lat: float):
    """Build a WGS84 -> local azimuthal-equidistant transformer centered on
    ``(lon, lat)``.

    AEQD preserves true distance from the projection center; for the regional
    extents these primitives operate on, off-center error stays small (well
    under a percent within a few hundred km). Coordinates come out in meters.
    """
    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    )
    wgs84 = CRS.from_epsg(4326)
    return Transformer.from_crs(wgs84, aeqd, always_xy=True).transform


def _load_reference(reference_path: str, heartbeat=None):
    """Load reference features into projected geometries + a spatial index.

    Returns ``(tree, geoms, names, transformer, reference_count)``. ``tree`` is
    ``None`` when the reference layer is empty — callers treat that as "no
    reference geometry exists" (within keeps nothing, beyond keeps everything).
    """
    from facetwork.runtime.storage import localize

    local_ref = localize(str(reference_path))

    raw_geoms = []
    names: list[str] = []
    # First pass: collect lon/lat extent so we can center the projection, and
    # keep the WGS84 geometries to project once we know the center.
    min_lon = min_lat = math.inf
    max_lon = max_lat = -math.inf
    for feature in iter_geojson_features(local_ref, heartbeat):
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception as exc:  # malformed geometry — skip, don't fail the run
            log.warning("spatial: skipping malformed reference geometry: %s", exc)
            continue
        if geom.is_empty:
            continue
        raw_geoms.append(geom)
        props = feature.get("properties") or {}
        names.append(str(props.get("name", "")))
        b = geom.bounds  # (minx, miny, maxx, maxy)
        min_lon, min_lat = min(min_lon, b[0]), min(min_lat, b[1])
        max_lon, max_lat = max(max_lon, b[2]), max(max_lat, b[3])

    if not raw_geoms:
        return None, [], [], None, 0

    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0
    transformer = _local_metric_transformer(center_lon, center_lat)

    proj_geoms = [shapely_transform(transformer, g) for g in raw_geoms]
    tree = STRtree(proj_geoms)
    return tree, proj_geoms, names, transformer, len(proj_geoms)


class _Mode(Enum):
    WITHIN = "within"
    BEYOND = "beyond"
    NEAREST = "nearest"


def _relate(
    subject_path: str,
    reference_path: str,
    distance: float,
    unit: str,
    mode: _Mode,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Core: stream subject features and relate them to the reference layer.

    ``within``/``beyond`` filter by ``distance``; ``nearest`` annotates with the
    nearest reference (``distance`` > 0 caps the search radius). Matched/annotated
    features gain ``nearest_distance_m`` / ``nearest_distance`` (and
    ``nearest_ref_name`` when the nearest reference carries a ``name``).
    """
    if not HAS_SHAPELY or not HAS_PYPROJ:
        raise RuntimeError(
            "shapely>=2.0 and pyproj>=3.0 are required for osm.Spatial operations"
        )

    unit_enum = Unit.from_string(unit)
    max_m = to_meters(distance, unit_enum) if distance and distance > 0 else None

    subject_path = str(subject_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial",
            uri_stem(subject_path),
            mode.value,
            uri_stem(str(reference_path)),
            f"{distance}{unit_enum.value}" if max_m is not None else None,
            ext="geojson",
            run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    tree, _geoms, names, transformer, reference_count = _load_reference(
        reference_path, heartbeat
    )

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_subject = localize(subject_path)
    original_count = 0

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_subject, heartbeat):
                original_count += 1
                keep, dist_m, ref_name = _evaluate(
                    feature, tree, names, transformer, max_m, mode
                )
                if not keep:
                    continue
                if dist_m is not None:
                    props = feature.setdefault("properties", {})
                    props["nearest_distance_m"] = round(dist_m, 3)
                    props["nearest_distance"] = round(from_meters(dist_m, unit_enum), 6)
                    if ref_name:
                        props["nearest_ref_name"] = ref_name
                writer.write_feature(feature)

        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=reference_count,
        operation=mode.value,
        distance=distance,
        unit=unit_enum.value,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def _evaluate(feature, tree, names, transformer, max_m, mode):
    """Return ``(keep, distance_m, ref_name)`` for one subject feature."""
    geom_json = feature.get("geometry")
    if not geom_json:
        # No geometry: kept only by BEYOND (it is trivially beyond any reference),
        # but with no distance to annotate.
        return (mode is _Mode.BEYOND, None, None)
    try:
        proj = shapely_transform(transformer, shape(geom_json)) if tree is not None else None
    except Exception as exc:
        log.warning("spatial: skipping malformed subject geometry: %s", exc)
        return (False, None, None)

    # Empty reference layer: nothing to be near.
    if tree is None or proj is None:
        if mode is _Mode.BEYOND:
            return (True, None, None)
        return (False, None, None)

    idxs, dists = tree.query_nearest(
        proj,
        max_distance=max_m,
        return_distance=True,
        all_matches=False,
    )
    has_match = len(idxs) > 0
    dist_m = float(dists[0]) if has_match else None
    ref_name = names[int(idxs[0])] if has_match else None

    if mode is _Mode.WITHIN:
        return (has_match, dist_m, ref_name)
    if mode is _Mode.BEYOND:
        # Within max_m of some reference -> NOT a "beyond" feature.
        return (not has_match, None, None)
    # NEAREST: keep everything; annotate when a match exists (capped by max_m).
    return (True, dist_m, ref_name)


def within_distance(
    subject_path: str,
    reference_path: str,
    distance: float,
    unit: str = "miles",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Keep subject features within ``distance`` of any reference feature."""
    return _relate(
        subject_path, reference_path, distance, unit, _Mode.WITHIN,
        output_path, heartbeat, run_id,
    )


def beyond_distance(
    subject_path: str,
    reference_path: str,
    distance: float,
    unit: str = "miles",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Keep subject features beyond ``distance`` from every reference feature."""
    return _relate(
        subject_path, reference_path, distance, unit, _Mode.BEYOND,
        output_path, heartbeat, run_id,
    )


def nearest(
    subject_path: str,
    reference_path: str,
    unit: str = "miles",
    distance: float = 0.0,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Annotate every subject feature with its nearest reference distance.

    ``distance`` > 0 caps the search radius; features with no reference within it
    are kept but left un-annotated.
    """
    return _relate(
        subject_path, reference_path, distance, unit, _Mode.NEAREST,
        output_path, heartbeat, run_id,
    )


# --- SpatialJoin: attach reference attributes by a topological predicate -------

_JOIN_PREDICATES = {"intersects", "within", "contains"}


def _load_reference_geoms(reference_path: str, heartbeat=None):
    """Load reference (geometry, properties) pairs in WGS84 + an STRtree.

    Topological predicates (intersects/within/contains) are projection-invariant,
    so SpatialJoin indexes the *unprojected* reference geometries directly.
    Returns ``(tree, geoms, props, count)``; ``tree`` is None for an empty layer.
    """
    from facetwork.runtime.storage import localize

    local_ref = localize(str(reference_path))
    geoms = []
    props: list[dict] = []
    for feature in iter_geojson_features(local_ref, heartbeat):
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception as exc:
            log.warning("spatial: skipping malformed reference geometry: %s", exc)
            continue
        if geom.is_empty:
            continue
        geoms.append(geom)
        props.append(feature.get("properties") or {})
    if not geoms:
        return None, [], [], 0
    return STRtree(geoms), geoms, props, len(geoms)


def spatial_join(
    subject_path: str,
    reference_path: str,
    predicate: str = "intersects",
    prefix: str = "ref_",
    how: str = "left",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Attach matching reference-layer properties onto each subject feature.

    For each subject feature, finds reference features satisfying ``predicate``
    (``intersects`` | ``within`` | ``contains``) and copies the *first* match's
    properties onto the subject under ``prefix`` (plus ``<prefix>joined_count``).
    The point-in-polygon join (PostGIS ``ST_Within`` join / GeoPandas ``sjoin``).
    ``how="inner"`` keeps only matched subjects; ``how="left"`` keeps all.
    """
    if not HAS_SHAPELY or not HAS_PYPROJ:
        raise RuntimeError("shapely>=2.0 and pyproj>=3.0 are required for osm.Spatial operations")
    if predicate not in _JOIN_PREDICATES:
        raise ValueError(f"predicate must be one of {sorted(_JOIN_PREDICATES)}; got {predicate!r}")
    if how not in ("left", "inner"):
        raise ValueError(f"how must be 'left' or 'inner'; got {how!r}")

    subject_path = str(subject_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(subject_path), "join",
            uri_stem(str(reference_path)), predicate, ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    tree, geoms, ref_props, reference_count = _load_reference_geoms(reference_path, heartbeat)

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_subject = localize(subject_path)
    original_count = 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_subject, heartbeat):
                original_count += 1
                geom_json = feature.get("geometry")
                matches = []
                if tree is not None and geom_json:
                    try:
                        g = shape(geom_json)
                        matches = sorted(int(i) for i in tree.query(g, predicate=predicate))
                    except Exception as exc:
                        log.warning("spatial: skipping malformed subject geometry: %s", exc)
                        matches = []
                if not matches and how == "inner":
                    continue
                props = feature.setdefault("properties", {})
                props[f"{prefix}joined_count"] = len(matches)
                if matches:
                    for k, v in ref_props[matches[0]].items():
                        props[f"{prefix}{k}"] = v
                writer.write_feature(feature)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=reference_count,
        operation="join",
        distance=0.0,
        unit="",
        extraction_date=datetime.now(UTC).isoformat(),
    )


# --- Buffer: expand each feature by a distance into polygons -------------------


def _layer_centroid(path: str, heartbeat=None) -> tuple[float, float] | None:
    """Center (lon, lat) of a layer's bounding box, or None if empty."""
    min_lon = min_lat = math.inf
    max_lon = max_lat = -math.inf
    seen = False
    for feature in iter_geojson_features(path, heartbeat):
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            b = shape(geom_json).bounds
        except Exception:
            continue
        seen = True
        min_lon, min_lat = min(min_lon, b[0]), min(min_lat, b[1])
        max_lon, max_lat = max(max_lon, b[2]), max(max_lat, b[3])
    if not seen:
        return None
    return (min_lon + max_lon) / 2.0, (min_lat + max_lat) / 2.0


def buffer(
    input_path: str,
    distance: float,
    unit: str = "miles",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Buffer every feature by ``distance`` (in ``unit``), producing polygons.

    The "area within ``distance`` of each feature" — PostGIS ``ST_Buffer`` / turf
    buffer. Geometries are buffered in a local azimuthal-equidistant projection
    (metric, centered on the layer) and reprojected to WGS84; properties are
    preserved. Feature count is unchanged. Feed the result to SpatialJoin /
    WithinDistance or RenderMap (e.g. service-area coverage).
    """
    if not HAS_SHAPELY or not HAS_PYPROJ:
        raise RuntimeError("shapely>=2.0 and pyproj>=3.0 are required for osm.Spatial operations")
    unit_enum = Unit.from_string(unit)
    dist_m = to_meters(distance, unit_enum)

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(input_path), "buffer",
            f"{distance}{unit_enum.value}", ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_input = localize(input_path)
    center = _layer_centroid(local_input, heartbeat)
    if center is None:
        # Empty input — write an empty FeatureCollection.
        with GeoJSONStreamWriter(output_path) as writer:
            pass
        return SpatialResult(
            output_path=output_path, feature_count=0, original_count=0, reference_count=0,
            operation="buffer", distance=distance, unit=unit_enum.value,
            extraction_date=datetime.now(UTC).isoformat(),
        )

    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center[1]} +lon_0={center[0]} +datum=WGS84 +units=m +no_defs"
    )
    wgs84 = CRS.from_epsg(4326)
    fwd = Transformer.from_crs(wgs84, aeqd, always_xy=True).transform
    inv = Transformer.from_crs(aeqd, wgs84, always_xy=True).transform

    original_count = 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_input, heartbeat):
                original_count += 1
                geom_json = feature.get("geometry")
                if not geom_json:
                    continue
                try:
                    projected = shapely_transform(fwd, shape(geom_json))
                    buffered = shapely_transform(inv, projected.buffer(dist_m))
                except Exception as exc:
                    log.warning("spatial: skipping malformed geometry in buffer: %s", exc)
                    continue
                if buffered.is_empty:
                    continue
                feature["geometry"] = mapping(buffered)
                writer.write_feature(feature)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=0,
        operation="buffer",
        distance=distance,
        unit=unit_enum.value,
        extraction_date=datetime.now(UTC).isoformat(),
    )


# --- Intersect / Union: geometric set operations ------------------------------
#
# Boolean overlay ops are projection-invariant for the geometry result, so these
# operate on WGS84 geometries directly (no AEQD needed, unlike Buffer/distance).


def _collect_geoms(path: str, heartbeat=None) -> list:
    """Load all non-empty shapely geometries from a GeoJSON layer (WGS84)."""
    from facetwork.runtime.storage import localize

    geoms = []
    for feature in iter_geojson_features(localize(str(path)), heartbeat):
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception as exc:
            log.warning("spatial: skipping malformed geometry: %s", exc)
            continue
        if not geom.is_empty:
            geoms.append(geom)
    return geoms


def intersect(
    subject_path: str,
    clip_path: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Clip each SUBJECT feature to the CLIP layer's geometry (ST_Intersection).

    The clip layer's features are unioned into one mask; each subject geometry is
    intersected with it, and the overlapping part is kept (with the subject's
    properties). Features that don't intersect are dropped — the geometric
    "cookie-cutter" (turf intersect / PostGIS ST_Intersection), distinct from
    SpatialJoin (which attaches attributes without cutting geometry).
    """
    if not HAS_SHAPELY:
        raise RuntimeError("shapely>=2.0 is required for osm.Spatial operations")

    subject_path = str(subject_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(subject_path), "intersect",
            uri_stem(str(clip_path)), ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    clip_geoms = _collect_geoms(clip_path, heartbeat)
    reference_count = len(clip_geoms)
    mask = unary_union(clip_geoms) if clip_geoms else None

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_subject = localize(subject_path)
    original_count = 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            if mask is not None and not mask.is_empty:
                for feature in iter_geojson_features(local_subject, heartbeat):
                    original_count += 1
                    geom_json = feature.get("geometry")
                    if not geom_json:
                        continue
                    try:
                        clipped = shape(geom_json).intersection(mask)
                    except Exception as exc:
                        log.warning("spatial: intersection failed, skipping: %s", exc)
                        continue
                    if clipped.is_empty:
                        continue
                    feature["geometry"] = mapping(clipped)
                    writer.write_feature(feature)
            else:
                # Empty clip mask -> nothing intersects; still count the subject.
                for _ in iter_geojson_features(local_subject, heartbeat):
                    original_count += 1
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=reference_count,
        operation="intersect",
        distance=0.0,
        unit="",
        extraction_date=datetime.now(UTC).isoformat(),
    )


def union(
    input_path: str,
    other_path: str = "",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Union all geometries into one merged feature (ST_Union aggregate).

    Merges every feature of ``input_path`` (and ``other_path`` if given) into a
    single (multi)geometry — the whole-layer / two-layer dissolve, distinct from
    ``Dissolve`` (which unions *per group*). The output is one feature carrying a
    ``merged_count`` of how many input geometries went in.
    """
    if not HAS_SHAPELY:
        raise RuntimeError("shapely>=2.0 is required for osm.Spatial operations")

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(input_path), "union",
            uri_stem(str(other_path)) if other_path else None,
            ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    geoms = _collect_geoms(input_path, heartbeat)
    original_count = len(geoms)
    reference_count = 0
    if other_path:
        other_geoms = _collect_geoms(other_path, heartbeat)
        reference_count = len(other_geoms)
        geoms = geoms + other_geoms

    from facetwork.config import get_temp_dir

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    feature_count = 0
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            if geoms:
                merged = unary_union(geoms)
                if not merged.is_empty:
                    writer.write_feature({
                        "type": "Feature",
                        "properties": {"operation": "union",
                                       "merged_count": original_count + reference_count},
                        "geometry": mapping(merged),
                    })
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=reference_count,
        operation="union",
        distance=0.0,
        unit="",
        extraction_date=datetime.now(UTC).isoformat(),
    )


# --- Centroid / Simplify: single-layer geometry reducers ----------------------


def centroid(
    input_path: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Replace each feature's geometry with its centroid Point (ST_Centroid).

    Reduces polygons/lines to a representative point (properties preserved) — e.g.
    label anchors, or to feed the point-based Spatial verbs. 1:1 feature count.
    Centroids are computed in lon/lat space (fine for the regional extents these
    compose at); points pass through unchanged.
    """
    if not HAS_SHAPELY:
        raise RuntimeError("shapely>=2.0 is required for osm.Spatial operations")

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(input_path), "centroid",
            ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_input = localize(input_path)
    original_count = 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_input, heartbeat):
                original_count += 1
                geom_json = feature.get("geometry")
                if not geom_json:
                    continue
                try:
                    c = shape(geom_json).centroid
                except Exception as exc:
                    log.warning("spatial: centroid failed, skipping: %s", exc)
                    continue
                if c.is_empty:
                    continue
                feature["geometry"] = mapping(c)
                writer.write_feature(feature)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=0,
        operation="centroid",
        distance=0.0,
        unit="",
        extraction_date=datetime.now(UTC).isoformat(),
    )


def simplify(
    input_path: str,
    tolerance: float,
    unit: str = "meters",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> SpatialResult:
    """Douglas-Peucker simplify each geometry at ``tolerance`` (ST_Simplify).

    Drops vertices within ``tolerance`` (in ``unit``, default meters) of the
    simplified line; topology-preserving. The tolerance is *metric*: geometries
    are projected to a local azimuthal-equidistant CRS, simplified, and reprojected
    to WGS84 — so the same tolerance behaves consistently regardless of latitude.
    Properties preserved; points pass through unchanged.
    """
    if not HAS_SHAPELY or not HAS_PYPROJ:
        raise RuntimeError("shapely>=2.0 and pyproj>=3.0 are required for osm.Spatial operations")
    unit_enum = Unit.from_string(unit)
    tol_m = to_meters(tolerance, unit_enum)

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-spatial", uri_stem(input_path), "simplify",
            f"{tolerance}{unit_enum.value}", ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    local_input = localize(input_path)
    center = _layer_centroid(local_input, heartbeat)
    if center is None:
        with GeoJSONStreamWriter(output_path):
            pass
        return SpatialResult(
            output_path=output_path, feature_count=0, original_count=0, reference_count=0,
            operation="simplify", distance=tolerance, unit=unit_enum.value,
            extraction_date=datetime.now(UTC).isoformat(),
        )

    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center[1]} +lon_0={center[0]} +datum=WGS84 +units=m +no_defs"
    )
    wgs84 = CRS.from_epsg(4326)
    fwd = Transformer.from_crs(wgs84, aeqd, always_xy=True).transform
    inv = Transformer.from_crs(aeqd, wgs84, always_xy=True).transform

    original_count = 0
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_input, heartbeat):
                original_count += 1
                geom_json = feature.get("geometry")
                if not geom_json:
                    continue
                try:
                    projected = shapely_transform(fwd, shape(geom_json))
                    reduced = shapely_transform(inv, projected.simplify(tol_m, preserve_topology=True))
                except Exception as exc:
                    log.warning("spatial: simplify failed, skipping: %s", exc)
                    continue
                if reduced.is_empty:
                    continue
                feature["geometry"] = mapping(reduced)
                writer.write_feature(feature)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return SpatialResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        reference_count=0,
        operation="simplify",
        distance=tolerance,
        unit=unit_enum.value,
        extraction_date=datetime.now(UTC).isoformat(),
    )
