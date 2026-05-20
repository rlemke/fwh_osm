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

Each source provides 8 extraction facets (routes, amenities, roads, parks,
buildings, boundaries, population, POIs) that produce category-specific
output schemas (`RouteFeatures`, `AmenityFeatures`, …).

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
