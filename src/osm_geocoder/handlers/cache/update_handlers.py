"""Handler for ``osm.cache.UpdateRegion`` — incremental diff-based cache update.

Backs ``UpdateRegion(region: Region, max_diff_mb: Long) => (cache: OSMCache,
method: String, applied_mb: Double)``. Reads the Region's ``geofabrik_path``
(same input contract as ``osm.cache.Download``), applies Geofabrik replication
diffs to the cached extract, and falls back to a full download when there's no
replication baseline / the extract is too far behind the diff budget.

NOT YET REGISTERED — pending Gate-B review of the replication seam in
``tools._osm_tools.pbf_update`` (the pyosmium tool/dependency). Wire
``register_update_handlers`` / ``register_handlers`` into the cache handler
registration once approved.
"""

from __future__ import annotations

import os
from typing import Any

# Direct tool import for the scaffold; finalize via the handlers/shared/pbf_cache
# shim (like region_handlers) when wiring for deploy.
from ...tools._osm_tools.pbf_update import update_region

NAMESPACE = "osm.cache"


def handle_update_region(params: dict[str, Any]) -> dict[str, Any]:
    """Update a region's cached PBF via replication diffs (full-download fallback)."""
    region = params.get("region") or {}
    if not isinstance(region, dict):
        raise ValueError(
            f"UpdateRegion: 'region' must be a Region dict, got {type(region).__name__}"
        )
    geofabrik_path = region.get("geofabrik_path") or region.get("canonical") or ""
    if not geofabrik_path:
        raise ValueError(
            "UpdateRegion: Region is missing geofabrik_path (and canonical). "
            "Resolve it via osm.Region.ResolveRegions first."
        )

    try:
        max_diff_mb = int(params.get("max_diff_mb") or 2048)
    except (TypeError, ValueError):
        max_diff_mb = 2048

    step_log = params.get("_step_log")
    result = update_region(geofabrik_path, max_diff_mb=max_diff_mb, region=region)
    applied_mb = round(result.applied_bytes / 1_000_000, 2)

    if step_log:
        display = region.get("name") or geofabrik_path
        step_log(
            f"UpdateRegion: '{display}' -> method={result.method}, "
            f"applied={applied_mb} MB",
            level="success",
        )
    return {"cache": result.cache, "method": result.method, "applied_mb": applied_mb}


_DISPATCH = {
    f"{NAMESPACE}.UpdateRegion": handle_update_region,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner. Blocking network I/O (diff fetch / full
    download fallback) — register with timeout_ms=0 and rely on the runner's
    global execution timeout, like the PBF source/download handlers."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_update_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
