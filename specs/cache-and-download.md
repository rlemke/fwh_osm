# Cache & Download — the consumer side of the self-hosted "Geofabrik"

**Namespace(s):** `osm.cache` · `osm.ops` · `osm.Region` · `osm.types` ·
**FFL:** `src/osm_geocoder/handlers/cache/ffl/*.ffl`
(`osmcache_download.ffl`, `osmcache_refresh.ffl`, `osmcache_update.ffl`,
`osmops.ffl`, `osmregion.ffl`, `osmtypes.ffl`, the per-continent
`osmafrica/osmasia/…` + `osmworld/osmcontinents` fan-in facets) ·
**Handlers:** `src/osm_geocoder/handlers/cache/{region_handlers,update_handlers,cache_handlers}.py` ·
**Tools:** `tools/_osm_tools/{pbf_download,download_gate}.py`

## Overview

This is the **consumer** side of the OSM data plane: it turns a region request
(a Geofabrik path like `north-america/us/california`, or a friendly name like
`California`) into a local `.osm.pbf` on disk that every downstream extractor
reads. Where [planet-extraction](planet-extraction.md) *produces* the
Geofabrik-shaped tree of per-region extracts, this feature *fetches* from it —
`osm.ops.CacheRegion` / `osm.cache.Download` is the first step of essentially
every region facet and every composed analysis workflow.

The seam between the two is a single URL. `pbf_download` builds every extract,
`.md5`, and replication-diff URL from `FW_GEOFABRIK_BASE_URL` (default
`https://download.geofabrik.de`), so pointing that env at our own published tree
(`http://afl-minio:9000/osm-extracts`) reroutes all downloads to us with no code
change. The on-disk cache layout is deliberately provider-agnostic
(`<region>-latest.osm.pbf`) — only the remote URL changes.

## How it works

A region request resolves and downloads in these stages:

1. **Resolve** — a friendly name (no `/`) is run through `region_resolver`
   (`osm.Region.ResolveRegion[s]`) into a typed `Region` whose
   `geofabrik_path` is the authoritative download key. A path with a `/` is used
   directly. `CacheRegion` resolves the path *back* into a `Region` too, so the
   resulting `OSMCache.region` is always populated regardless of entry point.
2. **URL selection** — `resolve_extract_url(region)` returns
   `{base}/<key>-latest.osm.pbf`. Default provider is Geofabrik; under
   `FW_OSM_EXTRACT_PROVIDER=osmfr` it downloads from OpenStreetMap France for
   regions osmfr covers (probed + memoized per process) and falls back to
   Geofabrik for the rest.
3. **Freshness gate** — `download_region` decides whether to touch the network at
   all (see below), then does a conditional GET: it fetches the upstream `.md5`
   and, if unchanged from the sidecar, keeps the cached file; if changed (or
   forced) it re-downloads.
4. **Download** — a resumable streamed GET (`_download_resumable`) writes to a
   `.staging` temp file, verifies MD5, and finalizes into
   `cache/osm/pbf/<key>-latest.osm.pbf` with a `.meta.json` sidecar. Multi-GB
   transfers that drop mid-stream are resumed with `Range` requests (up to
   `FW_OSM_RESUME_MAX_ATTEMPTS`, default 50).
5. **Return** — `to_osm_cache` wraps the result as an `OSMCache`
   (`{region, url, path, date, size, wasInCache}`) that flows into the extractors.

### The download gate (freshness policy)

`cache_policy` (on `osm.cache.Download`) / `_download_kwargs` map to
`download_region` flags:

- `prefer_cache` (Download's default) — use a cached PBF **as-is** if present, no
  Geofabrik call. Geofabrik rebuilds every extract daily with a fresh MD5, so
  revalidating would re-pull gigabytes for effectively-unchanged data; cache-backed
  runs (MinIO) should not depend on egress.
- `auto` (`CacheRegion`'s default) — defer to `FW_OSM_USE_CACHE_IF_PRESENT`.
- `refresh` — force a full fresh download (used by the refresh workflows).
- `revalidate` / `strict` — always conditional-GET against the remote.

### Region resolution & expansion

`osm.Region` is the resolver surface:

- `ResolveRegion(name)` / `ResolveRegions(names)` — friendly names, qualifier
  suffixes (`"Georgia, US"` → US state, `"Georgia (country)"` → country),
  canonical paths (pass-through), and named features (`"Alps"` → its 7 constituent
  countries) all resolve to typed `Region` records. `prefer_continent` is a
  tiebreaker; `strict` fails on any unresolved/ambiguous name, else diagnostics
  are returned for the caller to inspect.
- `expand = "subregions"` performs **hierarchical expansion** — a parent name
  becomes its finest-grained Geofabrik leaf extracts (`["us"]` → 51 states;
  `["north-america"]` → all US states + Canadian provinces + Mexico + Greenland).
  This is what lets a single name drive a whole-continent fan-out without listing
  every subregion.
- `ListRegions(parent_canonical, level, continent)` — enumerate the known catalog,
  filtered (e.g. `level="continent"`, or `parent_canonical="north-america/canada"`
  for all provinces).

The static name → Geofabrik-path map lives in `cache_handlers.py`
(`REGION_REGISTRY`, ~10 continent/country/subnational buckets). The per-country
facets (`osm.cache.Africa.Algeria()`, `osm.cache.UnitedStates.California()`, …)
are **pure** `andThen` facets that expand inline to a single `CacheRegion` call —
they never emit their own event task.

### Cache refresh & update

Two families keep the cache current:

- **`RefreshAllCaches` / `RefreshRegionCaches`** (`osmcache_refresh.ffl`) —
  `ListCachedRegions` enumerates what's cached (no network), then a `foreach`
  re-downloads each region **fresh** (`cache_policy = "refresh"`). This is the
  heavy path: a full multi-GB re-pull per region.
- **`UpdateRegion` / `UpdateAllCaches` / `UpdateRegionCaches`**
  (`osmcache_update.ffl`) — the light path. Reads the cached PBF's replication
  sequence from its header, fetches + applies the day's `.osc.gz` diffs
  (`method="diff"`, KB–MB per region), and falls back to a full download
  (`method="full"`) only when there's no replication baseline or the region is too
  far behind the `max_diff_mb` budget. `method="current"` means already up to date.
  This is the rate-limit-friendly "update all caches" — a fleet-wide full refresh
  once got the shared egress IP blocked.

> `UpdateRegion` is scaffolded but **NOT YET REGISTERED** — `update_handlers.py`
> notes it is pending Gate-B review of the `pbf_update` replication seam.

## Fan-out

Both the refresh and update workflows fan out **per region** via
`andThen foreach r in $.regions` over the list `ListCachedRegions` /
`ResolveRegions(expand="subregions")` produces — one download/update task per
leaf, distributed across the runner fleet, with list-typed yield-merge
aggregating the resulting paths. `osm.examples.DownloadAcrossRegions`
(`osmcrossregion.ffl`) is the canonical demonstration: N regions of any mixed
admin level → N parallel downloads → one `[OSMCache]` list.

The legacy per-continent fan-in facets (`osm.World.cache.World()`,
`ContinentsIndividually`) instead list every country call inline and merge with
`++`; these predate the resolver-driven `foreach` pattern.

## Filtering & attributes

None — a PBF download is a **whole-extract transfer**, not a tag filter. The only
tag-adjacent logic is region *resolution* (matching a name to a Geofabrik path
and, for `ListRegions`, filtering the catalog by `level`/`continent`). All OSM
tag filtering happens later, in the source/extractor handlers.

## External libraries / binaries

- **`requests`** (pip, optional) — bulk streamed/resumable download; falls back to
  stdlib `urllib` when absent.
- **`pymongo`** (pip) — backs the fleet-wide `download_gate` semaphore. A **no-op**
  when `FW_MONGODB_URL` is unset or pymongo is missing (local/test runs never block).
- **stdlib only** for hashing (`hashlib` MD5 verification), HTTP conditional GETs,
  and per-region `threading.Lock` de-duplication.
- No osmium/binary dependency on the download path — the cache layer just moves
  bytes; osmium enters at the extractor/clip layer.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `osm.ops.CacheRegion(region: String)` | event | Download/reuse a PBF by path or friendly name — the universal first step. `Effect(external)`, `Cost(expensive)` |
| `osm.cache.Download(region: Region, cache_policy)` | event | Region-driven download; idempotent (`wasInCache`). `Effect(external)`, `Cost(expensive)` |
| `osm.cache.ListCachedRegions()` | event | Regions present in the local cache, no network. `Effect(io)`, `Cost(cheap)` |
| `osm.cache.UpdateRegion(region, max_diff_mb)` | event | Diff-based update (full-download fallback); returns `method`+`applied_mb`. *Not yet registered* |
| `osm.Region.ResolveRegion(name, prefer_continent)` | event | Name → single `Region`. `Effect(pure)`, `Cost(cheap)` |
| `osm.Region.ResolveRegions(names, strict, expand)` | event | Batch resolve + hierarchical expansion + diagnostics. `Effect(pure)`, `Cost(cheap)` |
| `osm.Region.ListRegions(parent_canonical, level, continent)` | event | Enumerate the region catalog, filtered. `Effect(pure)`, `Cost(cheap)` |
| `osm.cache.<Continent>.<Country>()` | pure | Inline one-line wrapper → `CacheRegion` (no event task) |
| `RefreshAllCaches` / `RefreshRegionCaches` | workflow | Force full re-download of cached / named regions (heavy) |
| `UpdateAllCaches` / `UpdateRegionCaches` | workflow | Diff-based update of cached / named regions (light) |
| `osm.examples.DownloadAcrossRegions(region_names)` | workflow | Canonical mixed-level parallel download fan-out |

Core schemas (`osmtypes.ffl`): `Region` (`query/name/canonical/level/level_label/
parent_canonical/continent/geofabrik_path`) and `OSMCache`
(`region/url/path/date/size/wasInCache`).

## Cache / output

- **Cache namespace:** `cache/osm/pbf/<region>-latest.osm.pbf` under
  `$FW_CACHE_ROOT` (default `cache`, resolved via the storage backend —
  `s3://afl-cache` on MinIO), each with a `.meta.json` sidecar recording the
  upstream URL, MD5, size, and last-modified for freshness checks.
- **Output:** the `OSMCache` handle (not a rendered artifact) — the input to every
  extractor. On a fleet with `FW_STORAGE=s3`, `path` is an `s3://afl-cache/...`
  URI any runner can resolve; extractors localize before running osmium.
- Concurrency across the fleet is capped by `download_gate` (Mongo semaphore,
  `FW_OSM_DOWNLOAD_CONCURRENCY`, default 3) so a wide refresh can't open dozens of
  multi-GB GETs from the shared egress IP at once.

## Gotchas & notes

- **`prefer_cache` never revalidates.** By design — a present-but-stale extract is
  used as-is so runs don't depend on egress. Use `refresh`/`revalidate` or the
  Update workflows to actually pull fresher data.
- **Full refresh got the IP blocked.** A fleet-wide `RefreshAllCaches` fans out
  hundreds of simultaneous multi-GB GETs from one egress IP → Geofabrik ban. Prefer
  the diff-based `UpdateAllCaches`; the `download_gate` semaphore is the safety cap.
- **osmfr ≠ Geofabrik clip.** An osmfr-sourced extract embeds *osmfr's* replication
  header, so its deltas follow osmfr — you cannot cross-apply osmfr diffs onto a
  Geofabrik baseline. Provider is chosen at download time for exactly this reason.
- **osmfr coverage is a subset.** OSM France lacks many regions (most US states,
  several countries); `_osmfr_covers` probes per region and silently falls back to
  Geofabrik, so `FW_OSM_EXTRACT_PROVIDER=osmfr` does not mean "everything from osmfr".
- **Missing `.md5`.** Some extracts publish no `.md5` (404); the download is
  accepted without hash verification (opt-in tolerance via `FW_OSM_TOLERATE_MD5`
  for the brief window where Geofabrik's `.md5` and `.pbf` are inconsistent).
- **`UpdateRegion` is unregistered** pending review — wire
  `register_update_handlers` into the cache registration before relying on it.

## Related specs

- [planet-extraction](planet-extraction.md) — the producer side; set
  `FW_GEOFABRIK_BASE_URL` at its published tree and this consumer fetches from us.
- [source-adapters](source-adapters.md) — the PBF/PostGIS/GeoJSON extractors that
  read the `OSMCache` this feature produces.
- [osm-changes](osm-changes.md) — reuses the same replication machinery to *surface*
  diffs instead of applying them.
- [clip](clip.md) — subsets a cached PBF to a smaller region before extraction.
