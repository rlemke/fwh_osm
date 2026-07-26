# Composed Workflows

**Namespace(s):** `osm.Combined` (single-pass scan), `osm.workflows` (composition
patterns), `examples.routes` (route+map examples) ·
**FFL:** `handlers/combined/ffl/osmcombined.ffl`,
`handlers/composed_workflows/ffl/osmworkflows_composed.ffl`,
`handlers/composed_workflows/ffl/example_routes_visualization.ffl` ·
**Handlers:** `handlers/combined/combined_handlers.py`, `handlers/combined/combined_handler.py`

## Overview

This is the **composition layer**: high-level workflows that chain lower-level facets
(cache → extract → filter → statistics → visualize) into end-to-end pipelines a user can
run by name. It is the *linear* counterpart to the [fan-out pattern](fan-out-pattern.md) —
the same facets, wired sequentially rather than fanned across the fleet.

Two things live here:

- **`osm.Combined.CombinedScan`** — a performance primitive that scans a PBF **once** and
  extracts many categories in parallel (instead of one PBF pass per category), the engine
  most composed workflows sit on.
- **`osm.workflows` + `examples.routes`** — ~20 worked pipelines demonstrating the
  facet-composition idiom, from a three-stage cache→extract→visualize to a full
  five-stage cache→extract→filter→statistics→visualize.

## How it works

### The composition idiom

Every workflow follows the domain's **two-facet split**: a thin outer `workflow` that
takes a `region` and calls `osm.ops.CacheRegion` (or `CacheRegion`), then delegates to a
`...FromCache` inner facet holding the cache-dependent body. This separates the
(cacheable, region-keyed) download from the analysis, so re-running with a warm cache
skips the download and the inner facet is reusable across workflows.

Example (`VisualizeBicycleRoutes`):

```
workflow VisualizeBicycleRoutes(region) => (map_path, route_count) andThen {
    cache = osm.ops.CacheRegion(region = $.region)
    f = VisualizeBicycleRoutesFromCache(cache = cache.cache)
    yield VisualizeBicycleRoutes(map_path = f.map_path, route_count = f.route_count)
}
facet VisualizeBicycleRoutesFromCache(cache: OSMCache) => (map_path, route_count) andThen {
    routes = osm.Routes.BicycleRoutes(cache = $.cache, include_infrastructure = true)
    map    = osm.viz.RenderMap(geojson_path = routes.result.output_path, title = ..., color = ...)
    yield VisualizeBicycleRoutesFromCache(map_path = map.result.output_path, route_count = routes.result.feature_count)
}
```

The 15 patterns in `osmworkflows_composed.ffl` scale this up: extract→statistics
(`AnalyzeParks`), extract→filter→visualize (`LargeCitiesMap`), parallel multi-extraction
→ aggregated stats (`TransportOverview`, `RegionalAnalysis`), quality-validation
pipelines (`ValidateAndSummarize`, `OsmoseQualityCheck`), GTFS transit analysis
(`TransitAnalysis`, `TransitAccessibility`), and a routing-graph zoom builder
(`RoadZoomBuilder`). `example_routes_visualization.ffl` is a compact family of
route-type → coloured-map examples (bicycle/hiking/train/bus/public-transport, with/
without stats, national cycle network).

### CombinedScan — single-pass multi-category extraction

`CombinedScan(cache, categories)` scans the PBF once and extracts all requested
categories (amenities, population, roads, routes, parks, buildings, boundaries) in one
pass, returning a JSON `results` manifest of per-category `{output_path, feature_count}`
plus `total_features`, `scan_duration`, `category_count`. `ExtractCategoryResult(results,
category)` then pulls one category's path out of the manifest. This is the reuse win: a
workflow needing roads *and* population *and* amenities pays one PBF read, not three. It
carries `with Timeout(minutes = 120)` because a large PBF single pass is long-running.

## Fan-out

**These workflows are linear (single-region) — no `foreach` fan-out.** They are the
"single-task" side of the [fan-out decision rule](fan-out-pattern.md): a workflow scans
one region's PBF on the runner that claims it. Within a workflow, independent steps
(e.g. `TransportOverview`'s four route extractions, `GetStateVotingBoundaries`' three
downloads) run as parallel *steps*, but there is no per-leaf task fan-out. To run a
composed pipeline continent-wide, fan the *workflow* per subregion at the caller, or use
the dedicated fan-out workflows in the cities/heatmap/emergency namespaces.

`CombinedScan`'s "extracts all categories in parallel" is intra-task parallelism (one
task, many categories), not a fleet fan-out.

## Filtering & attributes

Filtering is delegated to the composed facets, each keyed on concrete OSM tags:

- **Routes** — `route=bicycle|hiking|train|bus`, `network=ncn` (national cycle network).
- **Parks** — `boundary=national_park`/`protected_area`, `leisure=nature_reserve`,
  `protect_class`.
- **Population/cities** — `place=city|town|village` + `population` tag, thresholded.
- **Boundaries** — `boundary=administrative` + `admin_level`.
- **Buildings** — `building=*` by type.
- `CombinedScan` categories map to these same tag families in one pass.

`CombinedScan` guards its cache against a **storage-backend change**: a manifest records
per-category `output_path`s nested in its `results` JSON; if those were written under a
different backend than the one now in effect (e.g. local paths cached before
`FW_STORAGE=s3`), the guard forces a re-scan rather than handing downstream a dead/
unreadable path.

## External libraries / binaries

- **`osmium`** (osmium-tool binary + `pyosmium`) — `combined_handler.py` lazy-imports
  `osmium` and gates on `HAS_OSMIUM`; the single-pass scan is an osmium pass. Binary
  dependency.
- **`shapely`** (`shapely.wkb`, `shapely.geometry.mapping`) — geometry assembly in the
  combined scan plugins.
- **`osmium.filter.KeyFilter`** — tag pre-filtering inside the pass.
- The composed workflows also pull in the tiles (`tippecanoe`), network (in-process graph),
  GraphHopper (Java, for `RoadZoomBuilder`), and GTFS facets by composition — each
  documented in its own feature.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.Combined.CombinedScan(cache, categories)` | event (external, expensive, Timeout 120m) | Single PBF pass → all requested categories |
| `osm.Combined.ExtractCategoryResult(results, category)` | event (external) | Pull one category's path from a scan manifest |
| `osm.workflows.VisualizeBicycleRoutes` / `AnalyzeParks` / `LargeCitiesMap` / `TransportOverview` / `NationalParksAnalysis` / `CityAnalysis` / `TransportMap` / `StateBoundariesWithStats` / `DiscoverCitiesAndTowns` / `RegionalAnalysis` | workflow | Cache→extract→(filter)→(stats)→visualize patterns 1–10 |
| `osm.workflows.ValidateAndSummarize` / `OsmoseQualityCheck` | workflow | Data-quality validation pipelines |
| `osm.workflows.TransitAnalysis` / `TransitAccessibility` | workflow | GTFS feed → stops/routes → statistics / accessibility gaps |
| `osm.workflows.RoadZoomBuilder` | workflow | Build routing graph → per-edge min-zoom layers |
| `examples.routes.*` | workflow | Route-type → coloured-map examples (bicycle/hiking/train/bus/PT) |

Each pattern pairs a `workflow` (region entry) with a `...FromCache` `facet` (the body).
`CombinedScan`/`ExtractCategoryResult` are the only facets here needing a dedicated
handler; everything else is composition of routes/parks/population/boundaries/viz/tiles/
network facets.

## Cache / output

- **Region cache**: `osm.ops.CacheRegion` → the shared `osm/pbf` download cache.
- **CombinedScan output**: per-category GeoJSONs under the `osm-combined` output dir
  (`resolve_output_dir("osm-combined")`), referenced by the manifest; backend-aware
  (local or `s3://afl-cache`).
- **Workflow outputs**: interactive HTML maps (`osm.viz.RenderMap`), statistics scalars,
  or (for `RoadZoomBuilder`) CSV + metrics files. Several patterns default `output_dir`
  to `/Volumes/afl_data/output/osm` — override on the fleet where durable storage is
  MinIO.

## Gotchas & notes

- **Prefer `CombinedScan` over N single extractions** when a workflow needs several
  categories from the same region — it pays one PBF read. The manifest's backend guard
  means a stale local-path cache won't silently poison an s3 run.
- **The `...FromCache` split is a requirement, not a style choice** — passing local file
  paths straight between workflow steps breaks on the fleet's per-host scratch disk; the
  cache facet hands downstream a portable `OSMCache`/URI.
- **`osmium` must be importable** for `CombinedScan` (`HAS_OSMIUM`) — a lite runner
  without pyosmium cannot serve it.
- **Default `output_dir`s are local paths** in several older patterns; on a fleet host
  point them at MinIO or they write to a non-durable local disk.
- **Long single-pass timeout.** `CombinedScan` allows 120 minutes; for continent-scale
  data prefer the per-subregion fan-out workflows instead of one giant scan.

## Related specs

- [fan-out-pattern](fan-out-pattern.md) — the parallel counterpart; when to fan a
  composed workflow across the fleet vs run it single-region.
- [cities](cities.md) / [emergency-atlas](emergency-atlas.md) — fan-out pipelines built
  from the same lower-level facets composed here.
- [postgis-db](postgis-db.md) — the PostGIS source adapter is a drop-in alternative to
  the PBF source these workflows cache-and-scan.
- [planet-extraction](planet-extraction.md) — supplies the region PBFs `CacheRegion`
  downloads.
