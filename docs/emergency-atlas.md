# Emergency-Access Atlas

**Namespace(s):** `osm.emergency`, `osm.emergency.flows` ·
**FFL:** `handlers/emergency/ffl/osm_emergency.ffl` ·
**Handlers:** `handlers/emergency/emergency_handlers.py`, `handlers/emergency/emergency_ops.py`

## Overview

The emergency-access atlas answers a civic question: *how quickly can each city in a
region reach the emergency facilities it depends on — hospitals, fire stations, police?*
It scores every qualifying city 0–100 on network distance to the nearest facility of
each category, rolls that up to a population-weighted **region readiness score**, ranks
the regions, and renders a MapLibre map (points coloured by readiness, popups with the
full per-category breakdown) plus a rankings table and a methodology disclosure.

It is the domain's **fan-out/fan-in showcase**: a four-level nesting of the
[fan-out pattern](fan-out-pattern.md) — regions → cities → categories → route-pairs —
where every fan-in is carried by a child facet's return, the canonical relative-scoping
composition. Its design contract lives in the framework repo at
`docs/architecture/emergency-access-atlas.md`.

## How it works

The pipeline nests four `foreach` levels, each in a facet that returns the aggregated
list its parent consumes:

```
ContinentalEmergencyAtlas
  → AnalyzeRegions        (foreach region)
     → AnalyzeRegion      (resolve + download, catch → RegionFailure)
        → AnalyzeRegionFromCache  (roads→network, population→cities, facility layers)
           → AnalyzeCities        (foreach city)
              → AnalyzeCity → AnalyzeCityCategories  (foreach category)
                 → AnalyzeCityCategory
                    → NearestCandidates  (pure: haversine k-nearest + crow-flies buckets)
                    → RouteCandidates    (foreach pair: osm.Network.ApproxRoute)
                    → CategoryMetrics    (pure fan-in, inline script)
```

Data shape at each stage:

1. **Per region** (`AnalyzeRegionFromCache`): the cached PBF →
   `osm.Source.PBF.ExtractRoads(road_class = "major")` → `osm.Network.BuildNetwork`
   (an in-process routable graph, no engine daemon); and
   `ExtractCategory(category = "population")` → `TopCities` (the top-N populous
   `place=city|town` settlements as a JSON list of `{name, lat, lon, population}`).
2. **Facility layers**: `ExtractCategory(category = "amenities")` (all `amenity=*`),
   then `osm.Filters.FilterGeoJSONByOSMType` per type (`amenity=hospital`,
   `fire_station`, `police`) → `BuildCategorySet` assembles the non-empty layers into a
   JSON `[{name, path}]`.
3. **Per city × category** (`AnalyzeCityCategory`): `NearestCandidates` picks the k
   nearest facilities by straight line and emits `(city → facility)` route pairs plus
   crow-flies bucket counts (10/25/50 km); `RouteCandidates` fans out one
   `osm.Network.ApproxRoute` task per pair over the region's major-road network;
   `CategoryMetrics` (inline script) reduces the routed distances to nearest/median
   network km, buckets, and per-100k facility density.
4. **Fan-ins**: `CityReadiness` scores each category component
   (`100 × (1 − nearest/50)`, clamped, 0 if no facilities) and bundles the city;
   `RegionReadiness` writes the city-point GeoJSON layer and the population-weighted
   region score; `RankRegions` sorts regions and appends excluded rows;
   `RenderAtlas` merges the layers and emits the HTML.

**Pure logic vs capability tier.** The reductions (`CategoryMetrics`, `CityReadiness`,
`RegionFailure`, `RankRegions`) are **inline `script {}` blocks** in the FFL — no handler,
no task, no deployment to change them (per the framework's script-environments model).
The handlers in `emergency_handlers.py` are only the capability tier: facets that read
or write files or feed the renderer (`TopCities`, `BuildCategorySet`,
`NearestCandidates`, `RegionReadiness`, `RenderAtlas`), thin dispatch over
`emergency_ops.py`.

## Fan-out

Four nested levels of the [fan-out pattern](fan-out-pattern.md), each a facet whose body
is a `foreach` yielding a 1-element list:

- **Regions** — `AnalyzeRegions` is the workflow-level `foreach name in $.region_names`;
  the whole region list runs concurrently across the fleet.
- **Cities** — `AnalyzeCities` fans `foreach city in $.cities`.
- **Categories** — `AnalyzeCityCategories` fans `foreach cat in $.categories`, reading
  `$.cat.path` / `$.cat.name` and the parent's network via `$.network_path`.
- **Route-pairs (leaf)** — `RouteCandidates` fans `foreach p in $.pairs`, one
  `osm.Network.ApproxRoute` per (city → facility) pair, yielding
  `distances = [r.result.distance_km]`.

`AnalyzeRegion` wraps its body in a **`catch`**: a region that fails at any depth (empty
routable network, zero qualifying cities, extract failure) degrades to a `RegionFailure`
marker (`layer_path = ""`, `score = 0.0`) so the atlas completes with that region
disclosed as "excluded" rather than the whole run hard-failing on one bad region
(finding #7 from the world run). The design doc notes continent-scale invocations should
run with **bounded region concurrency** — the download/extract tier is the disk-heavy
one; the 5-region pilot needs no throttle.

## Filtering & attributes

Concrete OSM tags this feature keys on:

- **Cities**: `place=city|town` only (`CITY_PLACE_TYPES`). Admin place nodes
  (`place=state/province/county` — e.g. a node named "Georgia" carrying the whole
  state's population) are **excluded**, or they outrank every real city and poison the
  ranking (found live in the US run). A `population` tag is required (untagged places are
  disclosed-and-dropped); duplicate place nodes within **5 km** dedupe to the most
  populous.
- **Facilities**: filtered out of the `amenities` scan by `amenity=hospital`,
  `amenity=fire_station`, `amenity=police`.
- **Roads**: `road_class = "major"` (motorway…secondary) — the freeway artifact is the
  wrong scale intra-urban, so city metrics route on the major tier only.

Filter mechanism: `osm.Filters.FilterGeoJSONByOSMType(osm_type, tag_key, tag_value)` for
facilities; a Python predicate over feature `properties`/`tags` in `top_cities`.

## External libraries / binaries

- **`osmium`** (indirect) — via `osm.Source.PBF.ExtractRoads` / `ExtractCategory` for
  the road and population/amenity extraction upstream. Binary dependency.
- **stdlib only in the compute core** — `emergency_ops.py` uses `json`, `math`
  (haversine, no `shapely`/`pyproj`), `html.escape`. Centroids are exact for Points and
  bbox-average otherwise.
- **`facetwork.runtime.storage`** — backend-aware read/write so layer paths may be
  `s3://` on the fleet; `resolve_output_dir` places outputs.
- **Routing** is the in-process `osm.Network.ApproxRoute` graph search (no
  GraphHopper/Valhalla daemon) — see [composed-workflows](composed-workflows.md) and the
  framework's `docs/architecture/approximate-freeway-routing.md`.
- **Rendering**: MapLibre GL (CDN `unpkg`) + CARTO Voyager basemap in the emitted HTML —
  no server-side map library.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `TopCities(input_path, max_cities, min_population)` | event (pure) | Top-N populous `place=city\|town` settlements → JSON list |
| `BuildCategorySet(hospitals_path, fire_path, police_path, shelters_path)` | event (pure) | Assemble non-empty facility layers → JSON `[{name,path}]` |
| `NearestCandidates(facilities_path, from_lat, from_lon, k)` | event (pure) | k-nearest facilities → route pairs + crow-flies buckets |
| `CategoryMetrics(...)` | facet (script) | Fan-in: routed distances → nearest/median km, buckets, per-100k |
| `CityReadiness(city, category_metrics)` | facet (script) | Fan-in: score each category component, bundle the city |
| `RegionReadiness(region, city_metrics, weights)` | event (pure) | Fan-in: population-weight region score, write city GeoJSON layer |
| `RegionFailure(region, error)` | facet (script) | Failed-region marker → honest "excluded" row |
| `RankRegions(region_metrics)` | facet (script) | Continental fan-in: sort by score, append excluded rows |
| `RenderAtlas(layer_path, rankings, title)` | event (moderate) | Merged layer → MapLibre HTML + rankings table + About |
| `RouteCandidates` / `AnalyzeCityCategory` / `AnalyzeCityCategories` / `AnalyzeCity` / `AnalyzeCities` / `AnalyzeRegionFromCache` / `AnalyzeRegion` | facet | The nested fan-out/fan-in bodies |
| `AnalyzeRegions(region_names, ...)` | workflow | Region fan-out |
| `ContinentalEmergencyAtlas(region_names, ..., title)` | workflow | Full atlas: regions → merge → rank → render |

All facets are `Effect(kind="pure") Cost(tier="cheap")` except `RenderAtlas`
(`Cost(tier="moderate")`).

## Cache / output

- **Cache namespace**: `emergency` (via `resolve_output_dir('emergency')`) — per-facet
  output-cache keyed by input path + params (`cached_result` / `save_result_meta`).
- **Region layers**: `{output_dir}/emergency/regions/<region-slug>.geojson` (city points
  with `score` + JSON `breakdown` in properties).
- **Atlas HTML**: `{output_dir}/emergency/atlas/index.html` — a self-contained MapLibre
  page. On the fleet these paths are `s3://afl-cache/...` (MinIO); locally they are on
  disk. The map is generated by `osm.emergency.flows.ContinentalEmergencyAtlas` (stamped
  in the page attribution).

## Gotchas & notes

- **Shelters are dropped in the pilot.** The newer `healthcare`/`emergency` CategoryDefs
  are **not** in `CombinedScan`'s plugin registry, so requesting them returns a silent
  empty. Facilities therefore come from the warm `amenities` scan filtered per type;
  shelters (`emergency=*` with no `amenity` tag) are omitted — the disclosed-thin
  category anyway (`shelters_path = ""`).
- **All-empty category set fails LOUD.** If every facility layer is empty,
  `build_category_set` raises rather than emitting `[]` — an empty set would cascade into
  empty-`foreach` reference errors three levels downstream. Zero-feature individual
  layers are dropped and the weights renormalize.
- **Two distance kinds, both labeled.** 10/25/50 km buckets are **straight-line**
  (crow-flies) counts over *all* facilities; nearest/median are **network** distances to
  the k routed candidates. The About panel and popups distinguish them.
- **`distance_km = -1.0`** is `ApproxRoute`'s valid *unroutable* sentinel, not an error;
  `CategoryMetrics` filters it out and flags `network_unroutable` when all pairs are
  unroutable.
- **Equal category weights are a disclosed value judgment** (`weights` JSON overrides,
  renormalized over categories present). The score formula (`100 × (1 − nearest/50)`) is
  spelled out in the map's "About this data" modal — the atlas is honest-by-construction.
- **OSM coverage varies**: a low facility `count` can mean *under-mapped*, not absent;
  the count is coverage context only and feeds no score.

## Related specs

- [fan-out-pattern](fan-out-pattern.md) — the nesting idiom and scoping rules this atlas
  is the deepest example of.
- [cities](cities.md) — shares `TopCities`-style population/`place` handling.
- [composed-workflows](composed-workflows.md) — `osm.Network` routing and the
  linear-composition half.
- [planet-extraction](planet-extraction.md) — the region extracts these analyses read.
