"""Handler for ``osm.Change.ExtractChanges`` — changed features from replication diffs.

Reads the changed objects since `since` (the network seam), assembles changed
WAY and RELATION geometry from the diff-applied base extract via osmium (the
CLI/IO seam), classifies node + way + relation changes into added/modified/deleted
GeoJSON, writes the three FeatureCollections to the output store, and returns a
ChangeSet (paths + counts).

Geometry is escalated lazily: a diff that touched only nodes (the POI case) skips
the osmium pass entirely and uses inline Point geometry — the heavy assembly runs
only when ways/relations changed, and only for those ids.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from ...tools._osm_tools.osm_changes import (
    _assemble_geometry,
    _collect_changes,
    changed_geometry_tokens,
    classify_changes,
)
from ...tools._osm_tools.pbf_download import cached_path, get_storage, is_region_cached
from ..shared._output import open_output, resolve_output_dir

NAMESPACE = "osm.Change"


def handle_extract_changes(params: dict[str, Any]) -> dict[str, Any]:
    """Extract changed node/way/relation features for a region since `since`."""
    region = params.get("region") or {}
    if not isinstance(region, dict):
        raise ValueError(f"ExtractChanges: 'region' must be a Region dict, got {type(region).__name__}")
    geofabrik_path = region.get("geofabrik_path") or region.get("canonical") or ""
    if not geofabrik_path:
        raise ValueError("ExtractChanges: Region is missing geofabrik_path (and canonical). "
                         "Resolve it via osm.Region.ResolveRegions first.")
    since = str(params.get("since") or "")
    try:
        max_diff_mb = int(params.get("max_diff_mb") or 512)
    except (TypeError, ValueError):
        max_diff_mb = 512
    step_log = params.get("_step_log")

    storage = get_storage()
    region_cached = is_region_cached(geofabrik_path, storage=storage)
    # The cached base extract serves two roles: the replication baseline for
    # since="" AND the source osmium resolves changed-way/relation member geometry
    # from. Localize it once if present.
    local_pbf = storage.localize(cached_path(geofabrik_path)) if region_cached else None
    if since == "" and not local_pbf:
        raise ValueError("ExtractChanges: since=\"\" needs the region cached for its "
                         "replication baseline; pass an explicit date or sequence.")

    start_seq, changes, osc_path = _collect_changes(geofabrik_path, since, max_diff_mb, local_pbf)
    try:
        # Assemble way/relation geometry only when such objects changed (the
        # bulkhead: an all-node diff never pays for the osmium pass). Needs the
        # cached base extract for member resolution; without it, those features
        # degrade to null geometry.
        geom_map: dict[tuple[str, int], dict[str, Any]] = {}
        id_tokens = changed_geometry_tokens(changes)
        if id_tokens:
            if local_pbf:
                geom_map = _assemble_geometry(local_pbf, osc_path, id_tokens)
            elif step_log:
                step_log(
                    f"ExtractChanges: {len(id_tokens)} changed way(s)/relation(s) have no "
                    f"cached base extract to resolve geometry — emitting null geometry "
                    f"(cache the region for full way/relation geometry)",
                    level="warning",
                )
        classified = classify_changes(changes, geom_map)
    finally:
        if osc_path:
            shutil.rmtree(os.path.dirname(osc_path), ignore_errors=True)

    counts = classified["counts"]
    outdir = resolve_output_dir("changes")
    slug = geofabrik_path.strip("/").replace("/", "_")
    paths: dict[str, str] = {}
    for kind in ("added", "modified", "deleted"):
        p = os.path.join(outdir, f"{slug}-changes-{kind}.geojson")
        with open_output(p) as f:
            json.dump(classified[kind], f)
        paths[kind] = p

    if step_log:
        step_log(
            f"ExtractChanges: '{region.get('name') or geofabrik_path}' since seq {start_seq} -> "
            f"+{counts['added']} ~{counts['modified']} -{counts['deleted']} features "
            f"({counts['ways_changed']} ways / {counts['relations_changed']} relations changed, "
            f"with Point/LineString/Polygon/MultiPolygon geometry)",
            level="success",
        )

    return {"changes": {
        "region": region,
        "added": paths["added"], "modified": paths["modified"], "deleted": paths["deleted"],
        "added_count": counts["added"], "modified_count": counts["modified"],
        "deleted_count": counts["deleted"],
        "ways_added": counts["ways_added"], "ways_modified": counts["ways_modified"],
        "ways_deleted": counts["ways_deleted"], "ways_changed": counts["ways_changed"],
        "relations_added": counts["relations_added"], "relations_modified": counts["relations_modified"],
        "relations_deleted": counts["relations_deleted"], "relations_changed": counts["relations_changed"],
        "since_sequence": start_seq,
    }}


_DISPATCH = {f"{NAMESPACE}.ExtractChanges": handle_extract_changes}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner. Blocking network I/O -> timeout_ms=0."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_change_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
