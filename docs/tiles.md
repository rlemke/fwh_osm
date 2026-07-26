# Vector tiles — the scalable-visualization primitive

**Namespace:** `osm.Tiles` ·
**FFL:** `src/osm_geocoder/handlers/tiles/ffl/osmtiles.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/tiles/tile_handlers.py` ·
**Tools:** `tools/_osm_tools/vector_tiles_build.py`

## Overview

`osm.Tiles` is the path-in / artifact-out counterpart to `RenderMap`, for layers
too large to draw as a single inline Leaflet GeoJSON. It encodes a GeoJSON layer
into **vector tiles** — PMTiles (preferred) or MBTiles — via `tippecanoe`, so a
continental layer becomes a zoom-tiled archive the browser Range-fetches a viewport
at a time. Feed it any Extract / Filter / Transform / Spatial output; serve the
result with MapLibre (`osm.viz.RenderTiledMap`, see [visualization](visualization.md)).

It sits between transform and render: `… → GeoJSON → BuildVectorTiles → PMTiles →
RenderTiledMap → HTML`.

## How it works

The single facet `BuildVectorTiles(geojson_path, layer_name, min_zoom, max_zoom)`
is handled in `tile_handlers.py`, which:

1. does an `output_cache` lookup keyed on the input GeoJSON path+size and the
   tiling options (`layer_name`, `min_zoom`, `max_zoom`);
2. chooses the output extension by probing PATH — `pmtiles` if the `pmtiles` CLI is
   installed (smaller, single-file, HTTP-Range servable), else `mbtiles`;
3. `localize()`s the input (it may live on `s3://` / `hdfs://`) to a real local
   file, since `tippecanoe` can only read local paths;
4. runs the build under a **30 s heartbeat pump** (`_run_with_heartbeat`) so the
   task lease survives a long tiling job;
5. delegates to `vector_tiles_build.build_from_geojson`.

`build_from_geojson` shells out to `tippecanoe` to produce an MBTiles, then — when
the output is `.pmtiles` — runs `pmtiles convert` on it:

```
tippecanoe -o <tmp>.mbtiles -Z <min> -z <max> --force --layer <name>
           --drop-densest-as-needed --coalesce-densest-as-needed --read-parallel <input>
pmtiles convert <tmp>.mbtiles <out>.pmtiles
```

The `--drop-densest-as-needed` / `--coalesce-densest-as-needed` flags let
tippecanoe shed features in over-dense tiles instead of blowing the tile size
budget. When the durable output is remote (`s3://` / `hdfs://`), the artifact is
built to a **local staging** file and `finalize_from_local`'d onto the backend —
otherwise `Path("s3://…")` would collapse the scheme and write to a bogus local dir
that the downstream `RenderTiledMap` on another runner could not read.

The library also exposes `build_tiles` — a **region/cache-coupled** builder that
tiles a named region's cached GeoJSONSeq into
`cache/osm/vector_tiles/<region>-latest/<source>.pmtiles` with a sidecar, and whose
cache validity requires the source GeoJSONSeq SHA-256, the `tippecanoe_version`, and
the tiling options to all still match. The facet uses the path-based
`build_from_geojson`; `build_tiles` backs the CLI and region-oriented flows.

## Fan-out

**Single-task per layer — no `foreach` in the facet.** `BuildVectorTiles` tiles one
GeoJSON into one archive on the runner that claims it. It carries
`with Timeout(minutes=60)` because a large tiling run is long, not because it splits.
Fan-out lives at the *workflow* level that feeds it: the cities pipelines
(`osmcities_fanout.ffl`, `osmcities_routes_fanout.ffl`) fan the extraction per leaf,
tile each, and hand the resulting PMTiles set to one `RenderTiledMap`. The tiler
itself is the per-leaf unit of that fan-out, not its driver — see
[fan-out-pattern](fan-out-pattern.md).

## Filtering & attributes

None at this stage. `BuildVectorTiles` is tag-agnostic: it tiles whatever features
the upstream Extract/Filter produced, preserving their properties into the vector
tile so MapLibre can style on them. `tippecanoe`'s only "filtering" is the density
management (`--drop-densest-as-needed` / `--coalesce-densest-as-needed`) and the
zoom range — features outside a layer's `min_zoom`/`max_zoom` simply aren't emitted
at those zooms.

## External libraries / binaries

- **`tippecanoe`** — the vector-tiler. A **binary** dependency (subprocess); not a
  pip package. Produces the MBTiles.
- **`pmtiles`** CLI — converts MBTiles → PMTiles. A **binary** dependency
  (`brew install pmtiles`, or point `PMTILES_BIN` at it). Optional: absent → the
  facet stays on MBTiles. `pmtiles convert` raises a clear "install with
  brew install pmtiles" error if the binary is on PATH-miss during a PMTiles build.
- No Python GIS libraries are needed — the library is stdlib (`subprocess`,
  `hashlib`, `pathlib`) plus the domain's `sidecar` / `storage` helpers.

## Facets & workflows

| Facet | Kind | Purpose |
|---|---|---|
| `osm.Tiles.BuildVectorTiles` | event (`Effect external`, `Timeout 60min`) | GeoJSON layer → PMTiles (or MBTiles) via tippecanoe |

Returns `TileResult { output_path, format, size_bytes, min_zoom, max_zoom, layer }`,
where `format` is `"pmtiles"` when the CLI is present, else `"mbtiles"`. There is no
tiling *workflow* in `osmtiles.ffl` — the facet is composed into higher workflows
(cities, composed, network).

## Cache / output

- **Output cache:** `cached_result`/`save_result_meta` on the input GeoJSON + tiling
  options; a re-tile with identical options is a hit.
- **Path (facet) output:** `derive_output_path("vector-tiles", <input stem>, "tiles",
  <layer>, "z<min>-<max>", ext=pmtiles|mbtiles)`. PMTiles is a single file, servable
  over HTTP Range directly.
- **Region (library) output:** `build_tiles` writes
  `cache/osm/vector_tiles/<region>-latest/<source>.pmtiles` plus a sidecar recording
  the source SHA, tippecanoe version, and options.
- **Remote storage:** on `s3://` / `hdfs://` the artifact is staged locally during
  the build and finalized onto the backend, so any runner can read it — required for
  the downstream `RenderTiledMap` that may run on a different host.

## Gotchas & notes

- **Both binaries must be installed.** `tippecanoe` is mandatory; without `pmtiles`
  you silently get MBTiles instead of PMTiles (the facet degrades, `format` reflects
  it). A `pmtiles`-on-PATH but failing `convert` raises `BuildError` with stderr.
- **The 60-minute Timeout is real.** Continental layers can take many minutes;
  the handler's 30 s heartbeat keeps the lease alive so the reaper doesn't reclaim a
  healthy long-running tile job.
- **PMTiles needs a Range-capable server.** The archive is only useful behind an
  HTTP server that honours Range requests (see [visualization](visualization.md) —
  stdlib `http.server` does not).
- **Localize before tiling.** tippecanoe/pmtiles are local-only; the handler
  `localize`s remote inputs and stages remote outputs — do not hand the CLI an
  `s3://` path directly.

## Related specs

- [visualization](visualization.md) — `RenderTiledMap` serves the PMTiles this
  facet builds.
- [fan-out-pattern](fan-out-pattern.md) — per-leaf tiling as the unit of a fleet
  fan-out.
- [planet-extraction](planet-extraction.md) — the region extracts whose GeoJSONSeq
  the region-coupled `build_tiles` tiles.
