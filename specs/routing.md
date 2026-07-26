# Unified Routing — one abstraction over five engines

**Namespace(s):** `osm.Routing.{Types,API,OSRM,Valhalla,GraphHopper,PgRouting,Workflows}` ·
**FFL:** `src/osm_geocoder/handlers/routing/ffl/*.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/routing/{api,osrm,valhalla,graphhopper,pgrouting}_router.py` +
`routing_adapter_handlers.py` (registration) ·
**Tests:** `src/osm_geocoder/handlers/routing/tests/`

## Overview

`osm.Routing` is a **source-adapter pattern applied to routing engines**: one set of
result schemas (`osm.Routing.Types`) with five interchangeable backends behind it.
A workflow that routes A→B, builds a distance matrix, or draws an isochrone does so
against a *namespace* — `osm.Routing.OSRM.Route` vs `osm.Routing.Valhalla.Route` —
and swaps engines by changing that namespace and nothing else, because every adapter
returns the identical `PointToPointResult` / `MatrixResult` / `IsochroneResult`
shape. Downstream steps (rendering, analysis) are engine-agnostic.

The five backends span the practical routing landscape: a **zero-setup public API**,
three **local HTTP daemons** (OSRM, Valhalla, GraphHopper) each fed by their own
per-region graph/tile build, and a **database engine** (pgRouting over PostGIS). They
differ in what they need to stand up but present the same FFL surface.

## How it works

Each adapter is a thin translator: normalize the Facetwork profile → the engine's
profile/costing name, issue the engine call, parse its response into the shared
schema, write any geometry as GeoJSON, and return. The common thread across all five
is the **"Route philosophy" graceful fallback**: when the engine is unreachable, the
handler degrades to a great-circle estimate (`haversine × 1.3`, an ~80 km/h duration)
and tags the result `backend="estimate"` rather than failing the step — so a routing
workflow always yields *something* drawable.

| Backend | Transport | What it talks to | Setup a region needs |
|---|---|---|---|
| **API** (`api_router.py`) | HTTP GET | OSRM demo `router.project-osrm.org`, or OpenRouteService (`FW_ORS_API_KEY`) | none — public endpoint |
| **OSRM** (`osrm_router.py`) | HTTP GET | local `osrm-routed` (`FW_OSRM_URL`, default `:5000`) | `osrm-extract`/`-partition`/`-customize`/`-routed` over a PBF, then a running daemon |
| **Valhalla** (`valhalla_router.py`) | HTTP POST+JSON | local `valhalla_service` (`FW_VALHALLA_URL`, default `:8002`) | `valhalla_build_tiles` from a PBF (see [valhalla](valhalla.md)), then a daemon |
| **GraphHopper** (`graphhopper_router.py`) | HTTP GET | local graphhopper-web server (`FW_GRAPHHOPPER_URL`, default `:8989`) | `java -jar graphhopper-web.jar import` (see [graphhopper](graphhopper.md)), then a daemon |
| **pgRouting** (`pgrouting_router.py`) | SQL | PostGIS + `pgrouting` extension (`FW_POSTGIS_URL`) | `osm2pgrouting` topology tables + `CREATE EXTENSION pgrouting` — **no daemon** |

Notes per engine, taken from the handlers:

- **API** enforces a client-side rate limit (`FW_ROUTING_RATE_LIMIT`, default 1.0 s
  between calls) because the OSRM demo is non-commercial and throttled.
- **OSRM** exposes the widest verb set (below) and maps `car→driving`, `bike→cycling`,
  `foot→foot`.
- **Valhalla** POSTs JSON, maps profiles to *costing models* (`car→auto`,
  `bike→bicycle`, `foot→pedestrian`), and decodes Valhalla's precision-6 encoded
  polylines into `[lon,lat]` geometry.
- **GraphHopper** sends `point=lat,lon` with `points_encoded=false` to get GeoJSON
  directly. Only `Route`/`Isochrone` are covered — Matrix and map-matching are
  separate/commercial GraphHopper modules.
- **pgRouting** runs SQL (`pgr_dijkstra` / `pgr_dijkstraCostMatrix` /
  `pgr_drivingDistance`) against the osm2pgrouting tables `<prefix>ways` and
  `<prefix>ways_vertices_pgr`; it **snaps each waypoint to the nearest network
  vertex** (`the_geom <-> ST_MakePoint`), optimizes on `length_m`, and estimates
  duration from a per-profile speed (car 80 / bike 20 / foot 5 km/h). The connection
  is opened read-only (`default_transaction_read_only=on`).

> Not covered here: `osm.Routing.GraphHopper.RouteBatch` — the **embedded-Java**
> batch router — shares this namespace but is a different mechanism (in-JVM
> GraphHopper, no HTTP daemon). It is documented in [graphhopper](graphhopper.md).

## Fan-out

The adapter facets and the composed workflows are **single-task, no fan-out** — one
route / matrix / isochrone per call. Fleet fan-out belongs to the *higher-level*
batch workflows that call routing per city or per region (see
[graphhopper](graphhopper.md)'s `CityRouteMap` and [network](network.md)'s
`RouteFanout`). `routing_workflows.ffl` composes strictly linear pipelines
(`Route → RenderMap`).

## Filtering & attributes

Routing does **no OSM tag filtering** of its own — the road network is whatever the
engine's pre-built graph/tiles/topology contains. The one selective input is the
`profile` string, which each adapter maps to the engine's own vehicle/costing profile
and which determines *which* edges are traversable (a `foot` route uses footways a
`car` route ignores). pgRouting additionally restricts to the osm2pgrouting topology
(built from routable `highway=*` ways).

## External libraries / binaries

- **`requests`** (pip) — the HTTP for the API/OSRM/Valhalla/GraphHopper adapters.
- **`psycopg2`** (pip) — pgRouting's DB access; the adapter degrades to an estimate
  when it (or the DB / topology) is unavailable.
- **The engines themselves are external, not pip**: OSRM (`osrm-backend` binaries or
  its Docker image), Valhalla (`valhalla_*` binaries), GraphHopper (the Java
  `graphhopper-web` jar), and the PostgreSQL `pgrouting` extension. Each is a
  **daemon or database** that must be stood up per region *out of band* — routing
  handlers only *talk to* them. Only pgRouting is daemonless (it's a DB engine).

## Facets & workflows

All adapter facets carry `with Effect(kind="external") with Cost(tier="moderate")`.
Shared result schemas live in `osm.Routing.Types` (`Waypoint`, `RouteResult`,
`PointToPointResult`, `MultiStopResult`, `IsochroneResult`, `MatrixResult`,
`NearestResult`, `MapMatchResult`, `TripResult`).

| Facet / Workflow | Namespace | Kind | Purpose |
|---|---|---|---|
| `Route` | API / OSRM / Valhalla / GraphHopper / PgRouting | event | Point-to-point route between two waypoints |
| `MultiStopRoute` | API / OSRM | event | Route through an ordered waypoint list |
| `Isochrone` | API / OSRM / Valhalla / GraphHopper / PgRouting | event | Reachability polygon from a center point |
| `Matrix` | OSRM / Valhalla / PgRouting | event | NxN travel-time/distance matrix |
| `Nearest` | OSRM | event | Snap a coordinate to the road network |
| `MapMatch` | OSRM | event | Match a GPS trace to the road network |
| `Trip` | OSRM | event | TSP visit-order optimization (`/trip`) |
| `RouteViaAPI` / `RouteViaOSRM` | Workflows | workflow | Route + `osm.viz.RenderMap` — same pipeline, one line differs |
| `MultiStopViaAPI` | Workflows | workflow | Multi-stop route + map |
| `IsochroneViaAPI` | Workflows | workflow | Isochrone + map |

The verb coverage is deliberately uneven and mirrors what each engine reliably
exposes: OSRM is the richest (`/table`, `/nearest`, `/match`, `/trip`), Valhalla adds
`/sources_to_targets`, pgRouting maps its three SQL functions, and GraphHopper's OSS
server only guarantees `Route`/`Isochrone`.

## Cache / output

Adapters have **no PBF/graph cache of their own** — that lives in each engine's build
(graphhopper/valhalla caches) or in PostGIS. What they write are **result artifacts**
under `resolve_output_dir("routing")`: route/isochrone GeoJSON
(`<engine>-route-<slug>.geojson`, `<engine>-isochrone-…`) and matrix JSON
(`<engine>-matrix-Npts-<profile>.json`), via `open_output` — so on the fleet they
finalize to MinIO/S3 and any host's `RenderMap` can read them. `MatrixResult` /
`TripResult` also return the arrays inline as JSON-encoded fields.

## Gotchas & notes

- **`backend="estimate"` is expected, not an error** — it means the engine was
  unreachable and the result is a straight-line approximation. Check the `backend`
  field before trusting distances.
- **Every local engine needs a per-region build first.** A `Route` call against OSRM
  for a region whose PBF was never `osrm-extract`ed just falls back to the estimate.
- **pgRouting snaps to vertices**, so a waypoint far from any routable way lands on
  the nearest network node — distances are vertex-to-vertex, not door-to-door.
- **GraphHopper HTTP `Route`/`Isochrone` ≠ `RouteBatch`.** The former is this HTTP
  adapter; the latter is the embedded-Java in-process router in the `graphhopper`
  namespace. They share the `osm.Routing.GraphHopper` namespace but nothing else.
- **Same schemas ⇒ swap by namespace** is the whole point — resist adding
  engine-specific fields to `osm.Routing.Types`.

## Related specs

- [graphhopper](graphhopper.md) — graph builds + the embedded-Java `RouteBatch`.
- [valhalla](valhalla.md) — the tile builds that feed the Valhalla daemon.
- [network](network.md) — the engine-*free* approximate-routing alternative.
- [source-adapters](source-adapters.md) — the same adapter pattern applied to inputs.
