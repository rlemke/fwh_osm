"""Generic GeoJSON transforms — the Transform/Analyze layer of the library.

Category-agnostic reductions over GeoJSON layers, complementing the
category-specific statistics facets (AmenityStatistics, ParkStatistics, …):

* :func:`merge_layers` — concatenate many GeoJSON layers into one (the
  foreach-fanout aggregator: collect per-region/category layers → merge →
  render/analyze).
* :func:`summarize`    — count features, optionally grouped by a tag, with a
  count/sum/avg/min/max aggregate over a numeric measure (the generic
  Count/Summarize). Writes the breakdown as JSON.
* :func:`dissolve`     — union each group's geometries into one feature
  (PostGIS ``ST_Union ... GROUP BY`` / turf dissolve).

All stream their inputs (dissolve buffers geometry per group) and write
collision-safe, cache-backed artifacts via the shared helpers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..shared._output import derive_output_path, ensure_dir, uri_stem
from ..shared.geojson_writer import GeoJSONStreamWriter, iter_geojson_features

log = logging.getLogger(__name__)

try:
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    HAS_SHAPELY = True
except ImportError:  # pragma: no cover
    HAS_SHAPELY = False

_AGG_OPS = {"count", "sum", "avg", "min", "max"}


@dataclass
class TransformResult:
    """Result of a generic transform (mirrors the FFL TransformResult)."""

    output_path: str
    feature_count: int
    original_count: int
    group_count: int
    operation: str
    detail: str = ""
    format: str = "GeoJSON"
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "feature_count": self.feature_count,
            "original_count": self.original_count,
            "group_count": self.group_count,
            "operation": self.operation,
            "detail": self.detail,
            "format": self.format,
            "extraction_date": self.extraction_date,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def merge_layers(
    inputs: list[str],
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> TransformResult:
    """Concatenate the features of several GeoJSON layers into one collection.

    Properties are preserved verbatim. Missing/empty inputs are skipped (logged),
    not fatal — a foreach fan-out may legitimately yield some empty layers.
    """
    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    inputs = [p for p in (inputs or []) if p]
    if output_path is None:
        stem = uri_stem(inputs[0]) if inputs else "merged"
        output_path = derive_output_path(
            "osm-transform", stem, "merged", str(len(inputs)),
            ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    merged_layers = 0
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for path in inputs:
                try:
                    local = localize(str(path))
                except Exception as exc:
                    log.warning("merge_layers: cannot localize %s: %s", path, exc)
                    continue
                if not os.path.exists(local):
                    log.warning("merge_layers: input missing, skipping: %s", path)
                    continue
                merged_layers += 1
                for feature in iter_geojson_features(local, heartbeat):
                    writer.write_feature(feature)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return TransformResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=feature_count,
        group_count=merged_layers,
        operation="merge",
        detail=f"{merged_layers} layers",
        extraction_date=_now(),
    )


def summarize(
    input_path: str,
    group_by: str = "",
    measure: str = "",
    op: str = "count",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> TransformResult:
    """Reduce a GeoJSON layer to an aggregate, optionally grouped by a tag.

    ``op`` ∈ {count, sum, avg, min, max}. ``count`` ignores ``measure``; the
    others aggregate ``properties[measure]`` (numeric, non-numeric skipped). With
    no ``group_by`` the result is a single bucket (a plain count/aggregate). The
    full breakdown is written as JSON to ``output_path``.
    """
    if op not in _AGG_OPS:
        raise ValueError(f"op must be one of {sorted(_AGG_OPS)}; got {op!r}")
    if op != "count" and not measure:
        raise ValueError(f"op={op!r} requires a measure property")

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-transform", uri_stem(input_path), "summary",
            op, group_by or None, measure or None, ext="json", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    # group key -> {"count": n, "sum": s, "min": m, "max": M}
    groups: dict[str, dict[str, float]] = {}
    total = 0
    _ALL = "__all__"

    for feature in iter_geojson_features(localize(input_path), heartbeat):
        total += 1
        props = feature.get("properties") or {}
        key = str(props.get(group_by, "")) if group_by else _ALL
        g = groups.setdefault(key, {"count": 0, "sum": 0.0, "min": None, "max": None})
        g["count"] += 1
        if op != "count":
            try:
                val = float(props.get(measure))
            except (TypeError, ValueError):
                continue
            g["sum"] += val
            g["min"] = val if g["min"] is None else min(g["min"], val)
            g["max"] = val if g["max"] is None else max(g["max"], val)

    def _value(g: dict[str, float]):
        if op == "count":
            return g["count"]
        if op == "sum":
            return g["sum"]
        if op == "avg":
            return g["sum"] / g["count"] if g["count"] else 0.0
        if op == "min":
            return g["min"]
        if op == "max":
            return g["max"]
        return None

    breakdown = {k: {"count": g["count"], "value": _value(g)} for k, g in groups.items()}
    group_count = 0 if not group_by else len(groups)

    payload = {
        "operation": f"summarize:{op}",
        "input": input_path,
        "group_by": group_by,
        "measure": measure,
        "total": total,
        "group_count": group_count,
        "groups": breakdown,
        "extraction_date": _now(),
    }
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", dir=get_temp_dir())
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(payload, f, indent=2)
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    # A short human headline: the top group by value (or the plain total).
    if group_by and breakdown:
        top = max(breakdown.items(), key=lambda kv: kv[1]["value"] or 0)
        detail = f"top {group_by}={top[0]!r} ({op}={top[1]['value']})"
    else:
        detail = f"{op}={_value(groups.get(_ALL, {'count': 0, 'sum': 0.0, 'min': None, 'max': None}))}"

    return TransformResult(
        output_path=output_path,
        feature_count=total,
        original_count=total,
        group_count=group_count,
        operation=f"summarize:{op}",
        detail=detail,
        format="JSON",
        extraction_date=_now(),
    )


def dissolve(
    input_path: str,
    group_by: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> TransformResult:
    """Union each group's geometries into a single feature (one per group).

    Groups features by ``properties[group_by]`` and replaces each group with one
    feature whose geometry is the union of the group's geometries (PostGIS
    ``ST_Union ... GROUP BY`` / turf dissolve). The output feature carries the
    group key and a ``dissolved_count``. Geometry is buffered in memory per group.
    """
    if not HAS_SHAPELY:
        raise RuntimeError("shapely>=2.0 is required for dissolve")
    if not group_by:
        raise ValueError("dissolve requires a group_by tag")

    from facetwork.config import get_temp_dir
    from facetwork.runtime.storage import localize

    input_path = str(input_path)
    if output_path is None:
        output_path = derive_output_path(
            "osm-transform", uri_stem(input_path), "dissolved",
            group_by, ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    grouped: dict[str, list] = {}
    original_count = 0
    for feature in iter_geojson_features(localize(input_path), heartbeat):
        geom_json = feature.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception as exc:
            log.warning("dissolve: skipping malformed geometry: %s", exc)
            continue
        if geom.is_empty:
            continue
        original_count += 1
        key = str((feature.get("properties") or {}).get(group_by, ""))
        grouped.setdefault(key, []).append(geom)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)
    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for key, geoms in grouped.items():
                merged = unary_union(geoms) if len(geoms) > 1 else geoms[0]
                if merged.is_empty:
                    continue
                writer.write_feature({
                    "type": "Feature",
                    "properties": {group_by: key, "dissolved_count": len(geoms)},
                    "geometry": mapping(merged),
                })
        ensure_dir(output_path)
        shutil.move(tmp_path, output_path)
        feature_count = writer.feature_count
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return TransformResult(
        output_path=output_path,
        feature_count=feature_count,
        original_count=original_count,
        group_count=len(grouped),
        operation="dissolve",
        detail=f"by {group_by}",
        extraction_date=_now(),
    )
