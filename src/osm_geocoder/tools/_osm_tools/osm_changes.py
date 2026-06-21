"""Extract changed OSM features from Geofabrik replication diffs.

Tool side of ``osm.Change.ExtractChanges``. Reuses the replication API used by
``pbf_update`` (``ReplicationServer.collect_diffs``) but, instead of applying the
diffs to the cached PBF, surfaces them: which features were added / modified /
deleted since a sequence/date.

Split so the pure classification (diff objects -> GeoJSON FeatureCollections) is
unit-tested offline, and only the network/IO reads are the seams:

  - ``_collect_changes``  THE NETWORK SEAM (mocked in tests): resolves the start
    sequence and reads the merged change buffer into ``ChangeObj`` records.
  - ``_resolve_way_node_locations``  THE BASE-SCAN IO SEAM (mocked in tests):
    given the localized base PBF and the set of node ids referenced by changed
    ways, scans the base PBF ONCE collecting locations for ONLY those ids.
  - ``classify_changes``  pure: ChangeObj records + a node-location map ->
    {added, modified, deleted} FeatureCollections + counts.

Geometry:
  - NODE changes -> Point (the POI case).
  - WAY changes -> LineString (open way) or Polygon (closed/area way). A diff's
    way object carries only its node REF list, not coordinates; coordinates are
    resolved from (a) any changed nodes in the same diff and (b) a filtered
    one-pass scan of the localized BASE cached PBF for the referenced ids. A way
    whose nodes can't all be resolved is emitted with null geometry (still
    identifies osm_id + change_type), never a crash.
  - RELATION changes are counted only — their geometry (member resolution across
    nodes/ways/nested relations) is a separate, bigger problem and a follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pbf_download import GEOFABRIK_BASE


@dataclass
class ChangeObj:
    """One changed OSM object from a replication diff.

    Nodes carry ``lat``/``lon`` (their new coords). Ways carry ``nodes`` — the
    ordered list of referenced node ids — but no coordinates (a replication diff
    does not ship a way's node coordinates); those are resolved separately.
    """
    osm_type: str          # "node" | "way" | "relation"
    osm_id: int
    version: int
    visible: bool
    lat: float | None = None
    lon: float | None = None
    nodes: list[int] = field(default_factory=list)   # way node refs (ordered)
    tags: dict[str, str] = field(default_factory=dict)


def _change_type(c: ChangeObj) -> str:
    if not c.visible:
        return "deleted"
    return "added" if c.version <= 1 else "modified"


def _node_feature(c: ChangeObj, change_type: str) -> dict[str, Any]:
    geometry = None
    if c.lat is not None and c.lon is not None:
        geometry = {"type": "Point", "coordinates": [c.lon, c.lat]}
    props = dict(c.tags)
    props.update(osm_id=c.osm_id, osm_type="node", change_type=change_type, version=c.version)
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _way_geometry(refs: list[int], node_locs: dict[int, tuple[float, float]]) -> dict[str, Any] | None:
    """Build LineString/Polygon geometry for a way from resolved node coords.

    Closed ring (first ref == last ref, >= 4 refs) -> Polygon; otherwise
    LineString. Returns ``None`` if any referenced node is unresolved or there
    are too few points to form a geometry.
    """
    if len(refs) < 2:
        return None
    try:
        coords = [[node_locs[r][0], node_locs[r][1]] for r in refs]
    except KeyError:
        return None  # at least one node could not be resolved
    is_closed = len(refs) >= 4 and refs[0] == refs[-1]
    if is_closed:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


def _way_feature(c: ChangeObj, change_type: str,
                 node_locs: dict[int, tuple[float, float]]) -> dict[str, Any]:
    # Deleted ways carry no node refs in the diff -> null geometry (id only).
    geometry = _way_geometry(c.nodes, node_locs) if c.visible and c.nodes else None
    props = dict(c.tags)
    props.update(osm_id=c.osm_id, osm_type="way", change_type=change_type, version=c.version)
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _fc(features: list[dict]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def needed_way_node_ids(changes: list[ChangeObj]) -> set[int]:
    """Pure: the set of node ids referenced by visible changed ways.

    Used to drive the filtered base-PBF scan (so the scan resolves ONLY the
    nodes actually needed by changed-way geometry, not a full node index).
    """
    needed: set[int] = set()
    for c in changes:
        if c.osm_type == "way" and c.visible:
            needed.update(c.nodes)
    return needed


def classify_changes(changes: list[ChangeObj],
                     node_locs: dict[int, tuple[float, float]] | None = None) -> dict[str, Any]:
    """Pure: bucket changed objects into added/modified/deleted FeatureCollections.

    ``node_locs`` maps node id -> (lon, lat) and is used to assemble changed-way
    geometry (it should already include both the diff's own changed nodes and the
    unchanged referenced nodes resolved from the base extract). Nodes resolve
    their own coordinates from the ChangeObj.

    Returns ``{"added": FC, "modified": FC, "deleted": FC, "counts": {...}}``.
    Node and way changes become features (way geometry may be null if
    unresolvable); relation changes are counted only.
    """
    node_locs = node_locs or {}
    buckets: dict[str, list[dict]] = {"added": [], "modified": [], "deleted": []}
    way_counts = {"added": 0, "modified": 0, "deleted": 0}
    rels = 0
    for c in changes:
        if c.osm_type == "relation":
            rels += 1
            continue
        ct = _change_type(c)
        if c.osm_type == "way":
            buckets[ct].append(_way_feature(c, ct, node_locs))
            way_counts[ct] += 1
        else:
            buckets[ct].append(_node_feature(c, ct))
    ways_total = way_counts["added"] + way_counts["modified"] + way_counts["deleted"]
    return {
        "added": _fc(buckets["added"]),
        "modified": _fc(buckets["modified"]),
        "deleted": _fc(buckets["deleted"]),
        "counts": {
            "added": len(buckets["added"]),
            "modified": len(buckets["modified"]),
            "deleted": len(buckets["deleted"]),
            "ways_added": way_counts["added"],
            "ways_modified": way_counts["modified"],
            "ways_deleted": way_counts["deleted"],
            "ways_changed": ways_total,
            "relations_changed": rels,
        },
    }


def _updates_url(region_path: str) -> str:
    return f"{GEOFABRIK_BASE}/{region_path.strip('/')}-updates/"


def _resolve_way_node_locations(local_pbf: str,
                                needed_ids: set[int]) -> dict[int, tuple[float, float]]:
    """Resolve node id -> (lon, lat) for ``needed_ids`` from the BASE extract.

    THE BASE-SCAN IO SEAM. Scans the localized base PBF ONCE, keeping only the
    locations of nodes whose id is in ``needed_ids`` (the nodes referenced by
    changed ways that were NOT themselves changed in the diff). This is a
    filtered, memory-bounded pass — it stores at most ``len(needed_ids)``
    coordinates, NOT a full all-nodes index.

    Returns an empty map when there is nothing to resolve.
    """
    if not needed_ids or not local_pbf:
        return {}
    # pragma: no cover  (osmium base-PBF scan — finalize against installed pyosmium)
    import osmium

    needed = needed_ids  # local ref for the closure
    locs: dict[int, tuple[float, float]] = {}

    class _NodeScan(osmium.SimpleHandler):
        def node(self, n):
            if n.id in needed:
                loc = n.location
                if loc.valid():
                    locs[n.id] = (loc.lon, loc.lat)

    # Node-only pass: no locations=True needed (nodes carry their own coords),
    # so osmium builds no location index at all — bounded by len(needed_ids).
    _NodeScan().apply_file(local_pbf)
    return locs


def _collect_changes(region_path: str, since: str, max_diff_mb: int,
                     local_pbf: str | None) -> tuple[int, list[ChangeObj]]:
    """Resolve the start sequence and read changes since it. THE NETWORK SEAM.

    Start sequence: ``since`` is an ISO date, a sequence number, or "" (= the
    cached extract's own replication sequence, read from ``local_pbf``'s header).
    """
    # pragma: no cover  (network + osmium reader — finalize against installed pyosmium)
    import osmium
    import osmium.replication as rep
    from datetime import datetime, timezone
    from osmium.replication.server import ReplicationServer

    server = ReplicationServer(_updates_url(region_path))
    try:
        if since == "":
            if not local_pbf:
                raise ValueError("ExtractChanges: since=\"\" needs a cached extract baseline")
            header = rep.get_replication_header(local_pbf)
            if header.sequence is None:
                raise ValueError("ExtractChanges: cached extract has no replication baseline; pass an explicit `since`")
            start = header.sequence + 1
        elif since.isdigit():
            start = int(since)
        else:
            dt = datetime.fromisoformat(since)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            start = server.timestamp_to_sequence(dt) or 0

        result = server.collect_diffs(start, max_size=max_diff_mb * 1024)
        if result is None:
            return start, []

        collected: list[ChangeObj] = []

        class _Reader(osmium.SimpleHandler):
            def node(self, n):
                loc = n.location if n.visible else None
                collected.append(ChangeObj(
                    "node", n.id, n.version, n.visible,
                    lat=loc.lat if loc and loc.valid() else None,
                    lon=loc.lon if loc and loc.valid() else None,
                    tags={t.k: t.v for t in n.tags}))

            def way(self, w):
                # The diff carries the way's node REF list but no coordinates;
                # capture refs here and resolve coords from changed nodes + the
                # base extract (see _resolve_way_node_locations).
                refs = [n.ref for n in w.nodes] if w.visible else []
                collected.append(ChangeObj(
                    "way", w.id, w.version, w.visible,
                    nodes=refs,
                    tags={t.k: t.v for t in w.tags}))

            def relation(self, r):
                collected.append(ChangeObj("relation", r.id, r.version, r.visible))

        result.reader.apply(_Reader())
        return start, collected
    finally:
        server.close()
