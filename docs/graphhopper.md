# GraphHopper — graph builds + an embedded-Java batch router

**Namespace(s):** `osm.ops.GraphHopper`, `osm.cache.GraphHopper.*`,
`osm.GraphHopper.{Europe,NorthAmerica,UnitedStates}`, `osm.Routing.GraphHopper`
(RouteBatch), `osm.Routing.GraphHopper.Cities` ·
**FFL:** `src/osm_geocoder/handlers/graphhopper/ffl/*.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/graphhopper/graphhopper_handlers.py` (Python
build ops) + **Java agent** `java/osm-gh-router/` (the `RouteBatch` router) ·
**Tools:** `tools/_osm_tools/graphhopper_build.py` ·
**Tests:** `src/osm_geocoder/handlers/graphhopper/tests/`

## Overview

GraphHopper appears in this domain in **two distinct halves**, and keeping them
straight is the key to reading this spec:

1. **Graph *building*** — `osm.ops.GraphHopper` and the per-region
   `osm.cache.GraphHopper.*` facets turn a cached PBF into an optimized routing
   graph, caching the graph directory. This is **Python** subprocessing the
   GraphHopper 8.0 `-web.jar`.
2. **Graph *routing*** — `osm.Routing.GraphHopper.RouteBatch` routes many city pairs
   over a built graph using **embedded GraphHopper inside a JVM agent** (no HTTP
   daemon). This is the domain's worked example of a **polyglot (Java) handler** riding
   the Mongo-coordinated agent protocol alongside the Python runners.

(A third, unrelated GraphHopper surface — the HTTP `Route`/`Isochrone` adapter in
`osm.Routing.GraphHopper` — lives in [routing](routing.md). It shares the namespace
but talks to a standalone graphhopper-web server, not the embedded agent.)

## How it works

**Build half.** `graphhopper_handlers.py` is a thin adapter over
`tools/_osm_tools/graphhopper_build.py`, so the `build-graphhopper-graph` CLI and the
handlers share one code path. A build resolves the region from the cache's Geofabrik
URL (`.../<region>-latest.osm.pbf` → `<region>`), then runs
`java -jar graphhopper-web.jar import <config.yml>` under a per-`(region,profile)`
lock, staging locally and finalizing into
`cache/osm/graphhopper/<region>-latest/<profile>/` with a `<profile>.meta.json`
sidecar. Cache validity requires **both** the source PBF's SHA-256 to still match the
sidecar **and** the recorded `graphhopper_version` (8.0) to match — otherwise it
rebuilds. `recreate=true` forces a rebuild. The graph is a directory tree
(`nodes`/`edges`/`geometry`/`properties`), so it is **local-backend only** (HDFS
unsupported).

**Route half.** `RouteBatch` is served by the Java agent in `java/osm-gh-router/`.
`RouteAgent.main` registers **only** `osm.Routing.GraphHopper.RouteBatch` and polls
the shared MongoDB on the `osm` task list; because `claim_task` is name-filtered, the
Python osm runners (which lack this handler) never take these tasks and the JVM never
takes theirs — the two coexist. `GraphHopperPool` keeps **one loaded GraphHopper
instance per graph dir warm in the JVM** ("read-once, route-in-memory"):
`RouteBatchHandler` loops the origin/destination pairs, calls `pool.route(...)` for
each, assembles a GeoJSON `FeatureCollection` of LineStrings, and uploads it to
`FW_OSM_OUTPUT_BASE/routes/<region>_<profile>_routes.geojson` on MinIO so any host's
`RenderMap` can read it.

A subtle, load-bearing detail from `GraphHopperPool.getOrLoad`: the prebuilt
graphhopper-**web** graph can't be loaded straight by graphhopper-**core** (the
encoded-value/profile config hash differs — load fails "Profiles do not match").
So the pool instead **downloads the region's PBF from MinIO and `importOrLoad`s it
with the core `Profile`**, guaranteeing build+route run identical code. Imported once
per region, then cached in-JVM and on local scratch disk.

## Fan-out

Two fan-out shapes, both real:

- **Batch build workflows** (`BuildMajorEuropeGraphs`, `BuildNorthAmericaGraphs`,
  `BuildWestCoastGraphs`, `BuildEastCoastGraphs`) enumerate countries/states as
  **independent parallel `andThen` steps** — each `Download → BuildGraph` chain is a
  separate task, distributed across the fleet, aggregated by a list-typed `++` yield.
- **`RenderStateRoadMaps`** uses `andThen foreach r in $.regions` to fan out **one
  motorway-map chain per US state**, each independently claimed. Its comment notes a
  built graph has millions of edges (Texas ~7.8M) — far too many to draw — so the
  legible depiction is the **motorway backbone**, extracted per state and rendered.

`CityRouteMap` / `RouteCitiesFromPath` are linear per state; the batching *inside*
`RouteBatch` (looping pairs in one Java task) is not fleet fan-out — it is one task
routing many pairs against one warm graph.

## Filtering & attributes

- **Graph build** does no tag filtering — GraphHopper ingests the whole PBF and
  decides routability from the `highway=*` network per profile.
- **`RenderStateRoadMaps`** filters to `highway=motorway` (via
  `osm.Source.PBF.ExtractRoads(road_class="motorway")`) for the drawable backbone.
- **`PlanCityPairs`** (Python, `osm.Routing`) turns a cities-centroid GeoJSON into
  origin/destination pairs by `strategy`: `nearest` (each city to its nearest, deduped
  to undirected edges), `hub` (every city to the most-populous), or `all` (every
  pair); `cap` keeps only the N most-populous cities.

## External libraries / binaries

- **GraphHopper 8.0 (Java)** — required both as the `graphhopper-web` **jar** for
  builds (`java -jar`, resolved via `--jar` → `$GRAPHHOPPER_JAR` →
  `~/.graphhopper/graphhopper-web.jar`) and as the `graphhopper-core` **embedded
  library** in the routing agent. The two versions **must match** (8.0).
- **The Java agent** (`java/osm-gh-router/`) is a Maven **shaded fat jar**:
  `graphhopper-core` 8.0, AWS SDK `s3` 2.25.60 (MinIO I/O), Jackson 2.15.2, and the
  `afl:fw-agent` SDK (built from the facetwork repo). Two-stage Dockerfile on
  `eclipse-temurin:17`, `-Xmx4g`, `maxConcurrent=4`.
- **Java 17+** for both the build subprocess and the agent (matches the
  graphhopper-core the graphs were built with).
- Python side: `subprocess`, plus the shared `_osm_tools` sidecar/storage libraries.

## Facets & workflows

Build facets carry `with Effect(kind="external") with Cost(tier="expensive")`;
`PlanCityPairs` is `pure`/`cheap`; `RouteBatch` is `external`/`moderate`.

| Facet / Workflow | Namespace | Kind | Purpose |
|---|---|---|---|
| `BuildGraph` | `osm.ops.GraphHopper` | event | Build/return a cached routing graph for one profile |
| `BuildMultiProfile` | `osm.ops.GraphHopper` | event | Build graphs for several profiles at once |
| `BuildGraphBatch` / `ImportGraph` | `osm.ops.GraphHopper` | event | Bulk-loop variant / build-if-missing |
| `ValidateGraph` / `CleanGraph` | `osm.ops.GraphHopper` | event | Existence + node/edge counts / delete graph dir |
| `<Country>` / `<State>` | `osm.cache.GraphHopper.*` | event | Per-region graph-build facets (9 continent/country namespaces, ~250 regions) |
| `BuildMajorEuropeGraphs`, `BuildNorthAmericaGraphs`, `BuildWestCoastGraphs`, `BuildEastCoastGraphs` | `osm.GraphHopper.*` | workflow | Parallel multi-region graph builds |
| `RenderStateRoadMaps` | `osm.GraphHopper.UnitedStates` | workflow | **foreach** per-state motorway-backbone maps |
| `PlanCityPairs` | `osm.Routing` | event (pure) | Cities GeoJSON → origin/destination pairs JSON |
| `RouteBatch` | `osm.Routing.GraphHopper` | event (**Java**) | Route every pair over a built graph, embedded in-JVM |
| `CityRouteMap` / `RouteCitiesFromPath` | `osm.Routing.GraphHopper.Cities` | workflow | Resolve → build → pair → RouteBatch → RenderMap |

Profiles supported by the build library: `car`, `bike`, `foot`, `motorcycle`,
`truck`, `hike`, `mtb`, `racingbike` (`GRAPHHOPPER_XMX` default `4g`).

## Cache / output

- **Graph cache** — `cache/osm/graphhopper/<region>-latest/<profile>/` (directory
  tree) + `<profile>.meta.json` sidecar, keyed on source-PBF SHA-256 + GraphHopper
  version. Local backend only.
- **Route output** — a GeoJSON `FeatureCollection` of route LineStrings, finalized by
  the Java agent to `FW_OSM_OUTPUT_BASE/routes/<region>_<profile>_routes.geojson` on
  MinIO/S3.
- **Maps** — `RenderStateRoadMaps` / `CityRouteMap` produce interactive HTML maps via
  `osm.viz.RenderMap`.

## Gotchas & notes

- **"Profiles do not match" is by design.** The agent never loads the web-built graph;
  it re-imports the PBF with core's `Profile`. First `RouteBatch` for a region pays a
  PBF download + import (seconds + heap); later ones reuse the warm in-JVM instance.
- **Graph builds are local-only** — an `s3://` graph dir can't be stat'd, which is why
  `ValidateGraph` reads node/edge counts off the `GraphHopperCache` (recorded at build
  time) rather than the binary on-disk `properties`.
- **Version lock-step**: the build jar (`graphhopper-web` 8.0) and the agent's
  `graphhopper-core` 8.0 must stay pinned together, or graphs won't route.
- **Millions of edges are not drawable** — always reduce to the motorway backbone (or
  routed pairs) before rendering.
- **Polyglot coexistence** hinges on name-filtered `claim_task`: the Java agent polls
  the `osm` task list but only advertises `RouteBatch`, so it and the Python runners
  never contend.

## Related specs

- [routing](routing.md) — the HTTP GraphHopper adapter and the other four engines.
- [network](network.md) — the daemonless approximate router (the same "read-once,
  route-in-memory" idea over a tiny graph instead of a full GraphHopper build).
- [planet-extraction](planet-extraction.md) — the PBFs these graphs are built from.
