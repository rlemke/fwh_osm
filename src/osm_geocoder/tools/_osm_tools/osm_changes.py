"""Extract changed OSM features from Geofabrik replication diffs.

Tool side of ``osm.Change.ExtractChanges``. Reuses the replication API used by
``pbf_update`` (``ReplicationServer.collect_diffs``) but, instead of applying the
diffs to the cached PBF, surfaces them: which features were added / modified /
deleted since a sequence/date.

Split so the pure classification (diff objects -> GeoJSON FeatureCollections) is
unit-tested offline, and only the network read is the seam:

  - ``_collect_changes``  THE NETWORK SEAM (mocked in tests): resolves the start
    sequence and reads the merged change buffer into ``ChangeObj`` records.
  - ``classify_changes``  pure: ChangeObj records -> {added, modified, deleted}
    FeatureCollections + counts.

v1 emits NODE changes with Point geometry (the POI case); way/relation changes
are counted only (their geometry needs the base extract — a follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pbf_download import GEOFABRIK_BASE


@dataclass
class ChangeObj:
    """One changed OSM object from a replication diff."""
    osm_type: str          # "node" | "way" | "relation"
    osm_id: int
    version: int
    visible: bool
    lat: float | None = None
    lon: float | None = None
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


def _fc(features: list[dict]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def classify_changes(changes: list[ChangeObj]) -> dict[str, Any]:
    """Pure: bucket changed objects into added/modified/deleted FeatureCollections.

    Returns ``{"added": FC, "modified": FC, "deleted": FC, "counts": {...}}``.
    Only NODE changes become features (v1); ways/relations are counted.
    """
    buckets: dict[str, list[dict]] = {"added": [], "modified": [], "deleted": []}
    ways = rels = 0
    for c in changes:
        if c.osm_type == "way":
            ways += 1
            continue
        if c.osm_type == "relation":
            rels += 1
            continue
        ct = _change_type(c)
        buckets[ct].append(_node_feature(c, ct))
    return {
        "added": _fc(buckets["added"]),
        "modified": _fc(buckets["modified"]),
        "deleted": _fc(buckets["deleted"]),
        "counts": {
            "added": len(buckets["added"]),
            "modified": len(buckets["modified"]),
            "deleted": len(buckets["deleted"]),
            "ways_changed": ways,
            "relations_changed": rels,
        },
    }


def _updates_url(region_path: str) -> str:
    return f"{GEOFABRIK_BASE}/{region_path.strip('/')}-updates/"


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
                collected.append(ChangeObj("way", w.id, w.version, w.visible))

            def relation(self, r):
                collected.append(ChangeObj("relation", r.id, r.version, r.visible))

        result.reader.apply(_Reader())
        return start, collected
    finally:
        server.close()
