# OSM Geocoder — Tools

Standalone command-line utilities for OSM-related data operations. Each tool is a self-contained Python program paired with a shell wrapper. Tools are intended for operator/developer use at the terminal, and FFL handlers call into the same shared libraries the CLIs call — one code path, one cache, one manifest per data type.

The data flow looks like this:

```
                         ┌────────────────────────┐
                         │  download-pbf          │  ← Geofabrik (HTTP + md5)
                         │  clip-pbf              │  ← custom bbox / polygon
                         └───────────┬────────────┘
                                     │  pbf/
                  ┌──────────────────┼──────────────────┐
                  ▼                  ▼                  ▼
       ┌──────────────────┐ ┌──────────────┐ ┌──────────────────────┐
       │ convert-pbf-     │ │ extract      │ │ build-graphhopper-   │
       │   geojson        │ │ (per         │ │   graph /            │
       │ convert-pbf-     │ │  category:   │ │ build-valhalla-tiles │
       │   shapefile      │ │  water,      │ │ build-osrm-graph     │
       │                  │ │  parks, …)   │ │                      │
       └────────┬─────────┘ └──────┬───────┘ └──────────┬───────────┘
                │                  │                    │
                ▼                  ▼                    ▼
       geojson/    shapefiles/   <category>/     graphhopper/  valhalla/  osrm/
                │        ┌───────┘                      (routing graphs)
                ▼        ▼
       ┌────────────────────────┐       ┌──────────────────┐
       │  build-vector-tiles    │       │  download-gtfs   │  ← transit feeds (HTTP)
       │  (tippecanoe → PMTiles)│       └──────────────────┘
       └───────────┬────────────┘              │
                   ▼                           ▼
              vector_tiles/                  gtfs/

      ┌─────────────────────────┐  ┌─────────────────────────────┐
      │  download-elevation     │  │  update-all (meta)           │
      │  (Copernicus DEM COG)   │  │  runs every --update-all in  │
      └───────────┬─────────────┘  │  dependency order            │
                  ▼                └─────────────────────────────┘
             elevation/
```

Every arrow is manifest-mediated: each tool records the source's SHA-256 (plus engine/version for routing) in its own manifest, so re-running a tool is a no-op when nothing has changed, and a single `./tools/update-all.sh` propagates an upstream refresh through the whole chain.

## Directory layout

```
tools/
├── README.md                        ← this file
├── install-tools.sh                 ← one-shot binary installer (brew + GraphHopper jar)
├── update-all.sh                    ← chains every --update-all in dependency order
│
├── _lib/                            ← shared library (tools and FFL handlers both import these)
│   ├── manifest.py                  ← per-cache-type JSON manifest I/O (atomic, flock'd)
│   ├── storage.py                   ← Storage abstraction (LocalStorage, HdfsStorage)
│   ├── pbf_download.py              ← Geofabrik PBF download + path-mirroring cache
│   ├── pbf_clip.py                  ← custom-geometry clipping into pbf/clips/
│   ├── pbf_geojson.py               ← osmium export to GeoJSONSeq
│   ├── pbf_shapefile.py             ← ogr2ogr export to multi-layer shapefile
│   ├── pbf_extract.py               ← category-based osmium tags-filter extracts
│   ├── graphhopper_build.py         ← GraphHopper MLD graph builder
│   ├── valhalla_build.py            ← Valhalla tile-pyramid builder
│   ├── osrm_build.py                ← OSRM extract → partition → customize pipeline
│   ├── vector_tiles_build.py        ← tippecanoe → PMTiles
│   ├── gtfs_download.py             ← GTFS feed downloader (Last-Modified / ETag)
│   └── elevation_download.py        ← Copernicus DEM via /vsicurl/ + gdalwarp
│
├── download-pbf.sh        / download_pbf.py
├── clip-pbf.sh            / clip_pbf.py
├── convert-pbf-geojson.sh / convert_pbf_geojson.py
├── convert-pbf-shapefile.sh / convert_pbf_shapefile.py
├── extract.sh             / extract.py
├── build-graphhopper-graph.sh / build_graphhopper_graph.py
├── build-valhalla-tiles.sh / build_valhalla_tiles.py
├── build-osrm-graph.sh    / build_osrm_graph.py
├── build-vector-tiles.sh  / build_vector_tiles.py
├── download-gtfs.sh       / download_gtfs.py
└── download-elevation.sh  / download_elevation.py
```

One `.py` + one `.sh` per tool. Shared code lives in `tools/_lib/` — anything reused by two or more tools (or by FFL handlers) goes there. Handler-side code imports from `_lib/` via thin re-export shims in `handlers/shared/` (e.g. `pbf_cache.py`, `pbf_convert.py`).

## Quick start on a fresh machine

```bash
# 1. Install every binary the tools shell out to (brew + GraphHopper jar).
./tools/install-tools.sh

# 2. Download a small region to verify the chain works.
./tools/download-pbf.sh europe/liechtenstein

# 3. Run everything downstream (extracts, tiles, routing, etc.).
#    Each sub-tool skips work that's already current.
./tools/update-all.sh UPDATE_ALL_SKIP="gtfs"   # skip gtfs if you don't use transit
```

## Available tools

Organized by role in the pipeline.

### Setup

- **install-tools** — one-shot installer for every binary the rest of the tools shell out to: `osmium-tool`, `gdal` (ogr2ogr), `openjdk@17`, `valhalla`, `tippecanoe`, `osrm-backend` — all via Homebrew — plus the GraphHopper runnable JAR downloaded from GitHub releases to `~/.graphhopper/graphhopper-web.jar`. Idempotent; re-running only installs what's missing. Verifies each tool at the end and prints an env-var cheat sheet.

### Source acquisition

- **download-pbf** — fetch Geofabrik PBFs into `pbf/<region>-latest.osm.pbf`. Verified against Geofabrik's published `.md5`; local `sha256` recorded for tamper detection. Sequential (Geofabrik rate-limits per IP). Region selection: positional args, `--regions-file`, or `--all` / `--all-under PREFIX` resolved from Geofabrik's `index-v1.json` (leaves-only by default; `--include-parents` for continent/country-level PBFs). `--list-missing` previews uncached regions; `--update-all` refreshes only those whose upstream MD5 changed. Supports `--backend {local,hdfs}`.
- **clip-pbf** — custom-geometry PBF via `osmium extract`. Output lands at `pbf/clips/<name>-latest.osm.pbf` so **every downstream tool treats it as a normal region called `clips/<name>`** — no special casing. Cache validity: source region SHA + clip spec (bbox values or polygon content hash). `--update-all` re-clips entries whose source has changed.
- **download-gtfs** — per-agency GTFS feed downloader. Cache validity via HTTP `Last-Modified` / `ETag` — a HEAD request decides whether to skip. Manifest records parsed `feed_info.txt` fields (publisher, version, validity window). Zip integrity verified before the cache is committed.
- **download-elevation** — Copernicus DEM GLO-30 rasters for a bbox via `gdalwarp` + `/vsicurl/` against AWS Open Data (no auth). Computes the 1°×1° tile grid intersecting the bbox, streams only the bytes needed, outputs a compressed tiled GeoTIFF at `elevation/<name>-latest.tif`.

### Derived formats

- **convert-pbf-geojson** — `osmium export` PBF → GeoJSON. Defaults to `geojsonseq` (one feature per line, streamable); `--format geojson` for a FeatureCollection. Cache keyed by source PBF SHA + format. `--jobs N` parallelizes; `--update-all` sweeps the pbf manifest for regions whose GeoJSON is missing or stale.
- **convert-pbf-shapefile** — `ogr2ogr` PBF → multi-layer ESRI Shapefile **bundle directory** (one `.shp`/`.shx`/`.dbf`/`.prj`/`.cpg` set per layer). Layers: `points`, `lines`, `multilinestrings`, `multipolygons` (the `other_relations` GeometryCollection is always skipped — shapefile can't hold it). `--layers` restricts the output; superset-semantics cache hits (a cache built with all four layers satisfies a later request for a subset).
- **extract** — `osmium tags-filter | osmium export` — one pre-filtered GeoJSONSeq per category. Categories live in `_lib/pbf_extract.py::CATEGORIES` (one dict entry defines a category: name, FFL facet name, tag expression, filter_version). Current set: `water`, `protected_areas`, `parks`, `forests`, `roads_routable`, `turn_restrictions`, `railways_routable`, `cycle_routes`, `hiking_routes`. `--extract-all-categories` runs every category per region; `--update-all` pre-filters to just the stale work. Adding a new category = one dict entry + one FFL `event facet` line.
- **build-vector-tiles** — `tippecanoe` GeoJSONSeq → PMTiles. `--source` picks the input cache (`geojson` for whole-region, or any extract category); `--all-sources` fans out across every valid source. Tiling options (min/max zoom, layer name) are part of the cache key, so changing them triggers a rebuild only for affected entries.
- **render-html-maps** — MapLibre GL JS + PMTiles viewer per region. Consumes the `vector_tiles/` cache and emits `html/<region>-latest/index.html` + `style.json`. Sub-classifies layers at render time via MapLibre filter expressions — **highways split into motorway / trunk / primary / secondary / tertiary / residential**, **water split into lakes, rivers (line + polygon), canals, streams**, **protected areas split into national park / state park / other protected / nature reserve**, and **16 POI categories with per-category colored circles with click popups showing OSM tags**. Also refreshes the master `html/index.html` table on every run. Cache validity: source PMTiles SHAs + `STYLE_VERSION`. Serve with `python -m http.server --directory $AFL_OSM_CACHE_ROOT 8000` and open `http://localhost:8000/html/`.

### Routing engines

All three follow the same interface. Each keys its cache on `source PBF SHA-256 + engine version + profile (where applicable)`, so an engine upgrade invalidates cached graphs automatically without per-region intervention.

- **build-graphhopper-graph** — GraphHopper MLD graphs at `graphhopper/<region>-latest/<profile>/`. Profiles: `car`, `bike`, `foot`, `motorcycle`, `truck`, `hike`, `mtb`, `racingbike`. Profile is **build-time** (different graph per profile). Java + GraphHopper 8.x JAR (installed by `install-tools.sh` or via `--jar`).
- **build-valhalla-tiles** — Valhalla tilesets at `valhalla/<region>-latest/`. No profile axis — a tileset serves every profile at query time (`auto`, `bicycle`, `pedestrian`, `truck`, `motor_scooter`, `motorcycle`, `bus`, `taxi`). Cross-region queries are native within one tileset (build a parent region for coverage across children). `valhalla_build_*` binaries (installed by `install-tools.sh`).
- **build-osrm-graph** — OSRM MLD graphs at `osrm/<region>-latest/<profile>/`. Profiles: `car`, `bicycle`, `foot`. Profile is **build-time** (like GraphHopper). Uses OSRM's shipped `.lua` profiles from `share/osrm/profiles/` (override with `--profile-file`). `osrm-extract` → `osrm-partition` → `osrm-customize`.

### Meta

- **update-all** — runs every tool's `--update-all` in dependency order: download-pbf → clip-pbf → convert-pbf-geojson → convert-pbf-shapefile → extract (all categories) → build-graphhopper-graph → build-valhalla-tiles → build-osrm-graph → build-vector-tiles → render-html-maps → download-gtfs. Safe to re-run as often as desired — each step is a no-op when nothing is stale. `UPDATE_ALL_SKIP="gtfs osrm"` skips named steps (useful when some binaries aren't installed); `UPDATE_ALL_STOP_ON_FAIL=1` aborts on first failure.

## Cache layout and sidecars

Each cached artifact has a sibling `.meta.json` sidecar — no shared
manifest file per cache_type, so N writers on N different keys never
contend. See [`agent-spec/cache-layout.agent-spec.yaml`](../../../agent-spec/cache-layout.agent-spec.yaml)
for the full contract.

```
$AFL_DATA_ROOT/           (default: /Volumes/afl_data — override via env)
├── cache/
│   └── osm/
│       ├── pbf/
│       │   └── europe/germany/berlin-latest.osm.pbf
│       │       + .meta.json sibling                  ← Geofabrik paths mirrored
│       ├── pbf-clips/
│       │   └── <name>-latest.osm.pbf + .meta.json     ← from clip-pbf
│       ├── geojson/
│       │   └── <region>-latest.geojsonseq + .meta.json
│       ├── shapefiles/
│       │   ├── <region>-latest/ (dir: points.shp, lines.shp, ...)
│       │   └── <region>-latest.meta.json              (sibling of the dir)
│       ├── <category>/         ← one cache_type per extract category
│       │   └── <region>-latest.geojsonseq + .meta.json
│       ├── graphhopper/
│       │   ├── <region>-latest/<profile>/ (dir)
│       │   └── <region>-latest/<profile>.meta.json
│       ├── valhalla/
│       │   ├── <region>-latest/ (tile pyramid 0/, 1/, 2/)
│       │   └── <region>-latest.meta.json
│       ├── osrm/
│       │   ├── <region>-latest/<profile>/ (dir)
│       │   └── <region>-latest/<profile>.meta.json
│       ├── vector_tiles/
│       │   └── <region>-latest/<source>.pmtiles + .meta.json
│       └── html/
│           ├── index.html                  ← master index (links all regions)
│           ├── <region>-latest/            (index.html, style.json)
│           └── <region>-latest.meta.json
├── cache/gtfs/feeds/<agency>-latest.zip + .meta.json
├── cache/elevation/srtm/<name>-latest.tif + .meta.json
├── staging/                 ← in-flight files (atomic-rename into cache/)
├── tmp/                     ← scratch, safe to wipe
├── _indexes/                ← lazy, advisory cache-type indexes
└── locks/                   ← per-entry fcntl locks (overwrite contention only)
```

### Backends

- `local` (default) — standard POSIX filesystem, atomic temp+rename writes, per-entry `fcntl` advisory locking when overwriting an existing entry (no global lock).
- `hdfs` — HDFS via WebHDFS (soft-imports `facetwork.runtime.storage`). Default root `/user/afl`. **No advisory locking** — single-writer semantics assumed. Rename is atomic at the namenode; directory-finalize tools (shapefile, graphhopper, valhalla, osrm) don't support HDFS yet.

Select the backend per invocation with `--backend {local,hdfs}` or globally via `AFL_STORAGE`. Override the data root with `AFL_DATA_ROOT` (or individually: `AFL_CACHE_ROOT`, `AFL_STAGING_ROOT`, `AFL_TMP_ROOT`, `AFL_INDEXES_ROOT`, `AFL_LOCKS_ROOT`).

### Manifest entry shape

Baseline — each cache type extends as needed:

```json
{
  "version": 1,
  "entries": {
    "<relative/path/in/cache-type>": {
      "relative_path": "...",
      "size_bytes": 123456789,
      "sha256": "...",
      "generated_at": "2026-04-20T14:03:22Z",
      "source": {
        "cache_type": "pbf",
        "relative_path": "europe/germany/berlin-latest.osm.pbf",
        "sha256": "...",
        "source_timestamp": "2026-04-18T21:22:02Z"
      },
      "tool": { "command": "...", "version": "..." },
      "extra": { /* cache-type-specific fields */ }
    }
  }
}
```

Every tool records:
- **Its own** SHA-256 and size (for integrity checks of the cached file).
- **Its source's** SHA-256 (for cache-validity checks — "does this cached output still reflect the current input?").
- **Tool and engine versions** where applicable (so a version bump invalidates cached outputs without per-region `--force`).

## Conventions for adding a new tool

### Python program (`<name>.py`)

- Runnable directly: `python tools/<name>.py --help` must work.
- `argparse`. Every tool exposes `--help`.
- `main()` function and `if __name__ == "__main__": main()` guard.
- Exit codes: `0` on success, non-zero on failure. Errors to `stderr`.
- Log to `stderr`; reserve `stdout` for structured output (JSON, CSV, region lists).
- Config from env vars where possible (`AFL_DATA_ROOT`, `AFL_POSTGIS_URL`, etc.). CLI flags override env vars.
- Do **not** depend on the Facetwork runtime, MongoDB, or the dashboard. Tools should run without a workflow stack. PostGIS, HDFS, external APIs are fine.
- Type hints on every function. Module docstring with usage + external deps.
- If your tool caches outputs, put the core logic in `_lib/<name>.py` so the FFL handlers can call it too. The CLI becomes a thin wrapper.

### Shell wrapper (`<name>.sh`)

- Executable; `#!/usr/bin/env bash` + `set -euo pipefail`.
- Sources `scripts/_env.sh`, activates `.venv` if present, then `exec python3` the Python tool with `"$@"`.
- `python3` explicitly (not `python`) for cross-machine Python 2/3 safety.
- No argument parsing in the wrapper — that belongs in the Python program.

### Cache conventions for new data types

- Pick a short, plural-noun-ish cache type name (`shapefiles`, `parks`, `vector_tiles`, `elevation`).
- Use per-entry `.meta.json` sidecars (no shared manifest). See `agent-spec/cache-layout.agent-spec.yaml`.
- Mirror upstream path hierarchy where there is one (Geofabrik: `europe/germany/berlin-...`).
- Stage downloads / builds under `$AFL_STAGING_ROOT/<namespace>/<cache_type>/` and finalize via `storage.finalize_from_local` / `finalize_dir_from_local`. Don't write partial files to the destination.
- Record source-lineage SHA + tool version in every entry so cache validity is self-describing.
- Expose a `--list`, `--list-missing`, `--update-all`, `--force`, `--dry-run` surface. If your tool has an axis (profile, category, source), match the existing tools' flag shape.

### What does **not** belong here

- Workflow steps, event facets, or handler logic → `handlers/`.
- Scripts that manage the Facetwork stack itself (runners, Docker, databases) → repo-root `scripts/`.
- Test fixtures or pytest helpers → `tests/`.
- Anything that must run inside a Facetwork task — if it needs the runtime, it's a handler, not a tool. (But the handler can call your tool's library module.)

## Typical workflows

**Bootstrap a new machine for the Liechtenstein example:**

```bash
./tools/install-tools.sh
./tools/download-pbf.sh europe/liechtenstein
./tools/update-all.sh UPDATE_ALL_SKIP="gtfs"
```

**Rebuild everything after Geofabrik updates the German data:**

```bash
./tools/update-all.sh
# download-pbf detects the upstream MD5 change on German regions, re-downloads;
# every downstream tool sees the SHA flip and refreshes only those regions.
```

**Custom-polygon region:**

```bash
# Define a polygon covering a watershed
./tools/clip-pbf.sh --source europe/germany \
    --polygon my-watershed.geojson \
    --name watershed-example

# Downstream tools now see it as region "clips/watershed-example"
./tools/extract.sh water clips/watershed-example
./tools/build-graphhopper-graph.sh --profile bike clips/watershed-example
```

**Web-map tile + viewer pipeline for Liechtenstein:**

```bash
./tools/download-pbf.sh europe/liechtenstein
./tools/extract.sh --extract-all-categories europe/liechtenstein
./tools/build-vector-tiles.sh --all-sources europe/liechtenstein
./tools/render-html-maps.sh europe/liechtenstein

# Serve everything and open the result in a browser:
python -m http.server --directory "$AFL_CACHE_ROOT/osm" 8000 &
open http://localhost:8000/html/           # the master index
# → click into europe/liechtenstein-latest/
```

**Multi-profile routing across a bigger region:**

```bash
./tools/download-pbf.sh europe/germany           # country-level PBF
./tools/build-graphhopper-graph.sh --all-profiles europe/germany
./tools/build-valhalla-tiles.sh europe/germany   # no profile axis
./tools/build-osrm-graph.sh --all-profiles europe/germany
```

**Inspect what's cached / what needs work:**

```bash
./tools/download-pbf.sh --list-missing --all-under europe/germany   # what's left to download
./tools/extract.sh --update-all --extract-all-categories --dry-run  # what would rebuild
./tools/build-valhalla-tiles.sh --list-missing --all                 # which tilesets need building
```
