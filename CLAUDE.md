# CLAUDE.md — osm-geocoder

This repository is a **standalone Facetwork example package**. The Facetwork
platform (workflow compiler + runtime) lives at
`/Users/ralph_lemke/facetwork`; this repo only contains the OSM-specific
FFL, handlers, and tools. The two are wired together via the
`facetwork.domains` entry point in `pyproject.toml`.

## Quick orientation

```
osm/
├── pyproject.toml                  # declares the facetwork.domains entry point
├── src/osm_geocoder/__init__.py    # exports `domain: DomainPackage`
├── src/osm_geocoder/handlers/      # event-facet implementations (27 subpackages; see the library catalog below)
├── src/osm_geocoder/ffl/           # top-level FFL workflows
├── src/osm_geocoder/handlers/<domain>/ffl/   # per-domain FFL (osm-geocoder convention)
├── tools/                          # CLI utilities (PBF → tiles → HTML)
├── tests/                          # repo-level integration tests
└── agent-spec/                     # cross-cutting design specs
```

## Common operations

```bash
# Register this package with Facetwork's runner
pip install -e .

# From a Facetwork checkout:
fw ffl seed --include osm-geocoder
fw runner start --domain osm-geocoder -- --log-format text

# Run as a standalone agent (skip the registry runner path):
PYTHONPATH=src python agent.py

# Tests
pytest tests/ src/osm_geocoder/handlers/ -v
```

## Key concepts (OSM-specific)

### Source Adapter Pattern

Source namespaces normalize different inputs into GeoJSON so downstream analysis
facets work identically regardless of the source:

| Namespace | Input | Handler |
|-----------|-------|---------|
| `osm.Source.PBF` | `.osm.pbf` files via osmium | `handlers/sources/pbf_source.py` |
| `osm.Source.PostGIS` | SQL queries against `osm_nodes` / `osm_ways` | `handlers/sources/postgis_source.py` |
| `osm.Source.GeoJSON` | Existing GeoJSON files | `handlers/sources/geojson_source.py` |
| `osm.Source.OhsomePlanet` | OSM **history** GeoParquet (ohsome-planet) | `handlers/sources/ohsome_source.py` |

**`osm.Source.OhsomePlanet` is the only source with a TIME dimension.** Its rows
are contributions carrying `valid_from`/`valid_to`, so `as_of` on the source turns
any category extract into a historical one and `since`/`until` select an edit
window — putting time on the SOURCE means every extractor gains it rather than
growing a parallel temporal twin. `ExtractChanges` has no analogue in the snapshot
sources: each feature IS an edit, with user, changeset, editor and `contrib_type`.
Mapping is also more faithful than Overture's, because ohsome rows carry OSM's own
`tags` map — `amenity=cafe` is literally `tags["amenity"]`, not a vocabulary
translation. Needs the `[ohsome]` extra (pyarrow + shapely) and a converted
dataset (`FW_OHSOME_PARQUET`); without them the reader raises rather than
returning empty.

Each source provides per-category extraction facets (routes, amenities, roads,
parks, buildings, boundaries, population, POIs) that produce category-specific
output schemas (`RouteFeatures`, `AmenityFeatures`, …). The PBF extractors for
amenities, population (places), parks, and buildings are full osmium passes that
preserve all tags as feature properties plus a derived class — they mirror the
`extract_roads` contract (`localize` → stream via `GeoJSONStreamWriter` → atomic
move; heartbeat every N features). For the common "find a place/business" case,
prefer the cheap cached `ExtractCategory` facade below over a per-category full
pass or a full-PBF tag filter.

**Whole-region conversion.** `osm.Source.PBF.ToGeoJson(cache, format="geojson",
max_pbf_mb=0)` exports an *entire* region PBF to GeoJSON via `osmium export`
(every feature, not a category), object-store-native: it localizes the cached
PBF, runs osmium with a disk-backed node index (`-i sparse_file_array`,
overridable via `FW_OSMIUM_INDEX_TYPE`), and finalizes to the `geojson` cache
type (MinIO on `FW_STORAGE=s3`). Idempotent on the source SHA. `max_pbf_mb`
(0 = no limit; env fallback `FW_OSM_MAX_PBF_MB`) skips regions whose cached PBF
exceeds the limit (returns `skipped=true` without downloading), so a bulk run on
a memory-constrained host skips the continents/big countries that OOM or
seek-thrash osmium's index instead of stalling. Driven by the `osm.convert`
workflows `ConvertAllRegionsToGeoJson(max_pbf_mb)` (ListCachedRegions → foreach →
Download → ToGeoJson) and `ConvertRegionToGeoJson(region, max_pbf_mb)`.

```
Source Layer                        Algorithm Layer (unchanged)
─────────────                       ──────────────────────────
osm.Source.PBF.ExtractRoutes    ─┐
osm.Source.PostGIS.ExtractRoutes ─┼→ GeoJSON → RouteStatistics / FilterRoutesByType / RenderMap
osm.Source.GeoJSON.LoadRoutes   ─┘
```

Composed workflows in `osm.workflows.sourced` demonstrate the pattern:
`BicycleRoutesPBF` / `BicycleRoutesPostGIS` / `BicycleRoutesGeoJSON` — same
pipeline, different sources.

### Subregion fan-out — the PRIMARY path for continents and large countries

**Default to fanning out over subregions; do not download/extract a whole
continent or large-country PBF.** A continent extract (`north-america` ≈ 14 GB,
`europe` ≈ 28 GB) needs a big host — osmium OOMs / seek-thrashes its index on a
modest box — and runs serially. Instead, expand the region to its Geofabrik
**leaves** (states/provinces) and process them in parallel: each leaf PBF is
small enough to extract anywhere, and *N leaves become N independent sub-jobs
across the runner fleet*, then merge into one output.

The building blocks (all already exist, all object-store-native):

- **`osm.Region.ResolveRegions(names, expand="subregions")`** — expands each name
  to its finest Geofabrik leaves (`["North America"]` → 64: US states + Canadian
  provinces + Mexico + Greenland; an already-leaf region passes through). This is
  what keeps fan-out off the un-tractable mega-PBFs (`expand_to_subregions`).
- **`andThen foreach r in resolved.regions { … yield W(paths = [x.output_path]) }`**
  — one parallel sub-block per leaf; the list-typed yield aggregates every leaf's
  output into one list at completion (see `osmcrossregion.ffl` for the yield-merge
  semantics).
- **`osm.Transform.MergeLayers(inputs)`** — concatenate the small per-leaf outputs
  into one GeoJSON.

Canonical examples: **`osm.heatmap.ContinentHeatmap`** (Download → ExtractCategory
→ ByScript per leaf → MergeLayers → RenderHeatmap; `osmheatmap.ffl`) and
**`osm.Cities.workflows.CitiesByZoomTiledMapFanout`** (`osmcities_fanout.ffl`).
Use the single-region variant (e.g. `AmenityHeatmap`) **only** for one
leaf/small region. Add runners (`--scale runner-osm-geocoder=N`) to go faster —
amenity/population scans are node-only and light, so several run per host.

### Cheap category extraction — the warm-cache path

`osm.Source.PBF.ExtractCategory(cache, category) => (output_path, feature_count, category)`
is the **uniform, cheap, cached** way to get one category of features. It is the
preferred Extract primitive for composed workflows; reach for it instead of
filtering the full PBF per query.

- **One warm pass, then instant.** It runs (or reuses a cached) single-pass
  `osm.Combined.CombinedScan` and returns the requested category's GeoJSON path.
  The warm set is the cheap **point** categories `["amenities", "population"]`
  (`_WARM_CATEGORIES` in `pbf_source.py`) — both extracted in one osmium pass for
  ~the cost of one (osmium reads every element once regardless). The first
  `ExtractCategory` on a region pays the pass; every later one — any business
  type, the other warm category — is an **instant manifest lookup**.
- **Heavier families warm on demand.** `roads` / `parks` / `buildings` /
  `routes` / `boundaries` are not in the warm set (they are GB-scale lines/areas
  or relation-shaped); each gets its own cached single-category scan when asked,
  so a business query never pays to extract roads it doesn't use.
- **Shared cache.** `ExtractCategory` and the `osm.Combined.CombinedScan` facet
  hit the *same* per-`(region, categories)` scan sidecar (via the shared
  `ensure_scan()` in `combined_handlers.py`), so they interoperate.
- **The pattern to compose:** `CacheRegion → ExtractCategory(amenities) →
  FilterGeoJSONByOSMType(tag=value) → RenderMap`. This is what `FindBusinessMap`
  does. **Never** use `FilterByOSMTag` over the full PBF for this — that re-scans
  the whole multi-GB dataset every query (the ~54-minute trap); the warm extract
  is paid once and reused.
- **CombinedScan heartbeats.** A single pass over a full-state PBF (e.g.
  California) runs well past the 5-minute task lease, so `combined_scan` threads
  the runner's `_task_heartbeat` through `_CombinedHandler` and beats it on a
  time gate (≥15s) — without this the scan exceeds its lease and is retried in a
  loop. Any long handler added here must do the same (or register `timeout_ms=0`).
- **Node-only scans get a C-level pre-filter.** When every active plugin is
  node-only (no way/area/relation geometry — e.g. the amenities+population warm
  set), `combined_scan` pushes a pyosmium `KeyFilter` (union of the plugins'
  interest keys) down to osmium and drops the location index (`locations=False`),
  so the Python callback fires only for tagged candidates, not the ~99% of nodes
  that are untagged geometry. Benchmarked ~29× (264 MB region: 347s → 12s, counts
  unchanged); the bottleneck was the per-element Python loop, *not* the index.
  Scans that include a way/area/relation plugin keep `locations=True` and no
  filter (member nodes are needed for geometry).

### Updating the cache — diffs, not full re-pulls (Geofabrik rate limits)

Geofabrik rate-limits per IP: a fleet-wide parallel full re-download trips a
temporary block (a 255-wide `RefreshAllCaches` fan-out got our shared egress IP
blocked — `Connection refused` to `download.geofabrik.de` for hours, while the
rest of the internet was fine). So there are two update paths:

- **`osm.cache.UpdateRegion(region, max_diff_mb=512)` / `UpdateAllCaches` /
  `UpdateRegionCaches` — the DEFAULT for "update the cache".** Applies Geofabrik's
  daily replication diffs (`<region>-updates/` `.osc.gz`) to the cached PBF via
  the pyosmium 4.x API (`osmium.replication.get_replication_header` +
  `ReplicationServer.apply_diffs_to_file`). Transfers KB–MB per region (a day of
  change), sequentially — the recommended, rate-limit-friendly path, safe to fan
  out across the fleet. `method` returns `current` / `diff` / `full`; it falls
  back to a full `Download(refresh)` only when an extract has no replication
  baseline in its header or is too far behind the diff budget.
- **`osm.cache.RefreshAllCaches` / `RefreshRegionCaches` (`Download` with
  `cache_policy="refresh"`) — full re-pull. Heavy; rate-limit-dangerous at high
  fan-out.** Use only for a deliberate from-scratch refresh, and throttle it
  (low concurrency) — do NOT fan it out 200-wide.

Rule of thumb: **to keep the cache current, use `UpdateAllCaches` (diffs); reserve
the full-download `Refresh*` workflows for re-establishing extracts.**

**Rate-limit-safe downloads.** Independently of the diff-vs-full choice, every
cache-miss network fetch is defended in `tools/_osm_tools/pbf_download.py` +
`download_gate.py`: a **Mongo-backed, fleet-wide download semaphore**
(`download_gate.py`) caps concurrent Geofabrik fetches so independent runners
can't collectively hammer the shared egress IP (it wraps only the cache-miss
fetch and **fails open** if Mongo is unavailable); HTTP `429`/`503` are retried
honouring a **capped `Retry-After`**; and a cached file is revalidated with a
conditional GET (`If-Modified-Since` → `304` → cache hit, no re-download). Knobs:
`FW_GEOFABRIK_BASE_URL` (constant `GEOFABRIK_BASE`, default
`https://download.geofabrik.de`) reroutes **all** region + replication URLs to a
mirror/internal cache; `FW_OSM_DOWNLOAD_CONCURRENCY` (default `3`),
`FW_OSM_DOWNLOAD_LEASE_MS`, `FW_OSM_RETRY_AFTER_CAP_SECONDS` (`300`),
`FW_OSM_RATE_LIMIT_MAX_ATTEMPTS` (`6`). See `tools/README.md` →
**Rate-limit-safe downloads**.

The same replication machinery also powers **change detection** —
`osm.Change.ExtractChanges(region, since="", max_diff_mb=512) => ChangeSet`
surfaces the features *added / modified / deleted* since a date/sequence (or the
cache's own replication timestamp) as GeoJSON, instead of applying the diffs.
The "what's new/removed this month" feed. It emits **full geometry for nodes,
ways, AND relations**: `Point` (node), `LineString` (open way), `Polygon` (area
way) and `Polygon`/`MultiPolygon` (relation) — relations were previously
counted-only. Way/relation geometry is assembled by the **osmium CLI's area
builder**: the collected replication diff is applied onto the cached base extract,
the changed way/relation ids + members are subset out (`osmium apply-changes` →
`getid -r` → `export -a type,id`), and the result is deduped preferring the area
interpretation — so a closed *highway* stays a `LineString` while a closed
*building* becomes a `Polygon` (area-vs-line is decided by OSM area rules, not a
naive ring guess). The `ChangeSet` schema carries the per-type breakdown
`nodes_*` plus `ways_added`/`ways_modified`/`ways_deleted` and
`relations_added`/`relations_modified`/`relations_deleted`. **Bulkheads:** a
node-only diff (the common POI case) skips the osmium pass entirely; an uncached
region or any osmium failure yields **null** geometry rather than a crash, and
deleted objects (whose geometry is gone upstream) are emitted with null geometry.
Both `UpdateRegion` and `ExtractChanges` read `ReplicationServer.collect_diffs`
(pyosmium 4.x), so both depend on Geofabrik replication reachability.

The throttled **`update-delta`** CLI (`tools/update-delta.sh`) is the
command-line counterpart to `UpdateRegion` — serial replication-diff updates,
one region at a time with a `--delay`, diffs-only by default (`--allow-full` to
permit the full-download fallback). See `tools/README.md` → **Cache updates**.

### Composable facet library

Beyond the source adapters, this package ships a layered library of orthogonal,
path-composable primitives (design: facetwork's
`docs/architecture/composable-facet-library.md`). Discover them at runtime with
the **`fw_capabilities`** MCP tool (NL → facet) and resolve NL terms to OSM tags
with **`osm.Vocab.ResolveTag`** (NL → `key=value`) — *lookup-then-compose*.

| Layer | Namespace | Verbs |
|-------|-----------|-------|
| Source | `osm.ops`, `osm.Source.*` | `CacheRegion`, `ExtractCategory`, `Extract*` |
| Clip | `osm.Clip` | `ClipByBBox`, `ClipByPolygon` (osmium extract → clipped `OSMCache`) |
| Filter | `osm.Filters` | `FilterGeoJSONByOSMType` / `TagPrefix` / `TagContains` / `TagRegex`, radius, type |
| Spatial | `osm.Spatial` | `WithinDistance`, `BeyondDistance`, `Nearest`, `SpatialJoin`, `Buffer`, `Intersect`, `Union`, `Centroid`, `Simplify` (shapely STRtree + local AEQD) |
| Transform | `osm.Transform` | `MergeLayers`, `Summarize` (count/sum/avg/min/max), `Dissolve` |
| Geocoding | `osm.geocode` | `Geocode`, `ReverseGeocode` (Nominatim) |
| Vocabulary | `osm.Vocab` | `ResolveTag`, `ListTagValues` (NL term → OSM `key=value`) |
| Routing (engines) | `osm.Routing.{OSRM,API,Valhalla,GraphHopper,PgRouting}` | `Route`, `MultiStopRoute`, `Isochrone`, `Matrix`, `Nearest`, `MapMatch`, `Trip` (verbs per engine; uniform `osm.Routing.Types` schemas — swap engine by namespace) |
| Network (approx routing) | `osm.Network` | `BuildNetwork`, `ApproxRoute`, `RouteMatrix` — **engine-free**: pure shapely/networkx graph search over a tiny noded-freeway cache artifact (no daemon, read-once-per-runner). `+ workflows.CityRoutesByPopulation` / `RouteFanout` |
| Render / Tiles | `osm.viz`, `osm.Tiles` | `RenderMap`, `RenderLayers`, `BuildVectorTiles` (tippecanoe → MBTiles/PMTiles) |

Everything operates on GeoJSON **paths** (or an `OSMCache` for Source/Clip), so
steps chain: `CacheRegion → ExtractCategory → Filter* → Spatial/Transform →
RenderMap/BuildVectorTiles`. New facets follow the established pattern: FFL under
`handlers/<area>/ffl/`, a handler module + `register_*`/`register_handlers` wired
into `handlers/__init__.py`, tool logic in `tools/_osm_tools/` reached via the
`shared/pbf_convert.py` shim, and deterministic tests under `handlers/<area>/tests/`.

**Effect/cost annotations.** Facets may declare `with Effect(kind = "pure"|"external"|"io")` and
`with Cost(tier = "free"|"cheap"|"moderate"|"expensive")` after the `=> (...)` return clause (cost is
also inferred from an existing `with Timeout(minutes = …)`). `fw_capabilities` surfaces + filters on
these so the composer prefers pure/cheap primitives. **All event facets are annotated** (classified
by parameter type + namespace): PBF extractors / builds / imports / downloads → external/expensive;
PostGIS + routing engines → external/moderate; GeoJSON filters / stats / Spatial / Transform →
pure/cheap; `Load*` + render → io/cheap; vocab → pure/free. When adding a facet, tag it the same way:
a facet taking `cache: OSMCache` scans the PBF (external/expensive); one taking `input_path` (GeoJSON)
is in-process (pure/cheap); anything hitting a server/DB/subprocess is external.

**External-engine deps** (the only non-pure facets): Routing needs a running OSRM
(`FW_OSRM_URL`, default `http://localhost:5000`); `BuildVectorTiles` needs
`tippecanoe` (+ `pmtiles` for PMTiles output); geocoding hits Nominatim
(`FW_NOMINATIM_URL`). Each degrades gracefully or fails explicitly when its
engine is absent.

**`osm.Network` is the engine-free routing tier.** When you only need approximate
corridor distances (not turn-by-turn), `BuildNetwork`/`ApproxRoute`/`RouteMatrix`
route over a tiny noded-freeway graph entirely in-process — no OSRM daemon, no
GB-scale build. The graph is a small (~MB) content-addressed `osm/network/` cache
artifact, so every runner reads it once and the work fans out lock-free across the
fleet (proven live in Docker — see facetwork's
`docs/architecture/approximate-freeway-routing.md` §8). `ApproxRoute` reports the
closest reachable point + a `gap_to_b_km` when the destination is off-freeway.

### Output artifact naming

Derived **leaf** artifacts (a filtered GeoJSON, a rendered map) are named by
`derive_output_path()` in `handlers/shared/_output.py`, which encodes the
*discriminating parameters* of the query rather than just the input stem:
`…/osm-filtered/california-latest.osm_amenities_filtered_amenity-fast_food.geojson`.
So two different queries over the same input (e.g. `fast_food` vs `cafe`) no
longer overwrite each other, while the same query stays idempotent (same name →
safe overwrite with identical content). Long/odd param sets fall back to a short
stable hash; `*`/`None`/`""` wildcards are dropped. The rendered map inherits the
now-unique GeoJSON stem, so it is unique too.

**Do not** use `derive_output_path()` for *cacheable intermediates* (category
extracts, scan manifests) — those are input-addressed and shared across runs by
design (that sharing is what makes `ExtractCategory` cheap).

Opt-in per-run isolation: set **`FW_OUTPUT_PER_RUN=1`** and leaf artifacts land
under `<category>/runs/<workflow_id>/` (handlers pass `run_id=payload["_workflow_id"]`,
the execution id injected by the runner) — a retained, easily-cleaned per-run
tree. Default (unset) keeps the shared, cache-friendly directory.

### PostGIS source

Connects to the `osm` database (default: `FW_POSTGIS_URL`). The
`PostGISSource` schema takes `postgis_url` and `region` parameters. Queries
use `tags JSONB` for filtering, e.g. `tags->>'amenity' = 'hospital'`.

osm2pgsql-compatible views are auto-created by `ensure_schema`:
- `planet_osm_point` — nodes with flattened tag columns (name, amenity, shop, highway, building, tourism, place, …)
- `planet_osm_line` — ways with flattened tag columns (highway, railway, waterway, surface, lanes, …)
- `planet_osm_roads` — filtered ways where `highway` or `railway` is present

### Tools / handlers / cache pattern

Every domain pipeline follows one contract: a `tools/` dir of Python CLIs +
shell wrappers backed by `tools/_osm_tools/`, FFL handlers that call into the
same `_osm_tools/` via a `handlers/shared/<domain>_utils.py` shim, and a
sidecar-backed cache under `$FW_CACHE_ROOT/<namespace>/`. See
`agent-spec/tools-pattern.agent-spec.yaml` and
`agent-spec/cache-layout.agent-spec.yaml` for the full contract.

## Runner timeout overrides

PostGIS imports can take hours for large regions (e.g. California 1.2GB
PBF). The default 15-minute execution timeout kills imports before they
complete. The package bakes 4-hour overrides into the `DomainPackage` so
they apply automatically:

```python
runner_env = {
    "FW_TASK_EXECUTION_TIMEOUT_MS": "14400000",  # 4 hours
    "FW_STUCK_TIMEOUT_MS":           "14400000",
}
```

Heartbeats fire during the osmium scan but cannot fire during blocking
PostgreSQL UPSERT calls — the timeout must accommodate the longest
possible single-batch DB write.

## Local-first PostGIS import

For large imports, a disposable Docker-based PostgreSQL instance can absorb
the hours of PBF parsing I/O, then bulk-transfer the finished data to the
main server. Set `FW_IMPORT_POSTGIS_URL` (e.g.
`postgresql://afl_osm:afl_osm_2024@localhost:5433/osm`) to enable; the
import flow then:

1. Parses PBF and stages on the local instance (fast — disposable, no WAL)
2. Merges staging into local main tables (no index contention with readers)
3. Transfers via `COPY` binary stream from local to main server
4. Batched merge into main server tables
5. Writes audit log on the main server

The local instance is tuned with `fsync=off`, `synchronous_commit=off`,
`autovacuum=off` — disposable, so crash recovery is simply re-importing
from PBF.

## Adding new handlers

1. Add a Python module under `src/osm_geocoder/handlers/<domain>/`.
2. Export `register_handlers(runner)` that calls
   `runner.register_handler(facet_name=..., module_uri=f"file://{os.path.abspath(__file__)}", entrypoint=...)`.
3. Wire it into `register_all_registry_handlers` in
   `src/osm_geocoder/handlers/__init__.py`.
4. Drop the FFL declaration into `src/osm_geocoder/ffl/` (or a domain-specific
   `handlers/<domain>/ffl/` for nested workflows).
5. Re-run `fw ffl seed --include osm-geocoder` so the new flow
   shows up in the dashboard.

## Code review checklist

- For every state transition: "what if this crashes halfway?" Design the recovery path.
- For every timeout: heartbeat-aware. Distinguish start-to-close from last-activity.
- For every retry: max count and backoff. No infinite loops.
- For every shared resource (thread pool, connection, queue): consider isolation/bulkheads.
- For every log message at WARNING+: include a qualified human-readable name, not just IDs.
- For every error handler: never silently return empty defaults. Fail explicitly or re-raise.

## Domain research before implementation

For OSM/geospatial work, apply established practices:
- osmium processing patterns (single-pass reads; no random access into PBF)
- PostGIS indexing strategies (GiST on geom, BRIN on import-order, partial indices on common tag predicates)
- Coordinate system conventions (everything stored EPSG:4326; reproject on read for area/distance)
- Bulk import best practices (COPY > INSERT; tune `maintenance_work_mem`, disable autovacuum during load, re-cluster + ANALYZE after)
- Tile pyramids — keep min/max zoom limits explicit; fail loudly if extents fall outside
