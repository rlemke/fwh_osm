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
    from shapely.geometry import shape
    from shapely.ops import transform as shapely_transform
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
