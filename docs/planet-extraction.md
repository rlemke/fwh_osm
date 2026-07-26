# Planet Extraction — the self-hosted "Geofabrik"

**Namespace:** `osm.planet` ·
**FFL:** `src/osm_geocoder/handlers/planet/ffl/osmplanet.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/planet/planet_handlers.py` ·
**Tools:** `tools/_osm_tools/{planet_fetch,planet_bootstrap,polygon_fetch,tiger_fetch,boundary_gen}.py` ·
**Tests:** `tests/mocked/py/test_planet_pipeline.py`

> This is the flagship feature and the deepest write-up in `docs/`. A companion
> research paper is in the framework repo at
> `docs/thesis/paper-geofabrik-replacement.md`.

## Overview

Facetwork's OSM domain resolves a **region key** (`europe/france`,
`north-america/us/california`) to a `.osm.pbf`, caches it, and runs handlers over
it. The default remote for those extracts was `download.geofabrik.de`. When
Geofabrik rate-limited and then **IP-banned the fleet's shared egress**, every OSM
workflow broke at once — a third party we do not control had become a hard
dependency on the critical path.

`osm.planet` makes us **our own Geofabrik**: download the planet once, split it
into a Geofabrik-compatible tree of per-region extracts, publish them to our own
object store, and keep them current from OSM replication. The integration surface
is a **single environment variable** — set `FW_GEOFABRIK_BASE_URL` at our published
tree and every existing consumer (the downloader, the delta-update path, the cache)
keeps working unchanged, because we publish exactly Geofabrik's two URL shapes:

```
<base>/<region>-latest.osm.pbf        # the extract
<base>/<region>-updates/state.txt     # its replication state (for deltas)
```

The bucket currently holds **8 continents, 199 countries**, and sub-national sets:
German Länder (16) + Kreise (~400), US states (51), US counties (~3,143), Mexican
states (32), Canadian provinces (13) — all anonymously downloadable and
delta-updatable.

## How it works

The pipeline is four stages, each an `osm.planet` event facet, composable into
end-to-end workflows:

1. **`DownloadPlanet`** — `planet_fetch.fetch_planet`: resumable `curl -C -` of
   `planet-latest.osm.pbf` (~87 GB) from `planet.openstreetmap.org` (Geofabrik is
   banned), md5-verified. This is the master everything derives from.
2. **`UpdatePlanet`** — `planet_fetch.update_planet`: brings the planet current by
   applying OSM **replication diffs** (pyosmium `ReplicationServer.apply_diffs_to_file`,
   deriving the start sequence from the dump's timestamp). Degrades to a no-op
   status when replication is unreachable rather than failing.
3. **Boundaries** — either `DownloadPolygons` (ready-made osmfr `.poly` / TIGER
   GeoJSON) or `GenerateRegionPolygons` (self-generated from OSM admin relations,
   §"Boundaries"). Produces one polygon per region.
4. **`ExtractRegions`** — `planet_bootstrap.bootstrap[_batched]`: an `osmium
   extract -c <config>` pass clips the source into per-region PBFs, then each output
   is stamped with **our** `osmosis_replication_*` header (via `osmium cat
   --output-header`) so the delta path follows **our** server, and published as the
   Geofabrik-style layout. Header round-trip is verified per region.

`PublishExtracts` uploads the tree to the S3/MinIO bucket (anonymous-read).
The composed workflow **`BuildPlanetExtracts`** runs 1→4 end-to-end on a schedule
to keep the self-hosted "Geofabrik" fresh.

### Delta upkeep, not re-download

The replication-header indirection is what makes this a *self-updating* replacement
rather than a frozen mirror. A region re-downloaded from us carries our replication
URL, so `update-delta` applies OSM diffs against it — the marginal cost of freshness
is proportional to *change*, not to extract size. This is the core of "cheaper to
upkeep ourselves": the public model is periodic full re-downloads; ours is deltas.

## Boundaries — self-generation and the fallback hierarchy

The interesting decision was to **generate our own region boundaries** rather than
depend on anyone's `.poly` files. `boundary_gen.generate_polygons` filters
`boundary=administrative` relations at an `admin_level` out of a PBF
(`osmium tags-filter r/admin_level=N`) and assembles them to polygons
(`osmium export -f geojsonseq --geometry-types=polygon`). This is self-contained
and universal — but raw-OSM admin geometry has a long tail of assembly failures,
resolved by a layered fallback:

- **`osmium export` silently drops** boundaries it cannot close into a valid
  polygon. `simple`-strategy extraction *clips a few edge nodes*, which destroys
  the largest/most-coastal boundaries (Ontario vanished over **3 clipped nodes**).
  **Fix:** extract with **`complete_ways`** (keeps every node of a referenced way,
  even outside the region — also what makes our extracts genuinely self-contained).
- Some boundaries fail even with complete geometry (a maritime way outside the
  country poly; Nova Scotia's boundary is **18 nested sub-relations** osmium won't
  recurse into). **Fix:** fill only the gap from a **region- and level-aware
  ready-made poly provider** (`polygon_fetch.fetch_country_subregions`):
  - **osmfr** `/polygons/` — worldwide, but no US state polys and no county tree;
  - **US Census TIGER** shapefiles — US states (`admin_level=4`) and US counties
    (`admin_level=6`), the only county provider for the US.
- **Keying:** sub-country units are keyed under the **source country** from
  `source_region` (not an ISO→continent lookup, which mis-filed Mexico under
  `central-america`). County-level units (German Kreise, US counties) carry no
  ISO 3166-2, so they are kept and keyed under the known country; US counties are
  **nested** `north-america/us/<state>/<county>` (collision-free — ~30 states have
  a "Washington County"). County-type suffixes (`County`/`Parish`/`Borough`/…) are
  stripped from **both** self-gen and TIGER names so the two sources dedupe to one
  bare slug per county.

The general lesson: robust admin-boundary assembly from raw OSM is genuinely hard —
which is *why* Geofabrik/osmfr exist — so the pragmatic answer is a small fallback
hierarchy (cheap universal method under a robust ready-made source), not one
all-conquering algorithm.

## Fan-out

Two levels of granularity, one fan-out:

- **`BuildAdminSet`** is **single-atomic** — one event task downloads its own
  source, generates, extracts, and publishes an entire admin set (e.g. a country's
  states) *all on the one host that claims it*. This exists because a multi-step
  workflow handing local file paths between steps breaks on the fleet's per-host
  scratch disk; the single-atomic unit needs no cross-host handoff and is therefore
  a **relocatable job** (it has survived a mid-run host migration).
- **`BuildAdminFanout`** is the **fleet fan-out**: a `foreach` over the
  direct-child extracts (`ListExtracts` enumerates them) that spawns **one
  `BuildAdminSet` task per child**, distributed independently. US counties fan out
  **per state** — 51 `BuildAdminSet(admin_level=6)` tasks — so N states extract
  concurrently across the runner fleet and wall-clock ≈ the *slowest single state*,
  not the sum. Observed live at **8 states across 8 hosts at once**; adding runners
  adds parallelism directly. Fine-grained regions (counties) are individually cheap
  and embarrassingly parallel — a granularity the public source does not even offer.

## Work effort as a measurable, routable cost

Because each region is a self-contained job, its **processing cost is a property of
the shape** and is both measured and acted on — the architecturally novel part:

- **Adaptive memory-budgeted batching** (`bootstrap_batched`, `batch_size=0`).
  `osmium extract` holds a ~1.5 GB node-id bitmap per region in a pass, and a dense
  region can approach the container's memory ceiling alone. Rather than guess a
  fixed batch, the batcher (1) **detects the real ceiling** — cgroup `memory.max`,
  else `/proc/meminfo` `MemTotal` (which reveals the Docker-VM's true ~14 GiB, not
  the host's 32–64 GB); (2) sizes each pass under `ceiling × 0.7` from a learned
  per-region cost; (3) **measures** the pass's peak RSS (`psutil`) and updates the
  estimate (EWMA, persisted to a sidecar so the next run starts calibrated); (4)
  **self-heals on OOM** — an osmium SIGKILL (`-9`) raises a recoverable error and
  the same regions re-run in a smaller pass instead of dead-lettering.
- **Incremental publish + resume.** Each pass's extracts upload immediately
  (durable progress); on entry a job lists what is already published under its
  prefix and skips it, so a large set **converges across retries** regardless of
  per-attempt timeouts (German counties: 84 → 383 across five retries, an OOM
  series, a host migration, and MinIO load spikes — the count only ever rising).
- **Per-task scratch isolation.** Each `BuildAdminSet` gets a UUID-suffixed scratch
  dir, so several tasks on one host (a fan-out lands 2+ per host) don't clobber each
  other's in-flight download — a bug the fan-out surfaced.

## Filtering & attributes

Planet extraction is a **geometric split, not a tag filter** — it clips the full
OSM data (all tags) within a region polygon; downstream handlers do the tag
filtering. The one place it inspects tags is **boundary generation**, which selects
`boundary=administrative` relations at a given `admin_level` (2=country,
4=state/province, 6=county), keyed by `ISO3166-1`/`ISO3166-2` where present.

## External libraries / binaries

- **`osmium` (osmium-tool binary)** — `extract`, `tags-filter`, `export`, `cat`,
  `fileinfo`. The workhorse; a **binary** dependency.
- **`pyosmium`** (pip `osmium`) — replication headers + `ReplicationServer` for
  delta updates.
- **`pyshp`** (`shapefile`) — reading Census TIGER shapefiles → GeoJSON.
- **`boto3`** (`.[s3]` extra) — MinIO/S3 upload/download.
- **`psutil`** — per-pass peak-RSS measurement for the adaptive batcher.
- **`requests`** — resumable planet download.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `DownloadPlanet` | event | Resumable, md5-verified planet download |
| `UpdatePlanet` | event | Apply replication diffs (delta upkeep) |
| `DownloadPolygons` | event | Ready-made osmfr/TIGER region polys |
| `GenerateRegionPolygons` | event | Self-generate polys from OSM admin boundaries |
| `ExtractRegions` | event | Split source → per-region PBFs, stamp + publish |
| `PublishExtracts` | event | Upload the tree to the object store |
| `BuildAdminSet` | event | **Single-atomic** admin-set build (download→gen→extract→publish) |
| `ListExtracts` | event | Direct-child extract keys under a prefix (fan-out driver) |
| `BuildPlanetExtracts` | workflow | End-to-end planet → published tree |
| `BuildAdminSetWorkflow` | workflow | Distributed single-atomic admin set |
| `BuildAdminFanout` | workflow | **Fan-out**: one `BuildAdminSet` per child, in parallel |

Every facet carries `with Effect(kind="io"/"external")` and `with Cost(tier=...)`
mixins so the capability index (`fw_capabilities`) surfaces cost.

## Cache / output

Output is the Geofabrik-style tree in the **`osm-extracts`** MinIO bucket
(`FW_OSM_EXTRACT_BUCKET`), anonymous-read, served at `FW_GEOFABRIK_BASE_URL`
(default `http://afl-minio:9000/osm-extracts`). Consumers (see the
[cache-and-download](cache-and-download.md) spec) fetch `{base}/<key>-latest.osm.pbf`
exactly as they would a Geofabrik file and cache into `afl-cache`
(`cache/osm/pbf/<key>-latest.osm.pbf`), byte-for-byte verified. The planet master
and scratch stay on host-local disk (`FW_PLANET_DIR`, `/scratch`).

## Gotchas & notes

- **Provider vs base URL.** `FW_OSM_EXTRACT_PROVIDER=geofabrik` (default, empty) +
  `FW_GEOFABRIK_BASE_URL=<our tree>` routes downloads to us; `=osmfr` routes to
  OpenStreetMap France instead (bypasses us — used only pre-self-host).
- **Strategy vs shape.** `complete_ways` for country-edge provinces (self-contained,
  memory-heavier); `simple` for many small interior regions (counties) — the
  location index `complete_ways` rebuilds per pass is pathological for hundreds of
  small regions.
- **Osmium exit `-9`** is the OOM killer, not a logic error — handled by the
  adaptive batcher's self-heal.
- **Planet-scale extraction** (the 87 GB master) runs on the infra host with the
  planet on disk + RAM; the fleet containers' ~14 GB VM bounds in-container work.

## Related specs

- [cache-and-download](cache-and-download.md) — the consumer side that treats our
  tree as Geofabrik.
- [source-adapters](source-adapters.md) — PBF/PostGIS/GeoJSON sources over the extracts.
- [shapefiles](shapefiles.md) — Census TIGER shapefile handling.
- [fan-out-pattern](fan-out-pattern.md) — the per-leaf fleet fan-out shared with heatmaps/cities.
