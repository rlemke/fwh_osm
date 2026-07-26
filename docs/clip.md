# Clip — cheap spatial sub-setting of a cached PBF

**Namespace(s):** `osm.Clip` · `osm.Clip.workflows` ·
**FFL:** `src/osm_geocoder/handlers/clip/ffl/{osmclip,osmclip_workflows}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/clip/clip_handlers.py` ·
**Tools:** `tools/_osm_tools/pbf_clip.py`

## Overview

`osm.Clip` is the "cheap sub-region" verb of the OSM toolchain — the Source-layer
analogue of `osmium extract`, `osmconvert -b`, `ogr2ogr`, or an Overpass bbox
query. It takes a region handle (`OSMCache`) plus a bounding box or polygon and
produces a **new, much smaller `OSMCache`** over the clipped PBF.

Its value is compositional: every `Extract*` / Spatial handler reads the PBF from
`cache.path`, so a clipped `OSMCache` flows straight into `ExtractCategory` exactly
like a full-region cache. Clip a 1.2 GB state down to a metro bbox first, and the
downstream extract + distance work runs on megabytes instead of gigabytes — this is
what makes continental-scale spatial queries tractable.

## How it works

The handler (`clip_handlers.py`) is a thin facet boundary over the `pbf_clip` tool:

1. **Derive the source region** from the incoming `OSMCache` (`_source_region`).
2. **Mint a deterministic clip name** so the operation is idempotent across runs:
   - bbox → `<leaf>_bbox_<w>_<s>_<e>_<n>` (`_bbox_clip_name`),
   - polygon → `<leaf>_poly_<sha256-of-polygon-content>` (`_polygon_clip_name`),
     keyed on the polygon file's content so identical polygons dedupe.
3. **Output-cache short-circuit** — keyed on `(source PBF, bbox/polygon)`; a hit
   avoids even spawning osmium.
4. **Run `osmium extract` with a heartbeat** — `clip_tool.clip_pbf` shells out to
   `osmium extract --overwrite --output-format pbf`, staging to a `.staging` file
   and finalizing into the `pbf-clips` cache. Because the osmium subprocess blocks
   and can't heartbeat from inside, `_run_with_heartbeat` pumps the task heartbeat
   every 30 s (`_HEARTBEAT_INTERVAL`) so the lease (default 5 min) survives a
   multi-minute clip.
5. **Return a new `OSMCache`** over the clipped PBF (`_clipped_cache`), so
   downstream `ExtractCategory` / Spatial steps compose on the smaller region.

The clip's cache validity requires both the source PBF's SHA-256 to still match
what the clip sidecar recorded **and** the clip spec (bbox or polygon content) to
match; bumping either triggers a re-clip.

The worked composition `osm.Clip.workflows.ClipAndExtract`
(`osmclip_workflows.ffl`) shows the payoff: `CacheRegion` → `ClipByBBox` →
`ExtractCategory` on the clip — the default clips California to the SF Bay Area and
extracts amenities in seconds on a ~tens-of-MB clip versus a full-state scan.

## Fan-out

Single region per clip — one leaf, no `foreach` inside the clip facets. The facet
docstring is explicit: "One region per leaf; fan out across clips with
`andThen foreach` at the workflow." Clip is a building block, so parallelism comes
from a caller that fans a `foreach` over multiple bboxes/regions and clips each
independently; the clip itself is one blocking osmium extract on the claiming host.

## Filtering & attributes

**Geometric, not tag-based.** Clip is a spatial subset — it keeps every OSM feature
(all tags) whose geometry falls within the bbox or polygon; it does no attribute
filtering. Tag filtering is a downstream step (feed the clipped `OSMCache` into
`ExtractCategory` or the `FilterGeoJSON*` facets). The clip spec is purely
geometric:

- **bbox** — `west, south, east, north` in WGS84 degrees (`ClipByBBox`).
- **polygon** — a path to a GeoJSON or osmium `.poly` file (`ClipByPolygon`).

## External libraries / binaries

- **`osmium` (osmium-tool binary)** — `osmium extract` does the spatial clip; a
  **binary** dependency (`osmium_bin`, default `osmium`, checked via
  `osmium --version`). This is the only heavy dependency.
- **stdlib** (`hashlib` for the polygon-content SHA and cache keys, `threading` for
  the heartbeat pump, `subprocess` in the tool). No pip geometry library on the clip
  path.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.Clip.ClipByBBox(cache, west, south, east, north)` | event | Clip a cached region to a WGS84 bbox → new `OSMCache`; idempotent per (source PBF, bbox). `Timeout(30min)`, `Effect(external)` |
| `osm.Clip.ClipByPolygon(cache, polygon_path)` | event | Clip to a GeoJSON/`.poly` polygon → new `OSMCache`; idempotent per (source PBF, polygon content). `Timeout(30min)`, `Effect(external)` |
| `osm.Clip.workflows.ClipAndExtract(region, west, south, east, north, category)` | workflow | `CacheRegion` → `ClipByBBox` → `ExtractCategory` — the cheap metro-scale query demonstration |

## Cache / output

- **Cache namespace:** `cache/osm/pbf-clips/<name>-latest.osm.pbf` — a **sibling**
  cache_type of `pbf/` (not nested under it), so every cache_type root stays pure
  (per `agent-spec/cache-layout.agent-spec.yaml`). Each clip has its own validity
  sidecar recording the source PBF SHA + the clip spec. The clip's region key is
  flat (`<name>`), but the on-disk artifact is named `<name>-latest.osm.pbf` so it
  reads like any other PBF to downstream tools.
- **Output:** a new `OSMCache` (a smaller `.osm.pbf`, not a rendered artifact) —
  the input to the next extract/spatial step. On `FW_STORAGE=s3` the clip lands in
  MinIO under the same cache root, staged locally then finalized.

## Gotchas & notes

- **Heartbeat is load-bearing.** `osmium extract` blocks and can't heartbeat from
  inside; without the 30 s pump a multi-minute clip would let the task lease expire
  and get reclaimed mid-clip (the CombinedScan lesson applied here).
- **Idempotency is content-keyed.** A polygon clip is keyed on the polygon file's
  SHA-256, so editing the polygon (even to the same path) correctly invalidates the
  clip; a bbox clip is keyed on the rounded coordinates.
- **Double caching.** Both the output-cache short-circuit (keyed on source+spec) and
  the tool's own sidecar cache guard against re-running osmium — the former avoids
  even spawning the process on a hit.
- **Clip first, then extract.** The whole point is to shrink the PBF before the
  expensive extract/distance work; running `ExtractCategory` on a full state and
  filtering after defeats the purpose.
- **Source SHA sensitivity.** If the underlying region PBF is refreshed (new SHA),
  every clip derived from it is invalidated and re-clips on next use.

## Related specs

- [source-adapters](source-adapters.md) — the clipped `OSMCache` feeds
  `osm.Source.PBF.ExtractCategory` (and the other extractors) unchanged.
- [cache-and-download](cache-and-download.md) — produces the full-region `OSMCache`
  that clip subsets.
- [planet-extraction](planet-extraction.md) — the region-scale extraction split
  that clip complements at metro scale (both are `osmium extract` passes).
