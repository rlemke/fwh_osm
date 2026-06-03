# Cities by Zoom (and Routes)

Tier populated places into disjoint **population bands** that surface at
different **zoom levels**, render them as a MapLibre + PMTiles map where each
band appears only from its threshold zoom, and (optionally) draw the routes
between each band's cities — **one colour per band**.

The progressive reveal: megacities at low zoom, smaller cities filling in as
you zoom in. With routes, the busy corridors between the largest metros show
first, then denser networks appear per band.

## Population → zoom bands

Each city lands in the **highest** band it qualifies for (a 6 M-pop city is in
the 5M band only, never the smaller ones). Defaults:

| Band | Min population | Appears from zoom | Layer name | Colour |
|------|----------------|-------------------|------------|--------|
| A | ≥ 5,000,000 | 0–3 | `cities_5M` / `routes_5M` | `#e41a1c` red |
| B | ≥ 1,000,000 | 4 | `cities_1M` / `routes_1M` | `#ff7f00` orange |
| C | ≥ 500,000 | 7 | `cities_500K` / `routes_500K` | `#4daf4a` green |
| D | ≥ 10,000 | 9 | `cities_10K` / `routes_10K` | `#377eb8` blue |

Zoom-gating is done by tippecanoe's per-layer `min_zoom` in `BuildVectorTiles`
(features simply don't exist in tiles below their band's zoom), not by style.

## Event facets

### TierCitiesByPopulation
Assign each feature of a populated-places GeoJSON to the highest of four
disjoint zoom bands by population; records `zoom`, `tier_min_population`,
`name`, `country`, `population`, `lon`/`lat`, and a population-scaled `bbox`.
Pure / cheap.

```afl
event facet TierCitiesByPopulation(
    input_path: String,
    zoom_a: Long = 3,  min_pop_a: Long = 5000000,
    zoom_b: Long = 6,  min_pop_b: Long = 1000000,
    zoom_c: Long = 8,  min_pop_c: Long = 500000,
    zoom_d: Long = 10, min_pop_d: Long = 10000
) => (result: TieredCitiesResult)
```

### SplitTiers
Split a tiered GeoJSON into one FeatureCollection per zoom band (empty bands
still produce a file so downstream tilers don't trip). Pure / cheap.

```afl
event facet SplitTiers(input_path: String,
    zoom_a: Long = 3, zoom_b: Long = 6, zoom_c: Long = 8, zoom_d: Long = 10
) => (result: SplitTiersResult)
```

> Routing waypoints are bounded with **`osm.Population.TopNByPopulation`**
> (keep a band's N most-populous cities) so the downstream all-pairs
> `osm.Network.RouteLayer` stays tractable — without it, routing a dense band
> over a continent is millions of pairs.

## Workflows

| Workflow | Cities | Routes | Extraction | Use when |
|----------|--------|--------|------------|----------|
| `osm.Cities.workflows.CitiesByZoom` | ✓ | — | single region | just the tiered GeoJSON |
| `osm.Cities.workflows.CitiesByZoomTiledMap` | ✓ | — | single region | tiled cities map, one region |
| `osm.Cities.fanout.CitiesByZoomTiledMapFanout` | ✓ | — | **per-subregion foreach** | tiled cities map, continental |
| `osm.Cities.routes.CitiesAndRoutesByZoom` | ✓ | ✓ | single region | cities + routes, one region |
| `osm.Cities.routes.CitiesAndRoutesByZoomFanout` | ✓ | ✓ | **per-subregion foreach** | cities + routes, continental |

### Cities + routes pipeline

```
(extract) → TierCitiesByPopulation → SplitTiers            # 4 city bands
(extract roads) → osm.Network.BuildNetwork                 # freeway graph, once
per band: TopNByPopulation(n=route_cap) → osm.Network.RouteLayer
BuildVectorTiles ×8 (4 routes + 4 cities)
osm.viz.RenderTiledMap                                     # routes under dots
```

`RouteLayer` is all-pairs (O(n²) lines), so each band is capped to the
`route_cap` (default 50, 40 for continental) most-populous cities. Bands with
<2 cities yield an empty route layer (RouteLayer tolerates sparse input), so
the workflow is correct for any region.

## Monolithic vs fan-out — the decision rule

The `*Fanout` variants `ResolveRegions` → `andThen foreach` the per-subregion
extraction across the runner fleet (one task per subregion), then a linear
block merges → builds the network → tiers → routes → renders. The monolithic
variants extract from a single PBF in one pass.

| Setup | Choose |
|-------|--------|
| 1 osm runner, or a small region (state/country) | monolithic (`CitiesByZoomTiledMap`, `CitiesAndRoutesByZoom`) |
| several osm runners + a continent | fan-out (`*Fanout`) |

A single continental PBF (~19 GB for North America) is impractical to scan in
one pass; the fan-out splits it into ~64 small per-subregion scans that run in
parallel. Only the extraction fans out — tiering/tiling/render is an inherent
single merge step.

## Running and serving

```bash
# California cities + routes (fast, runnable on one runner):
scripts/ffl-run <bundle>.ffl --workflow osm.Cities.routes.CitiesAndRoutesByZoom

# North America cities + routes (fan-out across the fleet):
scripts/ffl-run <bundle>.ffl --workflow osm.Cities.routes.CitiesAndRoutesByZoomFanout

# The output is a directory of index.html + PMTiles archives; serve it over
# HTTP (PMTiles needs Range requests):
scripts/serve-tiled-map 8765 <output_map_dir>
```

### Viewer legibility

`osm.viz.RenderTiledMap` renders on a **dark, no-label CARTO backdrop** by
default so the band-coloured dots and routes pop — a full-colour OSM basemap
competes with the thematic overlays (its own coloured highways camouflage the
motorway routes). Routes are drawn with a casing beneath every city dot, dot
radii and line widths interpolate with zoom, and a **legend** (bottom-right)
names each band layer. Pick a different backdrop with the `basemap` param:
`"dark"` (default), `"light"` (CARTO Positron), `"osm"` (full-colour
OpenStreetMap), or `"none"` (flat dark, no tiles — offline-friendly).

> **Runner image requirements:** tile building needs both `tippecanoe`
> (mbtiles) and the `pmtiles` CLI (mbtiles → pmtiles) on the runner. Both are
> baked into the `example-runner` image — if a tiled map renders dots but no
> lines, a runner is missing `pmtiles` and `BuildVectorTiles` fell back to
> `.mbtiles` (which the browser's PMTiles client can't read).
