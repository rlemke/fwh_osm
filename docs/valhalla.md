# Valhalla — profile-agnostic tile builds

**Namespace:** `osm.ops.Valhalla` ·
**FFL:** `src/osm_geocoder/handlers/valhalla/ffl/osmvalhalla.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/valhalla/valhalla_handlers.py` ·
**Tools:** `tools/_osm_tools/valhalla_build.py`

## Overview

`osm.ops.Valhalla` **builds and manages Valhalla routing tilesets** from a cached
OSM PBF. It is the tile-production sibling of the GraphHopper graph builder: take an
`OSMCache`, run the Valhalla build binaries, and cache the resulting tile pyramid so a
Valhalla daemon can serve it.

The defining property — and the reason it is simpler than the GraphHopper builder —
is that **Valhalla tiles are profile-agnostic**. One build serves
`auto`/`bicycle`/`pedestrian`/`truck`/`motor_scooter`/`motorcycle`/`bus`/`taxi` at
*query time*, so there is no per-profile directory: one tileset per region, full stop.

This namespace only *produces* tiles. The tiles then feed a `valhalla_service`
daemon that the **routing** adapter (`osm.Routing.Valhalla`, see [routing](routing.md))
queries over HTTP. The two are separate concerns sharing the "Valhalla" name.

## How it works

`valhalla_handlers.py` is a thin adapter over `tools/_osm_tools/valhalla_build.py`, so
the `build-valhalla-tiles` CLI and the handlers share one code path and one cache
layout. `BuildTiles` resolves the region from the cache's Geofabrik URL, then, under a
per-region lock, subprocesses the two Valhalla binaries:

1. `valhalla_build_config --mjolnir-tile-dir <tiles> …` → a `valhalla.json` config.
2. `valhalla_build_tiles -c valhalla.json <region>.osm.pbf` → the `.gph` tile pyramid.

It stages locally and finalizes into `cache/osm/valhalla/<region>-latest/` with a
sibling sidecar. **Cache validity requires both** the source PBF's SHA-256 to still
match the sidecar **and** the recorded `valhalla_version` (3.5) to match the current
constant — otherwise it rebuilds. `recreate=true` forces a rebuild. `ValidateTiles`
confirms the tileset still has at least one `.gph` file and returns the tile count;
`CleanTiles` deletes the tileset directory.

## Fan-out

**Single-task per region** — one `BuildTiles` task builds an entire region's tileset
on the host that claims it (the tile build is a monolithic pass over the PBF that
can't be sharded). `BuildTilesBatch` is the FFL-batch-loop variant of the same
handler (identical logic), for driving many regions from a `foreach` at the workflow
level; fan-out across regions is expressed there, not inside a build.

## Filtering & attributes

**No tag filtering** — the tile build is a geometric/topological transform of the full
PBF road network. Which edges are usable for a given mode is decided by Valhalla's
*costing model* at query time, not by pruning the input here.

## External libraries / binaries

- **Valhalla build binaries** — `valhalla_build_config` and `valhalla_build_tiles`
  (`brew install valhalla` on macOS; build-from-source or the
  `ghcr.io/valhalla/valhalla` Docker image elsewhere). These are **binary**
  dependencies, not pip, and Valhalla is *not* on Homebrew for every platform (a
  documented setup friction).
- Python side: `subprocess`, plus the shared `_osm_tools` `sidecar`/`storage`
  libraries for the cache layout and finalize-from-local staging.

Pinned version: `VALHALLA_VERSION = "3.5"`, default build timeout 3600 s.

## Facets & workflows

All facets carry `with Effect(kind="external") with Cost(tier="expensive")` and
return/consume the `osm.types.ValhallaCache` schema (`tileDir`, `tileCount`, `size`, …).

| Facet | Kind | Purpose |
|---|---|---|
| `BuildTiles(cache, recreate=false)` | event | Build (or return cached) Valhalla tileset for a region |
| `BuildTilesBatch(cache, recreate=false)` | event | Bulk-mode equivalent for FFL batch loops |
| `ValidateTiles(tiles)` | event | Confirm the tileset exists; return `valid` + `tileCount` |
| `CleanTiles(tiles)` | event | Delete a built tileset directory |

## Cache / output

The tileset is the artifact: `cache/osm/valhalla/<region>-latest/` (the `.gph` tile
pyramid) + a sidecar recording source SHA-256, `valhalla_version`, tile count, and
per-level tile counts. Keyed on the source PBF + version. Built once per region, then
shared — but note tiles are a directory tree, so the same local-backend caveat as
GraphHopper graphs applies to on-disk validation.

## Gotchas & notes

- **One build, all modes** — do not build per profile; profiles are a query-time
  costing choice in Valhalla. This is the primary difference from the GraphHopper
  graph builder (which *is* per-profile).
- **Binaries must be installed** — `BuildTiles` raises a clear `BuildError` if
  `valhalla_build_config`/`valhalla_build_tiles` aren't on `PATH`. Valhalla's
  cross-platform install is the friction point.
- **Build vs. route** — `osm.ops.Valhalla` (here) makes tiles; `osm.Routing.Valhalla`
  ([routing](routing.md)) queries a running Valhalla daemon loaded with those tiles.
  You need both to route: build the tiles, run `valhalla_service`, then route.
- **Version match** — a tileset built by a different Valhalla version is treated as a
  cache miss and rebuilt.

## Related specs

- [routing](routing.md) — the `osm.Routing.Valhalla` HTTP adapter these tiles serve.
- [graphhopper](graphhopper.md) — the parallel (per-profile) graph builder.
- [cache-and-download](cache-and-download.md) — the PBF cache that feeds the build.
- [planet-extraction](planet-extraction.md) — where the source PBFs come from.
