"""Handler for ``osm.Change.ExtractChanges`` — changed features from replication diffs.

Reads the changed objects since `since` (the network seam), resolves changed-way
node geometry from the base extract, classifies node AND way changes into
added/modified/deleted GeoJSON, writes the three FeatureCollections to the output
store, and returns a ChangeSet (paths + counts).

NOT YET REGISTERED — Gate-B scaffold. Wire ``register_change_handlers`` /
``register_handlers`` into the handler registration once the change-reader seam
(``tools._osm_tools.osm_changes._collect_changes``) is reviewed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ...tools._osm_tools.osm_changes import (
    _collect_changes,
    _resolve_way_node_locations,
    classify_changes,
    needed_way_node_ids,
)
from ...tools._osm_tools.pbf_download import cached_path, get_storage, is_region_cached
from ..shared._output import open_output, resolve_output_dir

NAMESPACE = "osm.Change"


def handle_extract_changes(params: dict[str, Any]) -> dict[str, Any]:
    """Extract changed node features for a region since `since` (replication diffs)."""
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
    local_pbf = None
    if since == "":
        if not region_cached:
            raise ValueError("ExtractChanges: since=\"\" needs the region cached for its "
                             "replication baseline; pass an explicit date or sequence.")
        local_pbf = storage.localize(cached_path(geofabrik_path))

    start_seq, changes = _collect_changes(geofabrik_path, since, max_diff_mb, local_pbf)

    # Build node id -> (lon, lat) for changed-way geometry: (a) the diff's own
    # changed nodes carry new coords; (b) the unchanged nodes referenced by
    # changed ways are resolved from the localized BASE extract via a single
    # filtered scan (only the still-needed ids). If the base PBF can't be
    # localized (no cache), those ways fall back to null geometry.
    node_locs: dict[int, tuple[float, float]] = {
        c.osm_id: (c.lon, c.lat)
        for c in changes
        if c.osm_type == "node" and c.lat is not None and c.lon is not None
    }
    needed = needed_way_node_ids(changes) - set(node_locs)
    if needed:
        if local_pbf is None and region_cached:
            local_pbf = storage.localize(cached_path(geofabrik_path))
        if local_pbf:
            node_locs.update(_resolve_way_node_locations(local_pbf, needed))

    classified = classify_changes(changes, node_locs)
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
            f"({counts['ways_changed']} ways with geometry / {counts['relations_changed']} rels "
            f"changed, rels not emitted)",
            level="success",
        )

    return {"changes": {
        "region": region,
        "added": paths["added"], "modified": paths["modified"], "deleted": paths["deleted"],
        "added_count": counts["added"], "modified_count": counts["modified"],
        "deleted_count": counts["deleted"],
        "ways_added": counts["ways_added"], "ways_modified": counts["ways_modified"],
        "ways_deleted": counts["ways_deleted"], "ways_changed": counts["ways_changed"],
        "relations_changed": counts["relations_changed"], "since_sequence": start_seq,
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
