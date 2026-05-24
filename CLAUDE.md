# CLAUDE.md — osm-geocoder

This repository is a **standalone Facetwork example package**. The Facetwork
platform (workflow compiler + runtime) lives at
`/Users/ralph_lemke/facetwork`; this repo only contains the OSM-specific
FFL, handlers, and tools. The two are wired together via the
`facetwork.examples` entry point in `pyproject.toml`.

## Quick orientation

```
osm/
├── pyproject.toml                  # declares the facetwork.examples entry point
├── src/osm_geocoder/__init__.py    # exports `example: ExamplePackage`
├── src/osm_geocoder/handlers/      # event-facet implementations (23 subpackages)
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
scripts/seed-examples --include osm-geocoder
scripts/start-runner --example osm-geocoder -- --log-format text

# Run as a standalone agent (skip the registry runner path):
PYTHONPATH=src python agent.py

# Tests
pytest tests/ src/osm_geocoder/handlers/ -v
```

## Key concepts (OSM-specific)

### Source Adapter Pattern

Three source namespaces normalize different inputs into GeoJSON so
downstream analysis facets work identically regardless of the source:

| Namespace | Input | Handler |
|-----------|-------|---------|
| `osm.Source.PBF` | `.osm.pbf` files via osmium | `handlers/sources/pbf_source.py` |
| `osm.Source.PostGIS` | SQL queries against `osm_nodes` / `osm_ways` | `handlers/sources/postgis_source.py` |
| `osm.Source.GeoJSON` | Existing GeoJSON files | `handlers/sources/geojson_source.py` |

Each source provides per-category extraction facets (routes, amenities, roads,
parks, buildings, boundaries, population, POIs) that produce category-specific
output schemas (`RouteFeatures`, `AmenityFeatures`, …). The PBF extractors for
amenities, population (places), parks, and buildings are full osmium passes that
preserve all tags as feature properties plus a derived class — they mirror the
`extract_roads` contract (`localize` → stream via `GeoJSONStreamWriter` → atomic
move; heartbeat every N features). For the common "find a place/business" case,
prefer the cheap cached `ExtractCategory` facade below over a per-category full
pass or a full-PBF tag filter.

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

Opt-in per-run isolation: set **`AFL_OUTPUT_PER_RUN=1`** and leaf artifacts land
under `<category>/runs/<workflow_id>/` (handlers pass `run_id=payload["_workflow_id"]`,
the execution id injected by the runner) — a retained, easily-cleaned per-run
tree. Default (unset) keeps the shared, cache-friendly directory.

### PostGIS source

Connects to the `osm` database (default: `AFL_POSTGIS_URL`). The
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
sidecar-backed cache under `$AFL_CACHE_ROOT/<namespace>/`. See
`agent-spec/tools-pattern.agent-spec.yaml` and
`agent-spec/cache-layout.agent-spec.yaml` for the full contract.

## Runner timeout overrides

PostGIS imports can take hours for large regions (e.g. California 1.2GB
PBF). The default 15-minute execution timeout kills imports before they
complete. The package bakes 4-hour overrides into the `ExamplePackage` so
they apply automatically:

```python
runner_env = {
    "AFL_TASK_EXECUTION_TIMEOUT_MS": "14400000",  # 4 hours
    "AFL_STUCK_TIMEOUT_MS":           "14400000",
}
```

Heartbeats fire during the osmium scan but cannot fire during blocking
PostgreSQL UPSERT calls — the timeout must accommodate the longest
possible single-batch DB write.

## Local-first PostGIS import

For large imports, a disposable Docker-based PostgreSQL instance can absorb
the hours of PBF parsing I/O, then bulk-transfer the finished data to the
main server. Set `AFL_IMPORT_POSTGIS_URL` (e.g.
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
5. Re-run `scripts/seed-examples --include osm-geocoder` so the new flow
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
