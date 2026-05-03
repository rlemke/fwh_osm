"""Output caching for OSM handler results.

Prevents redundant re-extraction when the input data and handler code
haven't changed.  Each output gets a metadata file (keyed by SHA-256
of input PBF size, handler name, handler parameters, and app version).
On subsequent runs the key is compared; if it matches and the output
file still exists, the cached result is returned without re-running
the extractor.

Quick integration — wrap any handler with ``with_output_cache``::

    from osm_geocoder.handlers.shared.output_cache import with_output_cache

    def _make_handler(facet_name, admin_levels):
        cache_params = {"admin_levels": sorted(admin_levels)}

        def handler(payload):
            result = extract_boundaries(...)
            return {"result": {...}}

        return with_output_cache(handler, facet_name, cache_params)

The wrapper checks the cache before calling the handler and saves the
result metadata after a successful run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from facetwork.runtime.storage import get_storage_backend

log = logging.getLogger(__name__)

# Application-level version.  Bump when handler logic changes in a way
# that invalidates all cached outputs (e.g. GeoJSON schema change).
# Minor handler fixes that only affect a single handler should bump a
# per-handler version via ``cache_params`` instead.
_APP_VERSION = "5"


def _version_key(
    handler_name: str,
    pbf_path: str,
    pbf_size: int,
    cache_params: dict[str, Any],
) -> str:
    """Build a deterministic hash key from inputs.

    The key captures everything that could change the output:
    - PBF file identity (path + size as a proxy for content)
    - Handler identity (qualified facet name)
    - Handler parameters (admin levels, park type, etc.)
    - Application version (global invalidation knob)
    """
    raw = json.dumps(
        {
            "v": _APP_VERSION,
            "handler": handler_name,
            "pbf_path": pbf_path,
            "pbf_size": pbf_size,
            "params": cache_params,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _meta_path(output_path: str) -> str:
    """Sidecar metadata path for an output file."""
    return output_path + ".meta.json"


def cached_result(
    handler_name: str,
    cache: dict[str, Any],
    cache_params: dict[str, Any],
    step_log: Any | None = None,
) -> dict[str, Any] | None:
    """Check if a cached output is still valid.

    Args:
        handler_name: Qualified facet name (e.g. ``osm.Boundaries.StateBoundaries``).
        cache: The ``cache`` dict from the payload.  Must contain ``path``
            and ``size``; alternatively pass ``{"path": some_file_path}``
            and the size will be read from the filesystem.
        cache_params: Handler-specific parameters that affect the output.
        step_log: Optional step log callback.

    Returns:
        The full handler result dict if cache is valid, else ``None``.
    """
    pbf_path = cache.get("path", "")
    pbf_size = cache.get("size", 0)
    if not pbf_path:
        return None

    key = _version_key(handler_name, pbf_path, pbf_size, cache_params)

    # Read existing metadata (if any)
    # We don't know the output_path yet — scan cache_params isn't enough.
    # Instead, we store the meta path in a predictable location based on
    # the version key itself, under the output base directory.
    from facetwork.config import get_output_base

    meta_dir = os.environ.get(
        "AFL_OSM_OUTPUT_BASE",
        os.path.join(get_output_base(), "osm"),
    )
    meta_file = f"{meta_dir.rstrip('/')}/.osm-meta/{key[:2]}/{key}.json"

    backend = get_storage_backend(meta_file)
    if not backend.exists(meta_file):
        return None

    try:
        with backend.open(meta_file, "r") as f:
            meta = json.load(f)
    except Exception:
        return None

    if meta.get("key") != key:
        return None

    # Verify all output files still exist
    output_paths = meta.get("output_paths") or []
    output_path = meta.get("output_path", "")
    if output_path and output_path not in output_paths:
        output_paths.append(output_path)
    for op in output_paths:
        if not op:
            continue
        out_backend = get_storage_backend(op)
        if not out_backend.exists(op):
            log.info("output-cache: output gone (%s), invalidating %s", op, handler_name)
            return None

    result = meta.get("result")
    if result is None:
        return None

    if step_log:
        step_log(
            f"{handler_name}: output cache hit (key={key[:12]}…)",
            level="success",
        )
    log.info("output-cache: hit for %s (key=%s)", handler_name, key[:12])
    return result


def save_result_meta(
    handler_name: str,
    cache: dict[str, Any],
    cache_params: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Save result metadata for future cache lookups.

    Args:
        handler_name: Qualified facet name.
        cache: The ``cache`` dict from the payload.
        cache_params: Handler-specific parameters.
        result: The full handler result dict (e.g. ``{"result": {...}}``).
    """
    pbf_path = cache.get("path", "")
    pbf_size = cache.get("size", 0)
    if not pbf_path:
        return

    key = _version_key(handler_name, pbf_path, pbf_size, cache_params)

    # Extract output_path(s) from result for existence checks on future lookups.
    # Handles both single-output handlers (output_path in result/stats) and
    # multi-output handlers like CombinedScan (output_paths in a JSON results string).
    inner = result.get("result", result.get("stats", {}))
    output_path = inner.get("output_path", "")
    output_paths: list[str] = []
    if output_path:
        output_paths.append(output_path)
    # CombinedScan stores per-category results as a JSON string with output_path per category
    results_json = result.get("results", "")
    if isinstance(results_json, str) and results_json.startswith("{"):
        try:
            per_cat = json.loads(results_json)
            for cat_data in per_cat.values():
                if isinstance(cat_data, dict) and cat_data.get("output_path"):
                    output_paths.append(cat_data["output_path"])
        except (json.JSONDecodeError, AttributeError):
            pass

    from facetwork.config import get_output_base

    meta_dir = os.environ.get(
        "AFL_OSM_OUTPUT_BASE",
        os.path.join(get_output_base(), "osm"),
    )
    meta_file = f"{meta_dir.rstrip('/')}/.osm-meta/{key[:2]}/{key}.json"

    meta = {
        "key": key,
        "handler": handler_name,
        "pbf_path": pbf_path,
        "pbf_size": pbf_size,
        "params": cache_params,
        "output_path": output_path,
        "output_paths": output_paths,
        "app_version": _APP_VERSION,
        "result": result,
    }

    backend = get_storage_backend(meta_file)
    parent = meta_file.rsplit("/", 1)[0]
    backend.makedirs(parent, exist_ok=True)

    try:
        with backend.open(meta_file, "w") as f:
            json.dump(meta, f, separators=(",", ":"))
        log.debug("output-cache: saved meta for %s (key=%s)", handler_name, key[:12])
    except Exception:
        log.debug("output-cache: failed to save meta for %s", handler_name, exc_info=True)


def with_output_cache(
    handler: Any,
    handler_name: str,
    cache_params: dict[str, Any],
    *,
    cache_key: str = "cache",
) -> Any:
    """Wrap a handler function with output caching.

    Returns a new handler that checks the cache before calling the
    original handler and saves the result metadata after a successful run.

    Args:
        handler: The original handler function ``(payload) -> dict``.
        handler_name: Qualified facet name for cache key computation.
        cache_params: Handler-specific parameters that affect the output.
        cache_key: Key in payload that holds the cache/input dict
            (default ``"cache"``).

    Returns:
        Wrapped handler function with the same signature.
    """

    def wrapper(payload: dict) -> dict:
        cache = payload.get(cache_key, {})
        step_log = payload.get("_step_log")

        hit = cached_result(handler_name, cache, cache_params, step_log)
        if hit is not None:
            return hit

        result = handler(payload)

        # Save metadata for successful results that have an output_path
        if result:
            save_result_meta(handler_name, cache, cache_params, result)

        return result

    return wrapper
