"""Event-facet handlers for the ``osm.Transform`` namespace.

Wires MergeLayers / Summarize / Dissolve (osmtransform.ffl) to the compute
core in :mod:`transform_ops`. Follows the filters/spatial handler pattern:
per-facet factories, output cache, RegistryRunner dispatch + poller helper.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .transform_ops import dissolve, merge_layers, summarize

log = logging.getLogger(__name__)

NAMESPACE = "osm.Transform"


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _empty(operation: str, fmt: str = "GeoJSON") -> dict:
    return {
        "result": {
            "output_path": "",
            "feature_count": 0,
            "original_count": 0,
            "group_count": 0,
            "operation": operation,
            "detail": "",
            "format": fmt,
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _make_merge_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        inputs = payload.get("inputs") or []
        if isinstance(inputs, str):
            inputs = [p.strip() for p in inputs.split(",") if p.strip()]
        step_log = payload.get("_step_log")

        # Cache key: the ordered input list + their sizes (any change re-merges).
        cp = {"inputs": list(inputs), "sizes": [_file_size(p) for p in inputs]}
        input_cache = {"path": inputs[0] if inputs else "", "size": _file_size(inputs[0]) if inputs else 0}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: merging {len(inputs)} layers")
        if not inputs:
            return _empty("merge")

        result = merge_layers(inputs, heartbeat=payload.get("_task_heartbeat"),
                              run_id=payload.get("_workflow_id", ""))
        if step_log:
            step_log(f"{facet_name}: merged {result.group_count} layers -> "
                     f"{result.feature_count} features", level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_summarize_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        group_by = payload.get("group_by", "")
        measure = payload.get("measure", "")
        op = payload.get("op", "count")
        step_log = payload.get("_step_log")

        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"group_by": group_by, "measure": measure, "op": op}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: summarize {input_path} ({op}"
                     f"{' by ' + group_by if group_by else ''})")
        if not input_path:
            return _empty("summarize", fmt="JSON")

        result = summarize(input_path, group_by=group_by, measure=measure, op=op,
                           heartbeat=payload.get("_task_heartbeat"),
                           run_id=payload.get("_workflow_id", ""))
        if step_log:
            step_log(f"{facet_name}: {result.feature_count} features, "
                     f"{result.group_count} groups — {result.detail}", level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_dissolve_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        group_by = payload.get("group_by", "")
        step_log = payload.get("_step_log")

        input_cache = {"path": input_path, "size": _file_size(input_path)}
        cp = {"group_by": group_by}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: dissolve {input_path} by {group_by}")
        if not input_path or not group_by:
            return _empty("dissolve")

        result = dissolve(input_path, group_by=group_by,
                          heartbeat=payload.get("_task_heartbeat"),
                          run_id=payload.get("_workflow_id", ""))
        if step_log:
            step_log(f"{facet_name}: {result.original_count} features -> "
                     f"{result.feature_count} dissolved groups", level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


TRANSFORM_FACETS = [
    ("MergeLayers", _make_merge_handler),
    ("Summarize", _make_summarize_handler),
    ("Dissolve", _make_dissolve_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in TRANSFORM_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.Transform facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_transform_handlers(poller) -> None:
    """Register all osm.Transform event facet handlers with an AgentPoller."""
    for facet_name, factory in TRANSFORM_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered transform handler: %s", qualified_name)
