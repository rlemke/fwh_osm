"""Extract changed OSM features from Geofabrik replication diffs.

Tool side of ``osm.Change.ExtractChanges``. Reuses the replication API used by
``pbf_update`` (``ReplicationServer.collect_diffs``) but, instead of applying the
diffs to the cached PBF for a fresh extract, SURFACES them: which features were
added / modified / deleted since a sequence/date.

Geometry is built by the SAME engine the static extractors trust — the ``osmium``
CLI's area assembler — rather than hand-rolled from node refs. A replication diff
ships only node refs (ways) and member lists (relations), not coordinates, so we:

  1. persist the collected diff to a local ``.osc.gz`` (``_collect_changes``),
  2. ``osmium apply-changes`` it onto the cached base PBF -> the new state,
  3. ``osmium getid -r`` the changed way/relation ids (recursively pulling their
     member ways + nodes) -> a tiny subset PBF,
  4. ``osmium export -a type,id`` the subset -> GeoJSON stamped with @type/@id,
     deduped preferring the area interpretation.

This yields correct Point / LineString / Polygon / MultiPolygon for nodes / ways /
relations (including multipolygon holes and proper area-vs-line determination —
a closed ``highway`` is a LineString, a closed ``building`` is a Polygon), keyed
back to the change records by osm id.

Split so the pure classification (diff objects + a geometry map -> GeoJSON
FeatureCollections) is unit-tested offline, and only the network/CLI reads are
seams:

  - ``_collect_changes``   THE NETWORK SEAM (mocked in tests): resolves the start
    sequence, reads the merged change buffer into ``ChangeObj`` records, and
    writes the same changes to a local ``.osc.gz`` for the geometry pass.
  - ``_assemble_geometry``  THE CLI/IO SEAM (mocked in tests): apply-changes ->
    getid -r -> export, returning ``{(osm_type, osm_id): geometry}`` for the
    changed ways/relations.
  - ``classify_changes``  pure: ChangeObj records + a geometry map ->
    {added, modified, deleted} FeatureCollections + counts.

Geometry degrades gracefully: a way/relation whose geometry can't be assembled
(base PBF not cached, or osmium can't build it) is emitted with null geometry —
it still identifies osm_id + change_type, never a crash. Deleted objects always
carry null geometry (they no longer exist in the new state).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .pbf_download import GEOFABRIK_BASE

# The osmium CLI (already a hard dependency of the static extractors). Overridable
# for hosts where it isn't on PATH.
_OSMIUM_BIN = os.environ.get("AFL_OSMIUM_BIN", "osmium")

# Type char <-> osmium @type, and the id-token form osmium getid -i expects.
_TYPE_CHAR = {"node": "n", "way": "w", "relation": "r"}
_AREA_GEOMS = {"Polygon", "MultiPolygon"}


@dataclass
class ChangeObj:
    """One changed OSM object from a replication diff.

    Nodes carry ``lat``/``lon`` (their new coords). Ways and relations carry no
    coordinates in a diff — their geometry is assembled by ``_assemble_geometry``
    from the diff-applied base extract and keyed back here by ``osm_id``.
    """
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


def _node_geometry(c: ChangeObj) -> dict[str, Any] | None:
    if c.lat is not None and c.lon is not None:
        return {"type": "Point", "coordinates": [c.lon, c.lat]}
    return None


def _feature(c: ChangeObj, change_type: str, geometry: dict[str, Any] | None) -> dict[str, Any]:
    props = dict(c.tags)
    props.update(osm_id=c.osm_id, osm_type=c.osm_type, change_type=change_type, version=c.version)
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _fc(features: list[dict]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def changed_geometry_tokens(changes: list[ChangeObj]) -> list[str]:
    """Pure: id tokens (``w<id>`` / ``r<id>``) for visible added/modified ways &
    relations — the objects whose geometry must be assembled from the base extract.

    Deleted objects (gone in the new state) and nodes (geometry inline) are
    excluded, so an all-node diff yields ``[]`` and the handler skips the
    expensive osmium pass entirely.
    """
    tokens: list[str] = []
    for c in changes:
        if c.visible and c.osm_type in ("way", "relation") and _change_type(c) in ("added", "modified"):
            tokens.append(_TYPE_CHAR[c.osm_type] + str(c.osm_id))
    return tokens


def classify_changes(changes: list[ChangeObj],
                     geom_map: dict[tuple[str, int], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Pure: bucket changed objects into added/modified/deleted FeatureCollections.

    ``geom_map`` maps ``(osm_type, osm_id) -> geometry`` for ways/relations (built
    by ``_assemble_geometry``). Nodes resolve their own Point geometry inline from
    the ChangeObj. A way/relation absent from ``geom_map`` (or any deleted object)
    gets null geometry but still identifies its osm_id + change_type.

    Returns ``{"added": FC, "modified": FC, "deleted": FC, "counts": {...}}`` —
    node, way AND relation changes become features.
    """
    geom_map = geom_map or {}
    buckets: dict[str, list[dict]] = {"added": [], "modified": [], "deleted": []}
    way_counts = {"added": 0, "modified": 0, "deleted": 0}
    rel_counts = {"added": 0, "modified": 0, "deleted": 0}
    for c in changes:
        ct = _change_type(c)
        if c.osm_type == "node":
            geom = _node_geometry(c) if c.visible else None
        elif c.osm_type == "way":
            geom = geom_map.get(("way", c.osm_id)) if c.visible else None
            way_counts[ct] += 1
        elif c.osm_type == "relation":
            geom = geom_map.get(("relation", c.osm_id)) if c.visible else None
            rel_counts[ct] += 1
        else:
            continue
        buckets[ct].append(_feature(c, ct, geom))

    ways_total = sum(way_counts.values())
    rels_total = sum(rel_counts.values())
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
            "relations_added": rel_counts["added"],
            "relations_modified": rel_counts["modified"],
            "relations_deleted": rel_counts["deleted"],
            "relations_changed": rels_total,
        },
    }


def _updates_url(region_path: str) -> str:
    return f"{GEOFABRIK_BASE}/{region_path.strip('/')}-updates/"


def _parse_export(geojsonseq: str, wanted: set[str]) -> dict[tuple[str, int], dict[str, Any]]:
    """Parse ``osmium export -a type,id`` GeoJSONSeq into ``{(type, id): geometry}``.

    Keeps only features whose id token is in ``wanted`` (``getid -r`` also emits
    tagged member ways of changed relations — we drop those). When osmium emits
    both a line and an area for the same id (a closed area-tagged way), the area
    interpretation wins.
    """
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for line in geojsonseq.splitlines():
        line = line.strip().lstrip("\x1e")  # RS-delimited geojsonseq
        if not line:
            continue
        try:
            feat = json.loads(line)
        except json.JSONDecodeError:
            continue
        props = feat.get("properties") or {}
        otype = props.get("@type")
        oid = props.get("@id")
        geom = feat.get("geometry")
        if otype not in ("way", "relation") or oid is None or geom is None:
            continue
        token = _TYPE_CHAR[otype] + str(oid)
        if token not in wanted:
            continue
        key = (otype, int(oid))
        prev = result.get(key)
        if prev is None or (geom.get("type") in _AREA_GEOMS and prev.get("type") not in _AREA_GEOMS):
            result[key] = geom
    return result


def _assemble_geometry(base_pbf: str, osc_path: str,
                       id_tokens: list[str]) -> dict[tuple[str, int], dict[str, Any]]:
    """THE CLI/IO SEAM: build geometry for changed ways/relations via osmium.

    apply-changes (diff onto the cached base) -> getid -r (changed ids + their
    members) -> export (-a type,id). Returns ``{(osm_type, osm_id): geometry}``;
    empty on any missing input or osmium failure (caller falls back to null
    geometry, never crashes the whole extraction).
    """
    if not id_tokens or not base_pbf or not osc_path or not os.path.exists(osc_path):
        return {}
    work = tempfile.mkdtemp(prefix="osmchg-geom-")
    try:
        new_state = os.path.join(work, "new_state.osm.pbf")
        subset = os.path.join(work, "subset.osm.pbf")
        idfile = os.path.join(work, "ids.txt")
        with open(idfile, "w") as f:
            f.write("\n".join(id_tokens) + "\n")

        def _run(args: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run([_OSMIUM_BIN, *args], check=True,
                                  capture_output=True, text=True)

        _run(["apply-changes", base_pbf, osc_path, "-o", new_state, "--overwrite"])
        _run(["getid", "-r", "-i", idfile, new_state, "-o", subset, "--overwrite"])
        exported = _run(["export", "-a", "type,id", "-f", "geojsonseq", subset])
        return _parse_export(exported.stdout, set(id_tokens))
    except (OSError, subprocess.SubprocessError):
        # osmium not on PATH / build failure -> degrade to null geometry.
        return {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _collect_changes(region_path: str, since: str, max_diff_mb: int,
                     local_pbf: str | None) -> tuple[int, list[ChangeObj], str | None]:
    """Resolve the start sequence, read changes, and persist them. THE NETWORK SEAM.

    Returns ``(start_sequence, changes, osc_path)``. ``osc_path`` is a local
    ``.osc.gz`` of the collected diff (for the geometry pass) inside a fresh temp
    dir the CALLER must clean up (``shutil.rmtree(os.path.dirname(osc_path))``);
    it is ``None`` when there were no changes.

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
            return start, [], None

        collected: list[ChangeObj] = []
        work = tempfile.mkdtemp(prefix="osmchg-osc-")
        osc_path = os.path.join(work, "changes.osc.gz")
        writer = osmium.SimpleWriter(osc_path)

        class _Reader(osmium.SimpleHandler):
            def node(self, n):
                writer.add_node(n)
                loc = n.location if n.visible else None
                collected.append(ChangeObj(
                    "node", n.id, n.version, n.visible,
                    lat=loc.lat if loc and loc.valid() else None,
                    lon=loc.lon if loc and loc.valid() else None,
                    tags={t.k: t.v for t in n.tags}))

            def way(self, w):
                writer.add_way(w)
                collected.append(ChangeObj(
                    "way", w.id, w.version, w.visible,
                    tags={t.k: t.v for t in w.tags}))

            def relation(self, r):
                writer.add_relation(r)
                collected.append(ChangeObj(
                    "relation", r.id, r.version, r.visible,
                    tags={t.k: t.v for t in r.tags}))

        result.reader.apply(_Reader())
        writer.close()
        return start, collected, osc_path
    finally:
        server.close()
