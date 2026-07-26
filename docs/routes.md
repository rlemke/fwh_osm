# Routes — transit, trails, elevation, and city-to-city routing

**Namespace(s):** `osm.Routes`, `osm.Transit.GTFS`, `osm.Elevation`
(+ `osm.Elevation.Workflows` / `osm.Elevation.RegionWorkflows` / `osm.ElevationMap`),
`osm.Routing.ComputePairwiseRoutes` ·
**FFL:** `src/osm_geocoder/handlers/routes/ffl/{osmroutes,osmgtfs,osmcityrouting,osmelevation*}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/routes/{route_extractor,route_handlers,gtfs_extractor,gtfs_handlers,elevation_handlers,routing_handlers}.py` ·
**Tests:** `src/osm_geocoder/handlers/routes/tests/`

## Overview

The `routes` handler group covers **linear/network features that aren't the road
graph itself** — the things you route *on* or *along*: transport-mode route
extraction, GTFS transit feeds, elevation enrichment, and a simple all-pairs
city-routing primitive. It is a cluster of four loosely related capabilities that all
produce GeoJSON (or GeoJSON-adjacent) outputs from an OSM cache or an external feed:

- **`osm.Routes`** — extract bicycle / hiking / train / bus route networks + their
  infrastructure from a PBF.
- **`osm.Transit.GTFS`** — parse GTFS static feeds (stops, routes, frequencies) and
  cross-analyze them against OSM features (accessibility, coverage gaps).
- **`osm.Elevation`** — enrich route geometry with DEM elevation and filter by
  elevation criteria; compose into elevation maps.
- **`osm.Routing.ComputePairwiseRoutes`** — an all-pairs shortest-path primitive over
  a routing topology (the pure city-routing helper).

## How it works

**Route extraction** (`route_extractor.py`) is a **pyosmium** single-pass over the
PBF that collects route *relations* (`route=bicycle|hiking|train|bus|…`), the *ways*
that make up the network (`highway=cycleway`, `highway=path/footway`, `railway=rail`,
`highway=bus_guideway`, …), and optional *infrastructure* nodes (bike parking, alpine
huts, stations, bus stops), emitting them as one GeoJSON `FeatureCollection` with
`feature_type` = `route`/`way`/`infrastructure`. `RouteStatistics` /
`FilterRoutesByType` are pure post-passes over that GeoJSON.

**GTFS** (`gtfs_extractor.py`) is **pure Python stdlib** (`csv`, `zipfile`, `json`,
`math`) — it downloads a feed ZIP (`requests`), reads `stops.txt` / `routes.txt` /
`shapes.txt` / `stop_times.txt` / `calendar.txt`, and derives stops (points), route
geometries (from `shapes.txt`, falling back to stop sequences), per-stop service
frequency, and aggregate stats. The OSM-integration facets (`NearestStops`,
`StopAccessibility`, `CoverageGaps`, `RouteDensity`) join a stops GeoJSON against an
arbitrary OSM-feature GeoJSON to produce walk-distance bands (400 m / 800 m), grid
coverage gaps, and density heatmaps; `GenerateReport` runs them all.

**Elevation** (`elevation_handlers.py`) samples each route vertex against a DEM. The
default source `"srtm"` calls the free **Open-Elevation API**
(`api.open-elevation.com`, SRTM 30 m) in **batches of 100 coordinates**; `"mapbox"`
(Terrain-RGB) needs a key. All elevations are reported in **feet**. `EnrichWithElevation`
attaches an elevation profile + stats (min/max/gain/loss/avg); the `FilterBy*` facets
and convenience extractors (`HighElevationHikingTrails`, `ClimbingRoutes`, …) select
routes by elevation thresholds.

**Pairwise routing** (`routing_handlers.py`, `osm.Routing.ComputePairwiseRoutes`) is
the pure all-pairs helper referenced by the city-routing workflows: it takes a cities
GeoJSON + a `RoutingTopology` and returns one LineString per city pair
(`itertools.combinations`) with aggregate distance/duration. (The older
`osm.CityRouting.CityRouteMap` that lived here was removed; the live city-routing
workflows are the GraphHopper ones in [graphhopper](graphhopper.md) and the
approximate ones in [network](network.md).)

## Fan-out

**Single-region, linear** across this group. The `osm.ElevationMap.*` and
`osm.Elevation.RegionWorkflows.*` workflows are per-region `andThen` chains
(resolve → extract → enrich → filter → render), not `foreach` fan-outs; route
extraction and GTFS parsing are each one task over one input. Fleet fan-out is
expressed by callers that loop regions at a higher level.

## Filtering & attributes

- **Bicycle** — relations `route=bicycle|mtb`; ways `highway=cycleway`,
  `cycleway=lane|track|…`, `bicycle=designated|yes`; infra
  `amenity=bicycle_parking|bicycle_rental|bicycle_repair_station`, `shop=bicycle`.
- **Hiking** — relations `route=hiking|foot|walking`; ways
  `highway=path|footway|pedestrian|track`, `foot=designated|yes`, `sac_scale=*`; infra
  `amenity=shelter|drinking_water`, `tourism=alpine_hut|viewpoint|…`, `information=*`.
- **Train** — relations `route=train|railway|light_rail|subway|tram`; ways
  `railway=rail|light_rail|subway|tram|narrow_gauge`; infra `railway=station|halt|…`,
  `public_transport=*`.
- **Bus** — relations `route=bus|trolleybus`; ways `highway=bus_guideway`,
  `bus=designated`; infra `amenity=bus_station`, `highway=bus_stop`.
- **Network level** filters route relations by `network=` tag: cycling
  `icn`/`ncn`/`rcn`/`lcn`, walking `iwn`/`nwn`/`rwn`/`lwn` (`"*"` = all).
- **GTFS** filters by `route_type` (basic + extended GTFS codes).
- **Elevation** filters on computed elevation values (feet), not OSM tags.

## External libraries / binaries

- **`pyosmium`** (pip; osmium bindings) — route extraction from PBF. Presence is
  checked via `importlib.util.find_spec("osmium")`; the extractor **degrades
  gracefully to empty results** if it's missing.
- **`requests`** (pip) — GTFS feed download and the Open-Elevation / Mapbox HTTP
  lookups.
- **GTFS parsing uses only the Python stdlib** (`csv`, `zipfile`, `json`, `math`) —
  no external library or binary.
- No binary daemon anywhere in this group.

## Facets & workflows

`osm.Routes` extractors are `external`/`expensive`; filters/stats are `pure`/`cheap`.
GTFS `DownloadFeed` is `external`/`expensive`, the rest `pure`/`cheap`. Elevation
enrich is `external`/`moderate`, filters `pure`/`cheap`. `ComputePairwiseRoutes` is
`pure`/`cheap`.

| Facet / Workflow | Namespace | Kind | Purpose |
|---|---|---|---|
| `ExtractRoutes` / `BicycleRoutes` / `HikingTrails` / `TrainRoutes` / `BusRoutes` / `PublicTransport` | `osm.Routes` | event | Extract a transport-mode route network + infra |
| `FilterRoutesByType` / `RouteStatistics` | `osm.Routes` | event (pure) | Post-filter / aggregate stats |
| `DownloadFeed` | `osm.Transit.GTFS` | event | Download + unzip a GTFS static feed |
| `ExtractStops` / `ExtractRoutes` / `ServiceFrequency` / `TransitStatistics` | `osm.Transit.GTFS` | event (pure) | Stops, route geometries, frequency, stats |
| `NearestStops` / `StopAccessibility` / `CoverageGaps` / `RouteDensity` / `GenerateReport` | `osm.Transit.GTFS` | event (pure) | OSM×transit cross-analysis + consolidated report |
| `EnrichWithElevation` | `osm.Elevation` | event | Sample DEM elevation onto route vertices |
| `FilterByMax/Min/Gain/Range` + `HighElevation*` / `ClimbingRoutes` | `osm.Elevation` | event | Elevation-threshold selection |
| `BicycleElevationMap` / `HikingElevationMap` / `ClimbingRoutesMap` / … | `osm.ElevationMap` | workflow | Extract → enrich → filter → render map |
| `Find*ByRegion` | `osm.Elevation.RegionWorkflows` | workflow | Region-name wrappers over the elevation workflows |
| `ComputePairwiseRoutes` | `osm.Routing` | event (pure) | All-pairs shortest-path LineStrings between cities |

## Cache / output

Outputs are **GeoJSON** (route networks, transit stops/routes, elevation-enriched
routes) and derived JSON/HTML. Route extraction writes via
`facetwork.runtime.storage` / `finalize_output_file`, so on the fleet artifacts land
in **MinIO/S3** and any host can read them; elevation-map workflows render HTML maps
through `osm.viz`. This group reads its OSM input from the shared `osm.cache` PBF
cache — it has no PBF cache of its own; GTFS feeds are downloaded per `DownloadFeed`.

## Gotchas & notes

- **Open-Elevation is a public API** — rate-limited and occasionally flaky; the batch
  helper returns `0` for a failed batch rather than aborting, so a run can silently
  contain zeroed elevations. Use `mapbox` (with a key) for reliability at scale.
- **Elevations are feet**, not meters — every threshold parameter is in feet.
- **pyosmium optional** — if it isn't installed, route extraction returns empty
  GeoJSON instead of erroring; check `feature_count`.
- **GTFS route geometry degrades** to stop-sequence lines when a feed has no
  `shapes.txt` (`has_shapes=false` flags this).
- **`osm.Routes` ≠ `osm.Routing`** — `Routes` extracts route *networks* (what to draw
  along); `Routing` computes *paths* over a graph.

## Related specs

- [routing](routing.md) — engine-backed point-to-point/matrix routing.
- [network](network.md) — approximate all-pairs city routing over a tiny freeway graph.
- [graphhopper](graphhopper.md) — engine-backed city-to-city route maps.
- [source-adapters](source-adapters.md) / [cache-and-download](cache-and-download.md) —
  the PBF cache these extractors read.
