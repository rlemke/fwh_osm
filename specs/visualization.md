# Visualization — GeoJSON → interactive maps

**Namespace:** `osm.viz` (plus `osm.ops` for the per-region prebuilt-map family) ·
**FFL:** `src/osm_geocoder/handlers/visualization/ffl/osmvisualization.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/visualization/{visualization_handlers,map_renderer,html_map_handlers}.py`

## Overview

Visualization is the **render tail** of the pipeline: it turns a GeoJSON layer
(the output of any Extract / Filter / Transform / Spatial facet) into a shareable
map artifact. It sits at the very end — `source → filter → transform → render` —
and deliberately does no data work of its own; it consumes `output_path`s and
emits HTML/PNG.

Three render backends cover three scales of output, all under `osm.viz`:

1. **Folium (Leaflet.js)** — a single self-contained HTML file that embeds the
   GeoJSON inline. Good for a modest layer you want to open and pan.
2. **contextily + matplotlib** — a static PNG for embedding in a document.
3. **MapLibre GL + PMTiles** — a zoom-tiled viewer for layers too large to inline,
   where the browser Range-fetches only the tiles in the current viewport.

The heat-map facet (`RenderHeatmap`) also lives in this namespace but is
documented separately — see [heatmaps](heatmaps.md).

## How it works

Every facet is a thin handler in `visualization_handlers.py` that (1) does an
`output_cache` lookup keyed on the input GeoJSON's path+size and the render
params, (2) delegates to a function in `map_renderer.py`, and (3) saves the
result meta. The renderer functions:

- **`render_map_html`** (`RenderMap` html, `RenderMapAt`, `RenderStyledMap`,
  `PreviewMap`) — loads the GeoJSON, computes bounds/center/zoom
  (`calculate_bounds`/`calculate_center`/`calculate_zoom`), builds a
  `folium.Map(tiles="OpenStreetMap")`, adds a styled `folium.GeoJson` layer with a
  `GeoJsonTooltip` over the first five properties, `Fullscreen`, `MeasureControl`,
  `LayerControl`, a fitted bounds box, and a floating title/feature-count overlay.
- **`render_map_png`** (`RenderMap` format=`png`) — reads the GeoJSON with
  geopandas, reprojects `EPSG:4326 → EPSG:3857`, plots with matplotlib, and lays
  an OSM basemap under it via `contextily` (`ctx.providers.OpenStreetMap.Mapnik`).
- **`render_layers`** (`RenderLayers`) — the multi-file counterpart: N GeoJSON
  paths → one folium map, each layer a distinct colour from an 8-colour default
  palette, combined bounds fitted, one `LayerControl` toggle per layer.
- **`render_tiled_map`** (`RenderTiledMap`) — assembles a MapLibre GL viewer
  *directory*: an `index.html` from `_TILED_HTML_TEMPLATE` plus each input PMTiles
  archive symlinked (or copied) alongside it. The browser fetches tiles via the
  `pmtiles://` protocol over HTTP Range. Point layers render as MapLibre `circle`
  + `symbol` labels, line layers as a wide casing under a zoom-interpolated
  stroke; a legend and an "About this data" popup are generated from the layer set.

A separate family, `osm.ops.RenderHtmlMap` / `RenderHtmlMapBatch`
(`html_map_handlers.py`), renders **prebuilt per-region** HTML from a cached
Geofabrik region key (via the `_osm_tools.html_render` library), refreshing a
master `html/index.html` after each region.

## Fan-out

**Single-task — no fan-out.** Each render facet consumes one already-materialized
GeoJSON (or one PMTiles set) and writes one artifact on the runner that claims it;
there is no per-leaf split. Fan-out, when it happens, is upstream: a workflow fans
the *extraction* across regions, merges, and hands one combined layer to a single
`RenderMap`/`RenderTiledMap` call (see [heatmaps](heatmaps.md) and
[fan-out-pattern](fan-out-pattern.md)).

## Filtering & attributes

None. Visualization is source- and tag-agnostic: it renders whatever features are
in the GeoJSON it is given, reading `properties` only to build tooltips/popups and
`geometry` only to compute bounds. All tag filtering happens before this stage.

## External libraries / binaries

- **`folium`** (pip) — Leaflet.js HTML maps; gated behind `HAS_FOLIUM`. Absent →
  the handler raises an explicit `RuntimeError` with a `pip install folium` hint.
- **`geopandas` + `contextily` + `matplotlib`** (pip) — static PNG rendering;
  gated behind `HAS_STATIC`, with the same explicit-error pattern.
- **MapLibre GL JS `4.7.1` + PMTiles JS `3.2.1`** — loaded in the browser from the
  unpkg CDN by the `RenderTiledMap` template (not a Python dep). Basemap raster
  tiles come from CARTO (`dark`/`light`) or `tile.openstreetmap.org` (`osm`), and
  glyph fonts from `protomaps.github.io`.
- No GIS engine or API key is required for any facet here.

## Facets & workflows

All facets carry `with Effect(kind="io") with Cost(tier="cheap")`.

| Facet | Kind | Purpose |
|---|---|---|
| `RenderMap` | event | GeoJSON → interactive HTML (folium) or static PNG |
| `RenderMapAt` | event | HTML map centred on a lat/lon at a fixed zoom |
| `RenderLayers` | event | Several GeoJSON files → one colour-coded folium map |
| `RenderTiledMap` | event | MapLibre + PMTiles viewer dir over vector-tile layers |
| `RenderStyledMap` | event | HTML map with a custom `LayerStyle` (color/weight/opacity) |
| `PreviewMap` | event | Render + open in the default browser (dev convenience) |
| `RenderHeatmap` | event | Point GeoJSON → heat-map HTML — see [heatmaps](heatmaps.md) |
| `FormatGeoJSON` | event | Declared in FFL for text/other output; **not** in this module's `VISUALIZATION_FACETS` registration |
| `osm.ops.RenderHtmlMap` / `…Batch` | event | Prebuilt per-region HTML from a cached region key |

`FormatGeoJSON` is declared in `osmvisualization.ffl` but is not among the seven
handlers registered by `visualization_handlers.py` — treat it as a declared
surface without a handler in this module.

## Cache / output

- **Output cache:** `cached_result`/`save_result_meta` (the shared
  `output_cache`), keyed on the input GeoJSON path+size and the render params, so
  re-rendering the same layer with the same options is a hit.
- **HTML/PNG maps** land under the local maps dir (`resolve_local_output_dir("maps")`),
  named after the input GeoJSON stem so distinct queries don't collide; when
  `FW_OUTPUT_PER_RUN` is set and a `run_id` is supplied they isolate under
  `maps/runs/<run_id>/`. folium can only write to local disk, so for `s3://` /
  `hdfs://` outputs the map is saved to a temp file then finalized onto the backend
  (`_save_map_html`) — shareable on the object store like any other artifact.
- **`RenderTiledMap`** output is a **directory** (`index.html` + each PMTiles). When
  durable storage is remote it publishes the whole viewer dir (index + archives)
  under `maps/tiled/<name>/` on the object store, so a downstream publish step on a
  different runner can serve it.
- **`osm.ops` prebuilt maps** cache under `$FW_DATA_ROOT/cache/osm/html/<region>-latest/`.

## Gotchas & notes

- **`folium`/`geopandas` are optional deps.** A runner without them fails the facet
  loudly with a `pip install …` message rather than silently producing nothing.
- **The PMTiles viewer must be served over HTTP with Range support.** stdlib
  `python -m http.server` returns `200 OK` with the full file and silently breaks
  the viewer; `file://` is unreliable across browsers. Use the shipped
  `scripts/serve-tiled-map`, or nginx/caddy/`npx http-server`.
- **CDN dependency for tiled/heat maps.** The MapLibre + PMTiles JS and the CARTO/
  OSM basemap + protomaps glyphs are fetched by the browser at view time — the
  viewer needs internet, even though the build was offline.
- **MapLibre GL does not expand Leaflet's `{s}` subdomain token**, so CARTO
  subdomains `a,b,c,d` are listed explicitly in the source tiles array.
- **`RenderTiledMap` uses absolute PMTiles URLs** assembled at runtime from the
  page's own origin+path (`here`), because relative `./` URLs are unreliable with
  pmtiles 3.x.

## Related specs

- [heatmaps](heatmaps.md) — `RenderHeatmap` and the continent-scale fan-out that
  feeds it.
- [tiles](tiles.md) — `BuildVectorTiles` produces the PMTiles that `RenderTiledMap`
  serves.
- [fan-out-pattern](fan-out-pattern.md) — the per-leaf fleet fan-out that renders
  merge into a single map.
