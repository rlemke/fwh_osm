# OSM Changes — surfacing what changed from replication diffs

**Namespace(s):** `osm.Change` ·
**FFL:** `src/osm_geocoder/handlers/change/ffl/osmchange.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/change/change_handlers.py` ·
**Tools:** `tools/_osm_tools/osm_changes.py`

## Overview

`osm.Change` answers "*what changed*" — which OSM features were **added, modified,
or deleted** in a region since a point in time. "What new restaurants opened",
"which buildings were removed", freshness monitoring. It reuses the exact same
Geofabrik replication machinery as `osm.cache.UpdateRegion`, but instead of
*applying* the day's `.osc.gz` diffs to the cached PBF for a fresh extract, it
**surfaces** them as three GeoJSON FeatureCollections (added / modified / deleted).

It sits alongside the cache layer as the "delta" verb: light on the network (a diff
is KB–MB, not the multi-GB extract), with local geometry assembly from the cached
base PBF.

## How it works

A replication diff ships only node refs (for ways) and member lists (for
relations) — **not** coordinates — so geometry has to be rebuilt locally. The tool
(`osm_changes.py`) does this with the same `osmium` engine the static extractors
trust, split into testable seams:

1. **`_collect_changes`** (the network seam) — resolves the start sequence from
   `since`, reads the merged replication change buffer into `ChangeObj` records
   (via pyosmium's `ReplicationServer.collect_diffs`), and persists the collected
   diff to a local `.osc.gz` for the geometry pass. `since` may be an ISO date
   (`"2026-06-01"`), a replication sequence number, or `""` (= the cached extract's
   own replication timestamp → "what changed since my cache was last current").
   `max_diff_mb` caps the diff data pulled.
2. **Lazy geometry escalation** — `changed_geometry_tokens` inspects the changes;
   if the diff touched **only nodes** (the POI case), the expensive osmium pass is
   skipped entirely and nodes get inline `Point` geometry. The heavy assembly runs
   only when ways/relations changed, and only for those ids.
3. **`_assemble_geometry`** (the CLI/IO seam) — for changed way/relation ids:
   `osmium apply-changes` the `.osc.gz` onto the cached base PBF → the new state;
   `osmium getid -r` recursively pulls the changed ids plus their member ways +
   nodes → a tiny subset PBF; `osmium export -a type,id` → GeoJSON stamped with
   `@type`/`@id`, deduped preferring the area interpretation. This yields correct
   `Point` / `LineString` / `Polygon` / `MultiPolygon` per OSM area rules (a closed
   `highway` is a LineString, a closed `building` is a Polygon).
4. **`classify_changes`** (pure) — `ChangeObj` records + the geometry map →
   `{added, modified, deleted}` FeatureCollections + counts. Each feature carries
   its tags, `osm_id`, `osm_type`, `change_type`, and `version`.

The handler (`handle_extract_changes`) localizes the base PBF, runs the collect →
assemble → classify pipeline, writes the three FeatureCollections to the output
store, cleans up the temp `.osc.gz`, and returns a `ChangeSet`.

## Fan-out

Single-task per region — no `foreach` in `osmchange.ffl`. `ExtractChanges` is one
event facet over one region's diff stream; the diff collection is inherently
sequential (replication sequences are ordered) and light, so there is no per-leaf
fan-out here. A caller wanting many regions would compose `ExtractChanges` under an
`andThen foreach` at the workflow layer, exactly as the cache-update workflows do.

## Filtering & attributes

No tag *filter* — `ExtractChanges` surfaces **every** changed object (node, way,
relation) in the diff, carrying its full tag set through to the output features.
The only classification is by **change type** (added = new, modified = edited,
deleted = removed) and geometry type (derived from the OSM object type + area
rules, not a tag predicate). Downstream analysis can then filter the emitted
GeoJSON by tag (e.g. `amenity=restaurant`) using the standard filter facets.

## External libraries / binaries

- **`osmium` (osmium-tool binary)** — `apply-changes`, `getid -r`, `export`; the
  geometry assembler. A **binary** dependency (already required by the static
  extractors); overridable via `FW_OSMIUM_BIN`.
- **`pyosmium`** (pip `osmium`) — `osmium.replication.server.ReplicationServer` /
  `collect_diffs` for reading the replication stream (the network seam).
- **stdlib** (`subprocess`, `json`, `tempfile`, `shutil`) for orchestrating the
  osmium CLI and staging the `.osc.gz` + subset.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.Change.ExtractChanges(region: Region, since, max_diff_mb)` | event | Changed features (added/modified/deleted) since `since`, as three GeoJSON paths + counts. `Effect(external)`, `Cost(moderate)` |

Result schema `osm.Change.ChangeSet`: `region`, the three GeoJSON paths
(`added`/`modified`/`deleted`), per-bucket `*_count`, and way/relation breakdowns
(`ways_added/modified/deleted/changed`, `relations_*`), plus `since_sequence`.

## Cache / output

- **Output:** three GeoJSON FeatureCollections (added/modified/deleted) written to
  the configured output store (`resolve_output_dir` — MinIO on `FW_STORAGE=s3`),
  referenced by path in the returned `ChangeSet`.
- **Cache:** reads the base PBF from the shared `cache/osm/pbf/` cache (localizing
  from the object store first when on S3). The collected `.osc.gz` is a throwaway
  temp dir the handler removes after the geometry pass — not cached.

## Gotchas & notes

- **Geometry degrades gracefully to null.** A way/relation whose geometry can't be
  assembled (base PBF not cached on this host, or osmium can't build it) is emitted
  with `null` geometry but still identifies `osm_id` + `change_type` — never a
  crash. **Deleted** objects always carry null geometry (they no longer exist in
  the new state).
- **Cache-dependent geometry.** Way/relation geometry requires the region's PBF in
  the local cache; if it isn't cached, only node changes get real (Point) geometry.
- **The node-only fast path is a bulkhead.** An all-node diff never pays for the
  osmium apply-changes/getid/export pass — important because that pass is the
  expensive part.
- **Provider consistency.** Diffs come from the same replication tree as the cached
  extract's baseline (via `GEOFABRIK_BASE`); mixing an osmfr-sourced baseline with
  Geofabrik diffs would be unsound (see the cache spec's provider note).

## Related specs

- [cache-and-download](cache-and-download.md) — shares the replication machinery;
  `UpdateRegion` *applies* diffs where `ExtractChanges` *surfaces* them.
- [source-adapters](source-adapters.md) — the GeoJSON output can feed the GeoJSON
  source adapter for further filtering/analysis.
- [planet-extraction](planet-extraction.md) — the replication-header indirection
  that makes delta upkeep possible.
