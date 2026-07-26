# Network — approximate freeway routing, no engine

**Namespace(s):** `osm.Network`, `osm.Network.workflows` ·
**FFL:** `src/osm_geocoder/handlers/network/ffl/{osmnetwork,osmnetwork_workflows}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/network/{network_handlers,network_ops}.py` ·
**Tests:** `src/osm_geocoder/handlers/network/tests/` ·
**Design:** `docs/architecture/approximate-freeway-routing.md` (facetwork repo)

## Overview

`osm.Network` is the **engine-free alternative** to the routing adapters — the
"Network layer" of the composable library. It answers "route between all large cities"
without OSRM/Valhalla/GraphHopper/pgRouting by doing a **pure, in-process graph search
over a tiny noded-freeway artifact**. Because a country's interstate network is only
single-digit megabytes, the routable graph is a content-addressed cache *directory*
that every routing runner loads **once** into memory — no daemon, no `FW_*_URL`, no
replica lifecycle to manage.

Every facet here is `pure`, which is the point: pure tasks fan out freely across the
fleet, and the shared network artifact is loaded read-once-per-runner. This is the
domain's showcase of the "read-once, route-in-memory" model applied to a truly small
graph (contrast [graphhopper](graphhopper.md), which uses the same idea over a full
GraphHopper build).

## How it works

`network_ops.py` is the compute core (three operations), wrapped by
`network_handlers.py` (facet dispatch + result caching):

1. **`build_network`** — take a freeway-LineString GeoJSON, **node** it at
   intersections via **shapely** `unary_union`, build a **networkx** adjacency graph
   with edge weight = segment length in meters, and persist it as a cache **directory
   artifact**: `nodes.geojson` + `edges.geojson` + `graph.json`. `graph.json` is the
   authoritative, language-neutral adjacency list — every runner rebuilds a networkx
   graph from it in milliseconds. The entry is **content-addressed** (keyed by input
   SHA-256 + snap tolerance + ref filter) via the shared `_osm_tools` sidecar protocol,
   so it's built once and shared across the fleet exactly like the graphhopper/osrm
   artifacts. `NetworkResult` reports `connected_components` and
   `largest_component_frac` as **build-quality signals** — a fragmented graph means
   noding under-connected the network.
2. **`approx_route`** — snap A and B to the nearest network nodes, Dijkstra by length,
   and return the route plus the **closest reachable point to B** (`reached_b`,
   `gap_to_b_km`) when B is off-network.
3. **`route_matrix`** — all-pairs over the small graph, one single-source Dijkstra per
   origin. `route_layer` is its drawable twin: one LineString per city pair as a
   single GeoJSON layer, with optional `simplify_tolerance_m` geometry thinning.

`points` inputs are flexible: a JSON list of `{lon,lat,name}`, a raw GeoJSON *path*
(the cities layer feeds straight in), or a `"lon,lat;lon,lat"` string.

## Fan-out

This is where `osm.Network` earns its place. Two fan-out workflows in
`osmnetwork_workflows.ffl`:

- **`RouteFanout(network_path, pairs)`** — `andThen foreach p in $.pairs` spawns **one
  `ApproxRoute` task per (from,to) pair**. The N pure tasks are claimed lock-free
  across the runner fleet, each runner loading the shared network **once**
  (read-once-per-runner). This is the workflow used for the **multi-server proof**:
  runners on different hosts/containers share the one network artifact.
- **`CollectRouteLayers(region_names, …)`** — `andThen foreach name in $.region_names`
  fans out per region (each `osm.cache.Download` auto-caches that state's PBF, then
  extracts the interstate-motorway layer + cities ≥ threshold), returning per-region
  layer lists. Its companion fan-in workflows (`RenderCityRoutesMap`,
  `RenderCityRoutesTiledMap`) then linearly merge → `BuildNetwork` → route → render
  (folium HTML or PMTiles tiled MapLibre), because FFL has no post-`foreach`
  aggregation block — the genomics fan-out / fan-in split.

`CityRoutesByPopulation` / `CityRoutesFromCache` are the single-region capstone
(cache → population filter → motorway network → matrix), fannable across states with a
`foreach` at the workflow.

## Filtering & attributes

- **`ref_filter`** on `BuildNetwork` keeps only ways whose OSM `ref` tag **starts with
  the prefix** — `"I "` selects US interstates; `""` keeps all freeways (e.g. for
  Europe, where interstate numbering doesn't apply).
- Upstream, the freeway edges come from `osm.Source.PBF.ExtractRoads(road_class=…)` /
  `osm.Filters.FilterGeoJSONByOSMType(tag_key="highway", tag_value="motorway")`.
  `road_class="motorway"` is strict freeways (cheapest); `"major"` adds `trunk`, which
  bridges class-downgrade gaps where a freeway corridor drops to trunk (borders/urban
  bypasses).

## External libraries / binaries

- **`shapely`** (pip) — `unary_union` noding, `STRtree` nearest-node snapping,
  geometry ops.
- **`networkx`** (pip, ≥3.0) — the routable graph and Dijkstra shortest paths.
- Both are **hard requirements checked up front** (`HAS_SHAPELY` / `HAS_NETWORKX`);
  the layer fails clearly listing what's missing rather than degrading silently.
- The shared `_osm_tools` `sidecar`/`storage` libraries for the content-addressed
  cache. **No binary, no daemon, no engine** — that is the entire premise.

## Facets & workflows

`BuildNetwork` is `pure`/`moderate`; `ApproxRoute` / `RouteMatrix` / `RouteLayer` are
`pure`/`cheap`. Result schemas: `NetworkResult`, `ApproxRouteResult`,
`RouteMatrixResult`, `RouteLayerResult`.

| Facet / Workflow | Namespace | Kind | Purpose |
|---|---|---|---|
| `BuildNetwork(edges_path, snap_tolerance_m=25, ref_filter="")` | `osm.Network` | event (pure) | Node freeway LineStrings → cached routable graph dir |
| `ApproxRoute(network_path, from/to lat/lon)` | `osm.Network` | event (pure) | Snap A/B, Dijkstra, closest-reachable-to-B |
| `RouteMatrix(network_path, points)` | `osm.Network` | event (pure) | All-pairs routing (JSON list / GeoJSON path / string) |
| `RouteLayer(network_path, points, simplify_tolerance_m=0)` | `osm.Network` | event (pure) | All-pairs route geometries as one drawable GeoJSON |
| `CityRoutesByPopulation` / `CityRoutesFromCache` | `osm.Network.workflows` | workflow | Cache → cities → interstate graph → matrix |
| `RouteFanout(network_path, pairs)` | `osm.Network.workflows` | workflow | **foreach** one `ApproxRoute` per pair — the fleet fan-out |
| `CollectRouteLayers(region_names, …)` | `osm.Network.workflows` | workflow | **foreach** per-region road+city layer collection |
| `RenderCityRoutesMap` / `RenderCityRoutesTiledMap` | `osm.Network.workflows` | workflow | Fan-in merge → build → route → render (folium / PMTiles) |

## Cache / output

- **Network artifact** — `cache/osm/network/<key>/` (content-addressed dir:
  `nodes.geojson` + `edges.geojson` + `graph.json`) via the sidecar cache. Durable and
  content-addressed, so it is shared across the fleet like any other OSM artifact —
  the cross-server sharing contract that makes read-once-per-runner work.
- **Route outputs** — GeoJSON (route LineStrings, all-pairs layer) and JSON (the
  matrix of `{from,to,distance_km,reached_b}` pairs), via `finalize_output_file` →
  MinIO/S3 on the fleet.
- **Maps** — folium single-HTML (`RenderCityRoutesMap`) or a zoom-tiled
  MapLibre+PMTiles viewer (`RenderCityRoutesTiledMap`, which needs the output dir
  served over HTTP for Range fetches).

## Gotchas & notes

- **It's *approximate*.** Freeway-only, snap-to-nearest-node, straight segment weights
  — not turn-by-turn. Off-network destinations return the closest reachable point + a
  straight-line `gap_to_b_km` residual, not a failure.
- **Watch the build-quality signals.** Low `largest_component_frac` / many
  `connected_components` means the noding under-connected the network — usually the
  `snap_tolerance_m` is too tight for the source's coordinate precision.
- **`ref_filter` is prefix, and OSM-tag-dependent** — `"I "` works because US
  interstates carry `ref` like `I 5`; regions without that convention need `""` plus a
  road-class filter.
- **`simplify_tolerance_m` for continental maps** — leave it `0` when the downstream is
  a PMTiles tiler (tippecanoe simplifies per-zoom); set ~500 m for a single-HTML
  continental folium map so the file stays a sane size.
- **Pure ⇒ free fan-out** — resist making any facet here `external`; purity is what
  lets `RouteFanout` scatter across the fleet with a shared read-only artifact.

## Related specs

- [routing](routing.md) — the engine-backed alternatives this layer avoids needing.
- [graphhopper](graphhopper.md) — the same read-once-in-memory model over a full graph.
- [fan-out-pattern](fan-out-pattern.md) — the per-leaf fleet fan-out shared with
  heatmaps/cities/atlases.
- [source-adapters](source-adapters.md) — the `ExtractRoads` / population layers this
  composes over.
