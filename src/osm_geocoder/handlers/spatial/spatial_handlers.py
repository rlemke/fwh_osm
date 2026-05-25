"""Event-facet handlers for the ``osm.Spatial`` namespace.

Wires the WithinDistance / BeyondDistance / Nearest facets (osmspatial.ffl) to
the compute core in :mod:`spatial_ops`. Mirrors the filters handler module:
per-facet handler factories, an output cache keyed on *both* input layers, a
RegistryRunner dispatch entrypoint (:func:`handle`) and an AgentPoller
registration helper.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .spatial_ops import (
    HAS_PYPROJ,
    HAS_SHAPELY,
    beyond_distance,
    nearest,
    within_distance,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Spatial"


def _file_size(path: str) -> int:
    """Return file size in bytes, or 0 if unavailable."""
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _empty_result(operation: str, distance: float, unit: str) -> dict:
    """A zero-feature result payload (missing inputs / geometry stack absent)."""
    return {
        "result": {
            "output_path": "",
            "feature_count": 0,
            "original_count": 0,
            "reference_count": 0,
            "operation": operation,
            "distance": distance,
            "unit": unit,
            "format": "GeoJSON",
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _make_distance_handler(facet_name: str, op):
    """Build a handler for WithinDistance / BeyondDistance.

    Both share a contract: ``subject_path`` + ``reference_path`` + ``distance``
    + ``unit``. ``op`` is the compute function (``within_distance`` /
    ``beyond_distance``).
    """
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        subject_path = payload.get("subject_path", "")
        reference_path = payload.get("reference_path", "")
        distance = payload.get("distance", 0.0)
        unit = payload.get("unit", "miles")
        step_log = payload.get("_step_log")

        # Cache key folds in BOTH layers: the subject as the primary ``cache``
        # input (path+size), the reference + distance/unit as params, so a change
        # to either layer invalidates the cached result.
        input_cache = {"path": subject_path, "size": _file_size(subject_path)}
        cp = {
            "reference_path": reference_path,
            "reference_size": _file_size(reference_path),
            "distance": distance,
            "unit": unit,
        }
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: relating {subject_path} to {reference_path} "
                f"({facet_name} {distance} {unit})"
            )
        log.info(
            "%s relating %s to %s (%s %s)",
            facet_name,
            subject_path,
            reference_path,
            distance,
            unit,
        )

        if not HAS_SHAPELY or not HAS_PYPROJ or not subject_path or not reference_path:
            return _empty_result(facet_name.lower(), distance, unit)

        result = op(
            subject_path,
            reference_path,
            distance,
            unit=unit,
            heartbeat=payload.get("_task_heartbeat"),
            run_id=payload.get("_workflow_id", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: {result.feature_count}/{result.original_count} subject "
                f"features kept vs {result.reference_count} reference features",
                level="success",
            )
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_nearest_handler(facet_name: str):
    """Build a handler for the Nearest facet (annotate, keep all)."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        subject_path = payload.get("subject_path", "")
        reference_path = payload.get("reference_path", "")
        unit = payload.get("unit", "miles")
        distance = payload.get("distance", 0.0)
        step_log = payload.get("_step_log")

        input_cache = {"path": subject_path, "size": _file_size(subject_path)}
        cp = {
            "reference_path": reference_path,
            "reference_size": _file_size(reference_path),
            "unit": unit,
            "distance": distance,
        }
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: annotating {subject_path} with nearest in {reference_path}")
        log.info("%s annotating %s with nearest in %s", facet_name, subject_path, reference_path)

        if not HAS_SHAPELY or not HAS_PYPROJ or not subject_path or not reference_path:
            return _empty_result("nearest", distance, unit)

        result = nearest(
            subject_path,
            reference_path,
            unit=unit,
            distance=distance,
            heartbeat=payload.get("_task_heartbeat"),
            run_id=payload.get("_workflow_id", ""),
        )

        if step_log:
            step_log(
                f"{facet_name}: annotated {result.feature_count} features "
                f"against {result.reference_count} reference features",
                level="success",
            )
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


# Event facet definitions for handler registration.
SPATIAL_FACETS = [
    ("WithinDistance", lambda n: _make_distance_handler(n, within_distance)),
    ("BeyondDistance", lambda n: _make_distance_handler(n, beyond_distance)),
    ("Nearest", _make_nearest_handler),
]


# RegistryRunner dispatch adapter.
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in SPATIAL_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = handler_factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.Spatial facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_spatial_handlers(poller) -> None:
    """Register all osm.Spatial event facet handlers with an AgentPoller."""
    for facet_name, handler_factory in SPATIAL_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered spatial handler: %s", qualified_name)
