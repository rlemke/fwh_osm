# OSM Geocoder — Feature Specifications

This directory holds one **spec per OSM feature**. Each document follows a common
shape ([`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md)) and states, for that feature: how it
works, whether and how it **fans out** across the fleet, what it **filters** and on
which OSM **attributes/tags**, the **external libraries/binaries** it relies on, its
**facets & workflows**, and its **cache/output**. Claims are grounded in the FFL
`/** … */` docstrings, the handler code, and the tools — the source of truth for
each facet remains its FFL docstring; these specs are the feature-level narrative
over them.

**Start here:** [**Planet Extraction**](planet-extraction.md) — the flagship
feature (the self-hosted "Geofabrik") and the deepest write-up, plus the companion
research paper in the framework repo (`docs/thesis/paper-geofabrik-replacement.md`).

## Cross-cutting

| Spec | What it covers |
|------|----------------|
| [planet-extraction.md](planet-extraction.md) | **Flagship.** Planet → per-region extract tree; self-generated boundaries + osmfr/TIGER fallback; single-atomic `BuildAdminSet`; adaptive memory batcher; incremental publish + resume; delta upkeep. |
| [fan-out-pattern.md](fan-out-pattern.md) | The fleet fan-out pattern shared by heatmaps, cities, emergency, and planet: a `foreach` over region leaves → one distributed task per leaf, in parallel. |

## Data ingest & sources

| Spec | What it covers |
|------|----------------|
| [cache-and-download.md](cache-and-download.md) | The consumer side of the self-hosted Geofabrik: region-key resolution, `FW_GEOFABRIK_BASE_URL`, the download gate, and the `afl-cache` layout. |
| [source-adapters.md](source-adapters.md) | PBF / PostGIS / GeoJSON / Overture source adapters — one namespace per input format, same GeoJSON output schema. |
| [shapefiles.md](shapefiles.md) | Census TIGER (and other ESRI) shapefiles → GeoJSON polygons via `pyshp`; county-suffix normalization. |
| [osm-changes.md](osm-changes.md) | OSM change / delta processing. |
| [clip.md](clip.md) | Clipping extracts/features to a region or polygon. |

## Filtering, transform & vocabulary

| Spec | What it covers |
|------|----------------|
| [filters.md](filters.md) | The `ByScript` predicate over `props` (Python expression on OSM tags), Osmose QA checks, validation — the core filtering mechanism. |
| [transform.md](transform.md) | Geometry/attribute transforms. |
| [population.md](population.md) | Population-weighted filtering / areal interpolation. |
| [vocab.md](vocab.md) | NL term → `key=value` tag resolution (NL→tag composition). |
| [spatial.md](spatial.md) | Spatial operations and workflows. |

## Features, POI & buildings

| Spec | What it covers |
|------|----------------|
| [amenities.md](amenities.md) | `amenity=` / `shop=` / POI extraction (+ air quality). |
| [buildings.md](buildings.md) | `building=` footprints, heights/levels. |
| [poi.md](poi.md) | Points of interest. |
| [parks.md](parks.md) | Parks / protected areas (`leisure=park`, national parks). |
| [boundaries.md](boundaries.md) | Admin-boundary extraction/rendering (composes with planet's admin-set generation). |

## Visualization, tiles & roads

| Spec | What it covers |
|------|----------------|
| [visualization.md](visualization.md) | GeoJSON → maps (MapLibre/folium HTML), choropleths, publish-to-site. |
| [heatmaps.md](heatmaps.md) | Heat maps with per-leaf fleet fan-out (extract → script-filter → render → merge). |
| [tiles.md](tiles.md) | PMTiles / MBTiles / XYZ tiling. |
| [roads.md](roads.md) | `highway=` road extraction/rendering, zoom-level building. |

## Routing

| Spec | What it covers |
|------|----------------|
| [routing.md](routing.md) | Unified routing over multiple engines (GraphHopper / OSRM / Valhalla / pgRouting) + common types/API. |
| [graphhopper.md](graphhopper.md) | Embedded-GraphHopper Java agent (polyglot protocol), graph baking. |
| [valhalla.md](valhalla.md) | Valhalla routing. |
| [routes.md](routes.md) | GTFS transit, city routing, elevation. |
| [network.md](network.md) | Approximate freeway routing — in-process graph search over a noded-freeway artifact. |

## Composition & domain apps

| Spec | What it covers |
|------|----------------|
| [emergency-atlas.md](emergency-atlas.md) | The 4-level fan-out/fan-in emergency-access atlas. |
| [cities.md](cities.md) | Per-city fan-out pipelines. |
| [voting.md](voting.md) | Voting / electoral-geography workflows. |
| [postgis-db.md](postgis-db.md) | PostGIS import/query, osm2pgsql-compatible views, spatial SQL. |
| [composed-workflows.md](composed-workflows.md) | High-level composed pipelines reusing lower-level facets. |
| [geocoding.md](geocoding.md) | Forward/reverse geocoding. |

---

*See also the machine-readable capability index at
[`src/osm_geocoder/catalog.yaml`](../src/osm_geocoder/catalog.yaml) (workflows +
facets by intent), the repo [`CLAUDE.md`](../CLAUDE.md) (domain contract), and the
[`USER_GUIDE.md`](../USER_GUIDE.md) (project organization). The live/queryable
interface is the MCP `fw_capabilities` / `fw_catalog_search` / `fw_describe_handler`
tools.*
