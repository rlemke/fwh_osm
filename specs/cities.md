# Cities by Zoom

**Namespace(s):** `osm.Cities`, `osm.Cities.workflows`, `osm.Cities.fanout`,
`osm.Cities.routes` ·
**FFL:** `handlers/cities/ffl/osmcities.ffl`, `osmcities_fanout.ffl`,
`osmcities_routes.ffl`, `osmcities_routes_fanout.ffl` ·
**Handlers:** `handlers/cities/cities_handlers.py` ·
**Tools:** `tools/_osm_tools/tier_cities.py`

## Overview

The `cities` namespace builds **zoom-aware city maps**: populated places assigned to four
disjoint population bands, each shown only at the zoom where it belongs — at zoom 0–3 you
see only cities >5M; zooming in adds >1M, then >500K, then >10K. The output is a
MapLibre + PMTiles viewer where the visible city set matches the current zoom, so a
continental map stays legible at every scale.

Two extensions build on the same tiering: `osm.Cities.routes` also draws the routes
*between* the top cities of each band over the region's freeway network (one colour per
band), and both come in a **monolithic** (one region / one PBF scan) and a **fan-out**
(per-subregion, parallel) form.

## How it works

The core is two pure facets plus a per-region pipeline:

1. **`osm.Population.AllPopulatedPlaces(cache, min_population)`** extracts every populated
   place with a parseable population ≥ the smallest tier threshold from the region's PBF.
2. **`TierCitiesByPopulation(input_path, zoom_a/min_pop_a … zoom_d/min_pop_d)`** assigns
   each city to the **highest** band it qualifies for — a 6M-pop city lands in tier_a
   (zoom 3) only, never the smaller tiers; cities below the lowest threshold are dropped.
   Output features carry `zoom`, `tier_min_population`, `name`, `country`, `place`,
   `population`, `lon`, `lat`, `bbox`. Handler → `_osm_tools.tier_cities.tier_cities`,
   the same code path the CLI exercises.
3. **`SplitTiers(input_path, zoom_a…zoom_d)`** splits the tiered GeoJSON into one
   FeatureCollection per zoom band, keyed by each feature's `zoom` property (empty bands
   still produce an empty GeoJSON — downstream tilers need a path).
4. **`osm.Tiles.BuildVectorTiles`** turns each band's GeoJSON into PMTiles with a
   per-layer `min_zoom` (0 / 4 / 7 / 9) equal to the first zoom the band appears at, and
   **`osm.viz.RenderTiledMap`** assembles the index.html + PMTiles archives.

`CitiesByZoom` (GeoJSON only) and `CitiesByZoomTiledMap` (tiled viewer) wrap 1→4 for one
region; each has a `...FromCache` inner facet so the cache step is separated from the
cache-dependent body (the domain's standard split).

The **routes** variants add: `osm.Source.PBF.ExtractRoads(road_class = "major")` →
`osm.Network.BuildNetwork` (built once), then per band `osm.Population.TopNByPopulation`
caps the waypoints and `osm.Network.RouteLayer` draws all-pairs lines over the network;
route + city tiles share one colour per band.

## Fan-out

Both fan-out workflows use the [fan-out pattern](fan-out-pattern.md): a facet holding an
`andThen foreach` over `ResolveRegions(expand = "subregions")`, yielding 1-element path
lists that the runtime merges.

- **`osm.Cities.fanout.CitiesByZoomTiledMapFanout`** — `ExtractPlacesAcrossSubregions`
  fans one download + `AllPopulatedPlaces` per subregion; the workflow then
  `MergeLayers` → tier → split → tile → render exactly as the monolith.
- **`osm.Cities.routes.CitiesAndRoutesByZoomFanout`** —
  `ExtractRoadsAndCitiesAcrossSubregions` fans *both* road and place extraction per
  subregion, returning two aggregated lists (`road_paths`, `city_paths`) — the
  "genomics fan-out/fan-in split" (FFL has no post-`foreach` aggregation block, so the
  foreach lives in a facet returning the lists). The workflow merges roads → builds the
  network once → merges cities → tiers → routes per band.

The fan-out unit is one **Geofabrik leaf** (US state / Canadian province / Mexico /
Greenland — 64 leaves for North America). A single parent name (`"north-america"`)
expands to all leaves; an explicit list targets specific subregions (leaf names pass
through unchanged). The monolithic sibling scans one ~19 GB continental PBF on a single
runner (~50+ min); the fan-out collapses the CPU-bound extraction to ≈ the slowest single
subregion when several `osm` runners are up.

**Decision rule** (from `osmcities_fanout.ffl`): `>= several osm runners → fan out per
subregion`; `1 osm runner → osm.Cities.workflows.CitiesByZoomTiledMap` (the foreach
iterations would serialise, and one continental scan avoids N download/merge overheads).

## Filtering & attributes

- **Population bands** (defaults): tier_a ≥ 5,000,000 (zoom 3), tier_b ≥ 1,000,000
  (zoom 6), tier_c ≥ 500,000 (zoom 8), tier_d ≥ 10,000 (zoom 10) — all parameterised.
- **Attributes read**: the `population` tag (parseable integer required — commas
  stripped) and the `place` tag; upstream `AllPopulatedPlaces` selects `place=*`
  populated places. Below-threshold cities are dropped; each city lands in exactly one
  band (disjoint tiers).
- Filter mechanism: a Python tiering predicate in `_osm_tools.tier_cities` over feature
  properties; the extraction filter (which OSM nodes are "populated places") is
  `osm.Population.AllPopulatedPlaces` upstream.

## External libraries / binaries

- **`osmium`** (indirect) — upstream `osm.Population.AllPopulatedPlaces` /
  `osm.Source.PBF.ExtractRoads` extract from the PBF (binary dependency).
- **`tippecanoe`** (indirect) — `osm.Tiles.BuildVectorTiles` builds the PMTiles archives
  (binary dependency; see the tiles feature).
- **In-process `osm.Network`** graph search for the routes variants — no routing daemon.
- The `cities_handlers.py` / `_osm_tools.tier_cities` code itself is **stdlib-only**
  (json/geojson streaming); no `shapely`/`pyproj` in the tiering path.
- **MapLibre GL + PMTiles** in the emitted viewer (client-side).

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `TierCitiesByPopulation(input_path, zoom_a/min_pop_a…)` | event (pure) | Assign each city to its highest population/zoom band |
| `SplitTiers(input_path, zoom_a…zoom_d)` | event (pure) | Split tiered GeoJSON into one file per band |
| `CitiesByZoom(region, …)` | workflow | One region → tiered cities GeoJSON |
| `CitiesByZoomTiledMap(region, …)` | workflow | One region → MapLibre+PMTiles zoom viewer |
| `CitiesByZoomTiledMapFanout(region_names, …)` | workflow | **Fan-out** per subregion → merged zoom viewer |
| `CitiesAndRoutesByZoom(region, …)` | workflow | One region → cities + inter-city routes, one colour/band |
| `CitiesAndRoutesByZoomFanout(region_names, …)` | workflow | **Fan-out** cities + routes across subregions |
| `ExtractPlacesAcrossSubregions` / `ExtractRoadsAndCitiesAcrossSubregions` | facet | Hold the `foreach`, return aggregated path list(s) |

`TierCitiesByPopulation` and `SplitTiers` are `Effect(kind="pure") Cost(tier="cheap")`.
Only two facets in the namespace need handlers; the rest is composition of population,
network, tiles, and viz facets.

## Cache / output

- **Cache namespace**: `osm-cities` (`resolve_output_dir("osm-cities")`). Tiered output
  `<stem>_tiered.geojson`; split output under `<stem>_split/`. Per-facet output-cache
  keyed on input path + the `(zoom, min_population)` tier tuple.
- **Output**: for the tiled workflows, a **directory** — `index.html` + four (or eight,
  with routes) **PMTiles** archives. It **MUST be served over HTTP**: MapLibre relies on
  HTTP Range fetches that `file://` does not support (`scripts/serve-tiled-map` or any
  static server). On the fleet, artifacts land in MinIO (`s3://afl-cache`).

## Gotchas & notes

- **Serve over HTTP, always.** PMTiles Range requests fail on `file://`; opening the
  index.html directly shows an empty map.
- **Disjoint tiers.** A city appears in exactly one band — the highest it qualifies for.
  Don't expect a 6M city to also show in the >1M layer.
- **Empty bands are intentional.** `SplitTiers` emits empty GeoJSONs for bands with no
  cities so `BuildVectorTiles` always has a path; the route variants likewise tolerate
  <2-city bands (an empty route layer).
- **Route caps matter.** `RouteLayer` is all-pairs O(n²); each band is first capped to
  the `route_cap` most-populous cities (`TopNByPopulation`), or a dense low-population
  band over a continent is millions of pairs.
- **One-colour-per-band is deliberate.** Tiers are distinguished by *which zoom they
  appear at*, not by colour — the cities-only map uses the same red for all four layers;
  the routes map pairs each band's dots and lines in one band colour.
- **Fan-out vs monolith is a runner-count decision**, not a correctness one — same params,
  same outputs. See the [fan-out pattern](fan-out-pattern.md).

## Related specs

- [fan-out-pattern](fan-out-pattern.md) — the per-subregion parallelism and the
  fan-out/fan-in facet split both routes/cities fan-outs use.
- [emergency-atlas](emergency-atlas.md) — reuses `place=city\|town` + population handling
  and `osm.Network` routing.
- [composed-workflows](composed-workflows.md) — `osm.Network.BuildNetwork` / `RouteLayer`
  and the tiles/viz facets these workflows compose.
- [planet-extraction](planet-extraction.md) — the region/subregion extracts consumed here.
