# Roads — highway extraction and low-zoom infrastructure

**Namespaces:** `osm.Roads` + `osm.Roads.ZoomBuilder` ·
**FFL:** `src/osm_geocoder/handlers/roads/ffl/{osmroads,osmzoombuilder}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/roads/{road_handlers,road_extractor,zoom_handlers}.py`
(+ `zoom_graph`, `zoom_sbs`, `zoom_selection`, `zoom_detection`, `zoom_builder`)

## Overview

Roads is two related features over the OSM `highway=*` network:

1. **`osm.Roads`** — extract the road network from a region PBF as GeoJSON
   LineStrings, classified (motorway / trunk / primary / … / residential) and
   attributed (speed limit, surface, lanes, one-way), then compute stats or
   re-filter it.
2. **`osm.Roads.ZoomBuilder`** — an offline pipeline that decides, per logical road
   edge, the **minimum zoom** (2–7) at which it should appear on a progressively-
   rendered map, using structural importance (betweenness) plus bypass/ring
   detection. This is the "which roads matter at continental zoom" builder.

Both start from a cached region PBF (see [cache-and-download](cache-and-download.md))
and emit path-based artifacts for downstream rendering/tiling.

## How it works

### Extraction (`osm.Roads` / `road_extractor.py`)

`extract_roads` (in `road_extractor.py`) is the workhorse. Using an
`osmium.SimpleHandler`, it iterates every way carrying a `highway` tag, builds
geometry with osmium's `WKBFactory` (→ shapely → GeoJSON LineString), classifies it
via `classify_road`/`ROAD_CLASS_MAP`, and — unless `road_class == "all"` — keeps only
ways whose class matches. Every OSM tag is preserved as a feature property (including
`ref` route numbers like `I 5`, and `name`), plus a derived `road_class` and a
`node_ids` array (the OSM node-id sequence, aligned 1:1 with the geometry) that
`osm.Network` uses to build a shared-node-id graph. Output is streamed to a temp file
then finalized into place (multi-GB safe).

The post-processing facets are pure passes over an already-extracted GeoJSON:
`RoadStatistics` (`calculate_road_stats` — per-class km, speed/surface/lane counts),
`FilterRoadsByClass`, and `FilterBySpeedLimit`.

> **Registration note.** `road_handlers.py` registers **only** `RoadStatistics`,
> `FilterRoadsByClass`, and `FilterBySpeedLimit`. The many *extraction* facets
> declared in `osmroads.ffl` (`ExtractRoads`, `Motorways`, `PrimaryRoads`, …,
> `Bridges`, `Tunnels`, `Roundabouts`, `PavedRoads`, `RoadsWithSpeedLimit`) are the
> declared road-extraction surface; the concrete extraction is reached through the
> **source-adapter** namespaces — `osm.Source.PBF.ExtractRoads` (and PostGIS /
> Overture equivalents), which call `road_extractor.extract_roads`. See
> [source-adapters](source-adapters.md).

### Low-zoom builder (`osm.Roads.ZoomBuilder` / `zoom_*`)

An offline pipeline that produces a `minZoom` per logical edge (plus bypass / ring /
backbone flags) for map zooms 2–7. `zoom_handlers.py` wires each stage:

1. **`BuildLogicalGraph`** (`zoom_graph.build_logical_graph`) — reads the PBF,
   keeps `highway`-tagged routable ways (`HIGHWAY_TO_FC` → functional class,
   restricted to `ROUTABLE_FCS`), merges runs of way between decision nodes into
   *logical edges* with `fc`/`ref`/`maxspeed`/`bridge`/`tunnel` attributes.
2. **`BuildAnchors`** — city anchor node sets for a given zoom level.
3. **`ComputeSBS`** (`zoom_sbs`) — Structural Betweenness Sampling: samples
   origin-destination pairs between anchors, **routes each via the GraphHopper HTTP
   API** (parallel `requests` through a `ThreadPoolExecutor`), snaps routes back to
   logical edges, and accumulates betweenness votes.
4. **`ComputeScores`** → per-zoom importance scores from the SBS votes.
5. **`DetectBypasses` / `DetectRings`** — settlement bypass and ring-road detection.
6. **`SelectEdges`** — budgeted greedy selection under adaptive per-cell budgets,
   with backbone repair (keep the network connected).
7. **`ExportZoomLayers`** → `ZoomBuilderResult` (CSV + metrics + zoom distribution).

`BuildZoomLayers` orchestrates 1→7 end-to-end for one region.

## Fan-out

**Single-task per region — no `foreach` in these FFL files.** Road extraction runs
against one region PBF on the runner that claims it; `BuildZoomLayers` orchestrates
the whole zoom pipeline for a single region in one task. Fleet-scale parallelism
comes from *outer* workflows that fan the region set (the same per-leaf pattern as
heatmaps/cities — see [fan-out-pattern](fan-out-pattern.md)); an individual road
extract or zoom build is the per-leaf unit, not the driver.

## Filtering & attributes

- **Primary filter: the `highway` tag.** Only ways with `highway=*` are considered;
  `classify_road` maps the value through `ROAD_CLASS_MAP` — `motorway`,
  `motorway_link`, `trunk(_link)`, `primary(_link)`, `secondary(_link)`,
  `tertiary(_link)`, `residential`, `living_street`, `service`, `unclassified`,
  `track`, `path`/`footway`/`cycleway`/`bridleway` — everything else is `"other"`.
- **`road_class` selector** — an exact class, `"all"`, or a **composite token**
  `"major"`/`"freeway"` = `{motorway, trunk}`. The composite exists so a continuous
  freeway corridor that drops to `trunk` at borders/urban bypasses (e.g. the D-100/
  E80 at the Turkey↔Bulgaria border) stays connected — a motorway-only graph breaks
  there.
- **Attribute tags read:** `maxspeed` (parsed, `mph`→km/h via `parse_speed_limit`),
  `surface` (against `PAVED_SURFACES` / `UNPAVED_SURFACES` sets), `lanes`, `oneway`,
  `ref`, `name`. `FilterBySpeedLimit` keeps features whose parsed `maxspeed` is in a
  `[min, max]` range.
- **ZoomBuilder** additionally filters to `ROUTABLE_FCS` (motorway…unclassified) and
  scores functional class via `FC_SCORES`.

## External libraries / binaries

- **`osmium`** — osmium-tool binary + pyosmium; the PBF reader for both extraction
  (`SimpleHandler` + `WKBFactory`) and `BuildLogicalGraph`. Gated behind
  `HAS_OSMIUM`.
- **`shapely`** (pip) — WKB → GeoJSON geometry conversion in `extract_roads`.
- **GraphHopper** — an external **routing engine** (Java), reached over its **HTTP
  API** (`GRAPHHOPPER_API_URL`, default `http://localhost:8989`) by `ComputeSBS` for
  OD routing. Not a pip dep — a running service (`GraphHopperCache` is a facet param).
  See [multi-language-handlers] / the graphhopper handlers.
- **`requests`** (pip) — the GraphHopper HTTP calls, issued in parallel.
- Length is computed in-process with the Haversine formula (`_haversine_length`) — no
  projection library needed.

## Facets & workflows

`osm.Roads` extraction facets carry `Effect(external)` + `Cost(expensive)`;
stats/filter facets are `Effect(pure)` + `Cost(cheap)`.

| Facet | Kind | Purpose |
|---|---|---|
| `ExtractRoads(cache, road_class)` | event (declared; via Source adapters) | Extract highways, optionally by class |
| `Motorways` / `PrimaryRoads` / `SecondaryRoads` / `TertiaryRoads` / `ResidentialRoads` | event (declared) | Class-specific extracts |
| `MajorRoads` | event (declared) | motorway + primary + secondary |
| `Bridges` / `Tunnels` / `Roundabouts` | event (declared) | Structural segment extracts |
| `PavedRoads` / `UnpavedRoads` | event (declared) | By `surface` |
| `RoadsWithSpeedLimit` | event (declared) | Ways carrying `maxspeed` |
| `RoadStatistics(input_path)` | event (**registered**, pure/cheap) | Aggregate km-by-class + attribute counts |
| `FilterRoadsByClass` / `FilterBySpeedLimit` | event (**registered**, pure/cheap) | Re-filter an extracted GeoJSON |

`osm.Roads.ZoomBuilder` — all `Effect(pure)` + `Cost(moderate)`:

| Facet | Purpose |
|---|---|
| `BuildLogicalGraph(cache)` | PBF → logical-edge road graph |
| `BuildAnchors` / `ComputeSBS` / `ComputeScores` | City anchors → SBS routing → per-zoom scores |
| `DetectBypasses` / `DetectRings` | Settlement bypass / ring detection |
| `SelectEdges` / `ExportZoomLayers` | Budgeted selection → exported zoom layers |
| `BuildZoomLayers(cache, graph, …)` | End-to-end orchestration for one region |

## Cache / output

- **Output cache** (`cached_result`/`save_result_meta`) on all handlers, keyed on the
  input path+size and params (extraction bumps a `schema=node_ids_v2` cache key so
  the `node_ids` addition re-extracts cleanly).
- **Extraction output:** GeoJSON LineStrings at
  `<out_dir>/osm-roads/<pbf stem>_roads_<class>.geojson` (`resolve_output_dir`), on
  local disk or the object store per `FW_STORAGE`.
- **ZoomBuilder output:** an `output_dir` (default
  `/Volumes/afl_data/output/osm/zoom-builder`) with a per-edge CSV
  (`ZoomEdgeResult`) and a metrics JSON; intermediate graph/anchors/SBS artifacts sit
  beside the PBF under `zoom-builder/`.

## Gotchas & notes

- **Extraction facets are declared here but serviced by the source adapters.** Don't
  expect `osm.Roads.Motorways` to have a handler in `road_handlers.py`; road
  extraction runs through `osm.Source.PBF.ExtractRoads` (+ PostGIS/Overture).
- **Incomplete geometry is skipped, not failed** — ways with missing nodes (common at
  per-region extract seams) are dropped silently in `extract_roads`.
- **ZoomBuilder needs a running GraphHopper.** `ComputeSBS` will not route without the
  HTTP engine at `GRAPHHOPPER_API_URL`; it is a heavy, moderate-cost pipeline meant
  for capable hosts.
- **Composite `major`/`freeway` = motorway+trunk** deliberately — a motorway-only
  network breaks at borders/bypasses where corridors drop to `trunk`.
- **Speed parsing is lenient** — `mph` is converted to km/h; unparseable `maxspeed`
  becomes `None` (the way is kept, just without a speed).

## Related specs

- [source-adapters](source-adapters.md) — where `ExtractRoads` is actually wired
  (PBF / PostGIS / Overture).
- [cache-and-download](cache-and-download.md) — the region PBF these read.
- [fan-out-pattern](fan-out-pattern.md) — per-region fan-out that parallelizes road
  builds across the fleet.
- [tiles](tiles.md) / [visualization](visualization.md) — tiling and rendering the
  extracted road layers.
