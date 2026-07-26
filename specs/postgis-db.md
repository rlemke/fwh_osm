# Database Backends — MongoDB import & PostGIS source

**Namespace(s):** `osm.db` (MongoDB import), `osm.Source.PostGIS` (SQL source adapter) ·
**FFL:** `handlers/db/ffl/osmdb.ffl`, `handlers/sources/ffl/osm_source_postgis.ffl` ·
**Handlers:** `handlers/db/import_handlers.py`, `handlers/db/osm_store.py`,
`handlers/sources/postgis_source.py`

## Overview

The domain has two database touch-points, and they sit on **opposite sides** of the
pipeline:

- **`osm.db`** is the **write/persist** side — it streams extractor GeoJSON output *into
  MongoDB* with 2dsphere spatial indexing, so OSM features become queryable geospatial
  documents.
- **`osm.Source.PostGIS`** is a **read/source-adapter** side — it *extracts* OSM features
  *out of* pre-imported PostGIS tables (`osm_nodes` / `osm_ways`) via SQL and writes
  GeoJSON for the same downstream analysis facets that consume PBF output.

> Naming caveat: the persistence namespace `osm.db` is **MongoDB**, not PostGIS. The
> PostGIS story in this repo is the *source adapter* below. The PostGIS **import** into
> `osm_nodes`/`osm_ways` and the osm2pgsql-compatible `planet_osm_*` views are provided by
> the **framework** (`fw db import-pg`, the MCP `afl_postgis_query` tool), not by this
> package — this package only *queries* those tables.

## How it works

### `osm.db` — MongoDB geospatial import

`ImportGeoJSON(output_path, category, region, feature_count)` →
`import_handlers._handler` → `osm_store.import_geojson`:

1. Slugify the region (`"North Carolina"` → `north-carolina`) and build a compound
   `dataset_key` (`osm.<category>.<region-slug>`, e.g. `osm.parks.alabama`).
2. Stream the GeoJSON with `iter_geojson_features()` so multi-GB files never load into
   memory, and bulk-upsert into the `osm_features` collection with `ReplaceOne` on a
   compound unique key `(dataset_key, feature_key)` — **idempotent re-imports**
   (`feature_key` is the feature's `osm_id`, else `id`, else its index). Batches of 1000
   with heartbeats between batches.
3. `ensure_indexes` creates the unique upsert index, a **`2dsphere`** index on `geometry`
   (sparse), and a `dataset_key` index. A metadata doc is upserted into
   `osm_features_meta`. Returns `{imported_count, dataset_key, collection}`.

Collection lives in the `FW_EXAMPLES_DATABASE` database (default `facetwork_examples`),
keeping OSM data isolated from the Facetwork runtime DB. Connection:
`FW_MONGODB_URL` (default `mongodb://afl-mongodb:27017`).

### `osm.Source.PostGIS` — SQL source adapter

Each `Extract*` facet → `postgis_source._extract_*`:

1. Resolve the connection (`PostGISSource.postgis_url`, default `FW_POSTGIS_URL` /
   `postgresql://afl_osm:afl_osm_2024@afl-postgres:5432/osm`) and `region`.
2. Build a parameterised SQL `SELECT osm_id, region, tags, ST_AsGeoJSON(geom) AS geometry
   FROM osm_nodes|osm_ways WHERE region = %s AND <tag predicate>` and execute it through a
   **server-side named cursor** (`itersize = 5000`) so large result sets stream.
3. Stream rows into a GeoJSON FeatureCollection on disk: `ST_AsGeoJSON` geometry +
   `tags` JSONB flattened into `properties` (plus `osm_id`, `region`). Returns the
   category's typed result (output_path, feature_count, …) matching the PBF adapter's
   schema, so PostGIS is a drop-in **source** in any workflow.

`osm_nodes` carry point features (amenities, population, POIs); `osm_ways` carry
line/polygon features (routes, roads, parks, buildings, boundaries). Both have a `tags`
JSONB column and a PostGIS `geom`.

## Fan-out

**Single-task per import/extract — no `foreach` fan-out.** Both are atomic streaming
operations: one MongoDB import per GeoJSON file, one SQL query per `(region, category)`.
MongoDB upserts are idempotent (safe to retry); a large PostGIS extract streams rather
than fanning out. Parallelism, when wanted, comes from the *caller* fanning the workflow
per region (see [fan-out pattern](fan-out-pattern.md)), each region a separate task.

## Filtering & attributes

The PostGIS adapter filters entirely in SQL over the `tags` JSONB. Concrete predicates
(mirroring the PBF filters):

- **Routes** (`osm_ways`): `tags->>'route' = ANY(...)` for bicycle/hiking/train/bus, else
  `tags ? 'route'`.
- **Amenities** (`osm_nodes`): `tags->>'amenity' = ANY(<category list>)` (food, shopping,
  services, healthcare, education, entertainment, transport), else `tags ? 'amenity'`.
- **Roads** (`osm_ways`): `tags->>'highway' = ANY(...)` by class (motorway/primary/…/
  `major` = motorway+primary+secondary and `_link`s), else `tags ? 'highway'`.
- **Parks** (`osm_ways`): `boundary='national_park'`/`protect_class`/`leisure=
  'nature_reserve'`.
- **Buildings** (`osm_ways`): `tags->>'building' = ANY(...)` by type, else
  `tags ? 'building'`.
- **Boundaries** (`osm_ways`): `boundary='administrative' AND admin_level = %s`
  (country=2, state=4, county=6, city=8), or `natural` for lake/forest/park.
- **Population** (`osm_nodes`): `tags ? 'population'`, optional `place = %s`,
  `(tags->>'population')::bigint >= %s`.
- **POIs** (`osm_nodes`): any of `amenity`/`shop`/`tourism`/`leisure`/`historic`/`place`.

The MongoDB side does **no** filtering — it imports whatever GeoJSON it is handed
(features are already filtered by the extractor that produced the file).

## External libraries / binaries

- **`pymongo`** (`MongoClient`, `ReplaceOne`) — the MongoDB import (pip). 2dsphere index
  is a MongoDB server feature.
- **`psycopg2`** — the PostGIS adapter (pip); probed at import (`HAS_PSYCOPG2`), and every
  `Extract*` returns an empty result if it is missing (so a lite runner degrades rather
  than errors). Requires a **PostGIS-enabled PostgreSQL** with the `ST_AsGeoJSON` /
  geometry functions server-side.
- No `osmium` on either path — both work in lite agents (the import handler notes "no
  pyosmium dependency").

## Facets & workflows

| Facet | Kind | Purpose |
|---|---|---|
| `osm.db.ImportGeoJSON(output_path, category, region, feature_count)` | event (external, expensive) | Stream GeoJSON → MongoDB `osm_features`, 2dsphere-indexed |
| `osm.Source.PostGIS.ExtractRoutes(source, route_type, network, include_infrastructure)` | event (external, moderate) | Routes by transport type from PostGIS |
| `osm.Source.PostGIS.ExtractAmenities(source, category)` | event (external) | Amenities by category |
| `osm.Source.PostGIS.ExtractRoads(source, road_class)` | event (external) | Roads by classification |
| `osm.Source.PostGIS.ExtractParks(source, park_type, protect_classes)` | event (external) | Parks / protected areas |
| `osm.Source.PostGIS.ExtractBuildings(source, building_type)` | event (external) | Building footprints |
| `osm.Source.PostGIS.ExtractBoundaries(source, boundary_type, admin_level)` | event (external) | Admin + natural boundaries |
| `osm.Source.PostGIS.ExtractPopulation(source, place_type, min_population)` | event (external) | Populated places |
| `osm.Source.PostGIS.ExtractPOIs(source)` | event (external) | POIs (amenity/shop/tourism/…) → `OSMCache` |

Schemas: `osm.db.ImportResult`, `osm.Source.PostGIS.PostGISSource`. The PostGIS facets
return the *same* per-category result schemas as the PBF adapter (`osm.Routes.RouteFeatures`,
`osm.Amenities.AmenityFeatures`, …), which is what makes it a swappable source.

## Cache / output

- **MongoDB**: features persist in `osm_features` (DB `facetwork_examples`), not a file
  cache; `osm_features_meta` holds per-dataset counts. Re-import overwrites by
  `(dataset_key, feature_key)`.
- **PostGIS adapter**: writes GeoJSON to
  `<FW_LOCAL_OUTPUT_DIR>/postgis-extract/<category>/<region>_<subcat>.geojson` and caches
  the result meta via `cached_result`/`save_result_meta` keyed on `(postgis_url, region)`
  + the dynamic params. Output format is always `GeoJSON`.

## Gotchas & notes

- **`osm.db` is MongoDB.** Don't expect PostGIS SQL there — the `db/` handlers write
  documents + a 2dsphere index. PostGIS lives in `sources/`.
- **Import errors are swallowed to a zero result.** `import_handlers._handler` catches
  exceptions and returns `{"imported_count": 0, ...}` with an error step-log rather than
  failing the step — a missing/absent file logs a warning and returns zero (revisit if a
  hard-fail-on-import is wanted).
- **PostGIS connection option quirk.** `_connect` passes
  `options="-c default_transaction_read_only=off"` and `autocommit=False`; the read-only
  *guard* for ad-hoc queries is the framework's `afl_postgis_query` tool (keyword filter +
  `default_transaction_read_only=on`), not this adapter.
- **The PostGIS tables must already exist.** This package queries `osm_nodes`/`osm_ways`;
  the import that populates them (and the osm2pgsql-compatible `planet_osm_point/_line/
  _roads` views) is a framework/infra concern — see the framework CLAUDE.md and the
  `postgis-import` skill.
- **Idempotent MongoDB re-imports** rely on a stable `feature_key` — features without an
  `osm_id`/`id` fall back to a sequential index, so re-importing a reordered file can
  create duplicates. Prefer files with `osm_id`.

## Related specs

- [voting](voting.md) — Census district GeoJSON that can be joined against OSM boundaries.
- [composed-workflows](composed-workflows.md) — how a source adapter (PBF or PostGIS) and
  analysis facets compose into a workflow.
- [planet-extraction](planet-extraction.md) — the PBF source the PostGIS tables are an
  alternative to.
