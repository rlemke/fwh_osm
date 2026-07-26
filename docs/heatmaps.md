# Heat maps — the continent-scale fan-out showcase

**Namespace:** `osm.heatmap` (workflows) + `osm.viz.RenderHeatmap` (render facet) ·
**FFL:** `src/osm_geocoder/handlers/visualization/ffl/osmheatmap.ffl` ·
**Handlers:** `_make_heatmap_handler` in
`src/osm_geocoder/handlers/visualization/visualization_handlers.py` ·
**Tool/library:** `tools/_osm_tools/heatmap.py` (shared with the `make-heatmap` CLI)

## Overview

A heat map here answers "where are the *X* across *region*?" — Tesla superchargers
across North America, pizzerias across a state, hospitals across Europe. The
feature is the canonical demonstration of the **composable-facet** pattern: a heat
map of *any* tagged feature in *any* region is the **same four facets**
(`Download → ExtractCategory → ByScript → RenderHeatmap`); only the params change.

Its more important role is as the domain's **fan-out showcase**. A whole-continent
PBF is ~14 GB and OOMs `osmium` on a modest box, and extracting it is serial. The
preferred entry point, `ContinentHeatmap`, instead fans a `foreach` over the
region's Geofabrik leaves (states/provinces), extracts+filters each small leaf **in
parallel across the runner fleet**, merges the per-leaf point sets, and renders one
map — so a continent that the single-host path can't handle becomes N independent,
parallel sub-jobs, and wall-clock ≈ the *slowest single leaf*.

## How it works

Three workflows in `osmheatmap.ffl`, layered from single-region up to the fan-out:

- **`AmenityHeatmap`** — the single-leaf / single-small-region path. Pipeline:
  `osm.Region.ResolveRegion` → `osm.cache.Download` (the region PBF, cache hit if
  present) → `osm.Source.PBF.ExtractCategory` (pull the point category, e.g.
  `amenities`) → `osm.Filters.ByScript` (narrow to the features of interest) →
  `osm.viz.RenderHeatmap`. Default = Tesla superchargers in California.
- **`SubregionChargers`** — the **inner fan-out**. `osm.Region.ResolveRegions(names,
  expand="subregions")` expands e.g. "North America" to its finest Geofabrik leaves
  (US states + Canadian provinces + Mexico + Greenland), and the per-leaf work runs
  in its **step body** as `andThen foreach r in $.regions { Download → ExtractCategory
  → ByScript; yield [filt.output_path] }`. The list-typed `filtered_paths` return
  aggregates every leaf's filtered GeoJSON into one list at completion.
- **`ContinentHeatmap`** — the **preferred entry point** for any continent or large
  country. Calls `SubregionChargers` (the fan-out), merges the small per-leaf point
  sets with `osm.Transform.MergeLayers`, and renders one `RenderHeatmap`. Default =
  Tesla superchargers across all of North America (64 leaves).

At the render tail, `RenderHeatmap` delegates to `_osm_tools.heatmap.render_heatmap`
(the same code the `make-heatmap` CLI uses). Two styles, both dependency-light:

- **`kernel`** (default) — embeds the points and a MapLibre GL `heatmap` layer that
  does smooth Gaussian kernel density *in the browser*; ideal for sparse point sets
  like EV chargers. `weight_prop` optionally weights each point.
- **`grid`** — pure-Python square-bin aggregation (`_grid_aggregate`): counts points
  into ~`cell_km` cells (latitude-corrected longitude spacing) and renders each
  non-empty cell as a graduated-colour `circle` with its count in the popup.

## Fan-out

**Yes — this is the fan-out feature.** The unit is **one Geofabrik leaf
(state/province)**; the driver is the `foreach r in $.regions` inside
`SubregionChargers`, over the list `ResolveRegions(expand="subregions")` produces.
Each leaf independently runs `Download → ExtractCategory → ByScript`, so:

- each leaf PBF is small enough to extract on a modest host (no big-box requirement);
- the N leaves run concurrently across the fleet — add runners to go faster;
- the whole-continent alternative would need a big box and run serially.

The `foreach` lives in `resolved`'s **step body** on purpose: `$.regions` names its
own containing step (in scope) rather than a sibling block, satisfying the relative-
scoping rule `REF_CROSS_BLOCK_STEP`. `filtered_paths` (a `[String]` yield) is the
fan-in — every leaf's filtered path collected into one list, which `MergeLayers`
folds into a single GeoJSON before the one final render.

See [fan-out-pattern](fan-out-pattern.md) for the per-leaf fleet fan-out shared with
planet extraction and the cities pipelines.

## Filtering & attributes

Two filtering stages, in order:

1. **Category extract** — `osm.Source.PBF.ExtractCategory(category=…)` pulls a
   coarse point category from the PBF. `category` must be a valid `CombinedScan`
   category (`amenities`, `food`, `fuel_charging`, `parks`, `water`, `healthcare`,
   `emergency`, …).
2. **`ByScript` predicate** — a Python expression evaluated over each feature's
   `props` dict. The default `filter_script` keeps EV/Tesla chargers:

   ```python
   props.get('amenity') == 'charging_station' and (
       'tesla' in str(props.get('operator','')).lower()
       or 'tesla' in str(props.get('brand','')).lower()
       or 'tesla' in str(props.get('network','')).lower()
       or any(k.startswith('socket:tesla') for k in props))
   ```

   Reusable by swapping the script: `props.get('cuisine') == 'pizza'`,
   `props.get('amenity') == 'hospital'`, etc. — same facets, different tags.

## External libraries / binaries

- **`osmium`** (osmium-tool binary + pyosmium) — via `osm.cache.Download` /
  `ExtractCategory` upstream (the PBF extract).
- **MapLibre GL JS `3.6.2`** — loaded in the browser from unpkg by the heat-map
  HTML template; renders the `kernel` density layer and the OSM raster basemap. No
  Python GIS dependency, **no GIS engine, no API key** for the render itself.
- The render library is pure stdlib (`json`, `math`, `re`) plus the domain's
  `_osm_tools.storage` / `geojson_filter` helpers.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.viz.RenderHeatmap` | event (`Effect io`, `Cost cheap`) | Point GeoJSON → `kernel`/`grid` heat-map HTML |
| `osm.heatmap.AmenityHeatmap` | workflow | Single-region heat map (one leaf / small region) |
| `osm.heatmap.SubregionChargers` | workflow | **Inner fan-out**: `foreach` leaf → extract+filter, aggregate `filtered_paths` |
| `osm.heatmap.ContinentHeatmap` | workflow | **Preferred**: fan out → `MergeLayers` → one `RenderHeatmap` |

`RenderHeatmap` returns `(html_path, point_count, style)`; the handler derives the
output name as `<points_stem>_<style>_heatmap.html`.

## Cache / output

- **Upstream caches** (`osm.cache.Download`, `ExtractCategory`, `ByScript`) live in
  the `osm` cache namespace, so re-runs with `cache_policy="prefer_cache"` skip the
  download/extract.
- **Heat-map HTML** is written next to the point input as
  `<points_stem>_<style>_heatmap.html`. The render is storage-aware: an `s3://` /
  `hdfs://` input is localized first, the HTML is staged locally then finalized back
  onto the backend, so the map is shareable on the object store.

## Gotchas & notes

- **Prefer the fan-out.** For anything bigger than one state/province — a continent
  or a large country — use `ContinentHeatmap`, **not** `AmenityHeatmap`; the FFL
  header says so explicitly. `AmenityHeatmap` on a whole continent will try to
  download+extract a ~14 GB PBF and OOM `osmium` on a modest host.
- **Empty points fail loudly.** If the upstream `output_path` didn't propagate, the
  handler raises `ValueError` rather than silently completing with an empty map.
- **Non-point inputs** — `render_heatmap` raises `HeatmapError` when the input has no
  usable point coordinates; `kernel` style keeps only `Point` features.
- **`kernel` vs `grid`.** `kernel` is best for sparse points (smooth browser-side
  density); `grid` (a `cell_km` binned count with per-cell popups) reads better for
  dense sets and is fully pre-computed in Python.
- **MapLibre CDN.** The heat-map HTML pulls MapLibre `3.6.2` and OSM raster tiles at
  view time — the page needs internet even though the build was offline.

## Related specs

- [fan-out-pattern](fan-out-pattern.md) — the per-leaf fleet fan-out this feature
  showcases.
- [visualization](visualization.md) — `RenderHeatmap` is declared in `osm.viz`
  alongside the other renderers.
- [planet-extraction](planet-extraction.md) — the Geofabrik-leaf tree the fan-out
  expands over (`ResolveRegions(expand="subregions")`).
