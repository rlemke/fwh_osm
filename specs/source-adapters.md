# Source Adapters — one namespace per input format, one output schema

**Namespace(s):** `osm.Source.PBF` · `osm.Source.PostGIS` · `osm.Source.GeoJSON` ·
`osm.Source.Overture` · `osm.convert` · `osm.workflows.sourced` ·
**FFL:** `src/osm_geocoder/handlers/sources/ffl/*.ffl`
(`osm_source_pbf.ffl`, `osm_source_postgis.ffl`, `osm_source_geojson.ffl`,
`osm_source_overture.ffl`, `osm_convert_workflows.ffl`, `osm_source_workflows.ffl`) ·
**Handlers:** `src/osm_geocoder/handlers/sources/{pbf,postgis,geojson,overture}_source.py`,
`source_handlers.py`

## Overview

The source-adapter pattern **decouples data extraction from analysis**. Four
adapters — PBF files, a PostGIS database, existing GeoJSON, and Overture Maps
GeoParquet — each live in their own `osm.Source.<Format>` namespace, and every one
produces the **same category-specific output schemas** (`osm.Routes.RouteFeatures`,
`osm.Amenities.AmenityFeatures`, `osm.Roads.RoadFeatures`, …). Because the output
shape is identical, every downstream analysis facet (statistics, filtering,
rendering) is **source-agnostic**: only the extraction layer changes, and a
workflow can swap `ExtractRoutes` from PBF to PostGIS to Overture without touching
anything downstream.

This is the pattern the framework CLAUDE.md points at as the reference for building
multi-source domain pipelines: one source namespace per input format, all emitting
the same schema.

## How it works

Each adapter exposes the same verb set — `ExtractRoutes`, `ExtractAmenities`,
`ExtractRoads`, `ExtractParks`, `ExtractBuildings`, `ExtractBoundaries`,
`ExtractPopulation`, `ExtractPOIs` (GeoJSON names them `Load*`) — differing only in
the **input handle** and the **extraction mechanism**:

- **PBF** (`pbf_source.py`) — takes an `OSMCache`. Delegates each category to the
  existing category-specific extractors (`routes.route_extractor.extract_routes`,
  `amenities.amenity_extractor`, `roads.road_extractor`, `parks.park_extractor`,
  `buildings.building_extractor`, …) which scan the `.osm.pbf` via **pyosmium** and
  write GeoJSON. `_require_osmium` fails the task loudly if pyosmium isn't
  importable (so it retries on an osmium-capable runner) rather than returning
  empties.
- **PostGIS** (`postgis_source.py`) — takes a `PostGISSource` (`postgis_url`,
  `region`). Runs read-only SQL against the imported `osm_nodes` / `osm_ways`
  tables, filtering on the `tags` JSONB and emitting geometry via `ST_AsGeoJSON`,
  streamed through a server-side psycopg2 cursor into a GeoJSON file.
- **GeoJSON** (`geojson_source.py`) — takes an `input_path`. Streams an existing
  GeoJSON FeatureCollection, applies the optional category/tag filter, and writes a
  filtered copy. Pure I/O — for re-processing prior extracts or third-party GeoJSON.
- **Overture** (`overture_source.py`) — takes an `OvertureSource`
  (`theme/type/release` + a bbox window). Streams remote cloud-hosted GeoParquet
  and projects each Overture row into the same GeoJSON-shaped record, then maps it
  into the unified `osm.*` schemas. The remote read is isolated behind a single
  swappable `_read_overture_records` seam so the schema-mapping logic is fully
  testable offline.

All four normalize their result into the standard schema dict
(`{output_path, feature_count, …}`) that the analysis facets consume. Beyond the
per-category verbs, PBF adds two whole-region converters:

- **`ExtractCategory(cache, category)`** — the uniform, cached single-category
  primitive. Warms cheap point categories (amenities, population) in one cached
  osmium pass; heavier line/area families get their own cached pass on demand.
  Preferred over filtering the full PBF.
- **`ToGeoJson(cache, format, max_pbf_mb)`** — converts an **entire** region PBF to
  GeoJSON via `osmium export` (every feature, not one category), writing to the
  configured storage (MinIO under the `geojson` cache type on `FW_STORAGE=s3`).
  Idempotent on source SHA. `max_pbf_mb` skips over-limit regions cleanly.

## Fan-out

The extract facets are per-region single tasks; fan-out is driven by the workflows
that compose them. `osm.convert.ConvertAllRegionsToGeoJson`
(`osm_convert_workflows.ffl`) is the worked example: `ListCachedRegions` enumerates
the cache, then `andThen foreach r in $.regions` spawns **one `ToGeoJson` task per
region** across the fleet, with the list-typed yield aggregating every output path.
`osm_source_workflows.ffl` otherwise demonstrates the *source-swap* axis rather than
fan-out — the same analysis pipeline written three times (`BicycleRoutesPBF` /
`BicycleRoutesPostGIS` / `BicycleRoutesGeoJSON`) to show only the extraction layer
changes. `RoadsAndParksPostGIS` fans out two concurrent same-source extracts into a
layered map.

## Filtering & attributes

Filtering is by OSM category and happens per adapter, over the same tag vocabulary:

- **Amenities** — `amenity=*` (category → a set of amenity values, e.g. the
  `healthcare` category maps to a hospital/clinic/… value list; PostGIS filters
  `WHERE tags->>'amenity' = ANY(%s)`).
- **Roads** — `highway=*` (road_class → a set of highway values;
  `tags->>'highway' = ANY(%s)`, or `tags ? 'highway'` for all).
- **Parks** — `boundary=national_park`/`protected_area`, `leisure=nature_reserve`,
  and `protect_class` (`protect_classes` param filters `tags->>'protect_class'`).
- **Boundaries** — `boundary=administrative` + `admin_level` (`admin_level=4` for
  states, `=2` for countries).
- **Population** — `place=*` (city/town/…) with a `min_population` threshold.
- **Routes** — transport `route_type` (bicycle/hiking/…) + `network`, optionally
  including route infrastructure.
- **Buildings** — `building=*` by `building_type`.

Mechanism differs by source: PBF filters inside the pyosmium extractors; PostGIS
filters in the SQL `WHERE` over `tags` JSONB; GeoJSON applies a Python predicate
over each feature's `properties`; Overture filters the projected row properties.

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — PBF extraction; the category extractors run in a
  blocking C++ scan loop. PBF facets register with `timeout_ms=0` (fall back to the
  global execution timeout) because a continental scan far exceeds the default
  handler timeout.
- **`psycopg2`** (pip) — PostGIS source; read-only connection, server-side cursor
  streaming, `ST_AsGeoJSON` for geometry.
- **`pyarrow`** (or **`duckdb`**) + **`shapely`** (pip, `[overture]` extra) —
  Overture GeoParquet read + WKB→GeoJSON geometry. Missing deps raise
  `OvertureDependencyError` (never a silent empty). Overture facets also register
  `timeout_ms=0`.
- **stdlib** (`json`) for GeoJSON streaming/staging — the GeoJSON adapter has no
  third-party dependency.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.Source.PBF.Extract{Routes,Amenities,Roads,Parks,Buildings,Boundaries,Population,POIs}` | event | Category extract from a PBF `OSMCache`. `Effect(external)`, `Cost(expensive)` |
| `osm.Source.PBF.ExtractCategory(cache, category)` | event | Uniform cached single-category extract. `Timeout(120min)`, `Effect(external)` |
| `osm.Source.PBF.ToGeoJson(cache, format, max_pbf_mb)` | event | Whole-region PBF → GeoJSON (`osmium export`), idempotent. `Timeout(180min)`, `Effect(external)` |
| `osm.Source.PostGIS.Extract*` | event | Same verbs over `osm_nodes`/`osm_ways` via SQL. `Effect(external)`, `Cost(moderate)` |
| `osm.Source.GeoJSON.Load*` | event | Load + filter an existing GeoJSON file. `Effect(io)`, `Cost(cheap)` |
| `osm.Source.Overture.Extract*` | event | Same verbs over remote GeoParquet. `Effect(external)`, `Cost(expensive)` |
| `osm.convert.ConvertRegionToGeoJson(region)` | workflow | Resolve → Download → `ToGeoJson` one region |
| `osm.convert.ConvertAllRegionsToGeoJson(max_pbf_mb)` | workflow | Fan-out: one `ToGeoJson` per cached region |
| `osm.workflows.sourced.*` (e.g. `BicycleRoutesPBF/PostGIS/GeoJSON`, `RoadsAndParksPostGIS`) | workflow | Same analysis pipeline across different sources |

Source-specific input schemas: `osm.Source.PostGIS.PostGISSource`
(`postgis_url`, `region`) and `osm.Source.Overture.OvertureSource`
(`theme`, `release`, `min/max_lon/lat`, `region`).

## Cache / output

- **PBF category extracts** cache under their category namespaces (e.g.
  `cache/osm/water/...`, `cache/osm/amenities/...`) via the shared `output_cache`;
  `ToGeoJson` writes under the `geojson` cache type.
- **Output** is GeoJSON in every case (an `output_path` + `feature_count`), so the
  analysis facets consume one shape regardless of source. On `FW_STORAGE=s3`,
  outputs go to MinIO; adapters localize the source PBF first when it's an
  `s3://` URI. PostGIS/GeoJSON/Overture outputs are staged locally then finalized.

## Gotchas & notes

- **Fail-loud on missing engine.** PBF handlers `_require_osmium` and Overture
  raises `OvertureDependencyError` — a task fails (and retries on a capable runner)
  rather than returning an empty result that would be mistaken for "no features".
- **`timeout_ms=0` for PBF/Overture.** Full-region scans and remote GeoParquet
  streams block with sparse heartbeats; the short per-handler timeout would
  dead-letter them, so they rely on the global execution timeout.
- **PostGIS requires an import.** The PostGIS adapter reads `osm_nodes`/`osm_ways`,
  which must have been imported first; the default `postgis_url` in the sourced
  workflows points at `afl-postgres`.
- **`ToGeoJson` memory.** The whole-region `osmium export` holds the node-location
  index in (disk-backed) memory; on a memory-constrained host the largest extracts
  OOM or seek-thrash, so `max_pbf_mb` (or `FW_OSM_MAX_PBF_MB`) gates them out.
- **Overture is a distinct data source**, not OSM — it shares the *schema* so it's
  interchangeable downstream, but the underlying data and tag semantics differ.

## Related specs

- [cache-and-download](cache-and-download.md) — produces the `OSMCache` the PBF
  adapter reads.
- [clip](clip.md) — clip a PBF to a metro bbox first, then feed the clipped
  `OSMCache` into `ExtractCategory` for cheap sub-region queries.
- [planet-extraction](planet-extraction.md) — the extracts the PBF source runs over.
