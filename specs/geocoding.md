# Geocoding (Nominatim)

**Namespace(s):** `osm.geocode` ·
**FFL:** `src/osm_geocoder/ffl/geocoder.ffl` ·
**Handlers:** `handlers/geocoding/geocoding_handlers.py` ·
**Tools:** `tools/_osm_tools/geocode.py`

## Overview

`osm.geocode` is the domain's **address ↔ coordinate** service: forward geocoding
(street address / place name → lat/lon) and reverse geocoding (lat/lon → nearest
address/place), both backed by OSM **Nominatim** over HTTP. It is the namespace the whole
`osm_geocoder` package is named for — the smallest, most self-contained feature: a scalar
query→coordinate service rather than a layer transform, so results are a typed
`GeoCoordinate`, not a GeoJSON path.

## How it works

Two event facets over one Nominatim client:

1. **`Geocode(address)`** → `geocoding_handlers._make_geocode_handler` →
   `_osm_tools.geocode.forward(address, limit=1)`: GET Nominatim `/search`, take the
   best-ranked result, return `{lat, lon, display_name}`.
2. **`ReverseGeocode(lat, lon, zoom=18)`** → `_make_reverse_geocode_handler` →
   `geocode.reverse(lat, lon, zoom)`: GET Nominatim `/reverse`; `zoom` (0–18) sets
   address granularity (18 ≈ building, 10 ≈ city).

Both handlers wrap the tool with the domain's **output cache**: results key on a synthetic
path (`geocode:fwd:<address>` / `geocode:rev:<lat>,<lon>,<zoom>`) so a repeat — e.g. a
`foreach` over addresses — is a free cache hit, no HTTP call. Failures are **explicit**:
an unresolvable address/point raises `GeocodeError` (the step errors) rather than
returning empty coordinates. The `GeoCoordinate` schema is `{lat: String, lon: String,
display_name: String}`.

The handler predates its own facet: `Geocode` was declared in `geocoder.ffl` but
orphaned (no handler); `geocoding_handlers.py` implements it and adds `ReverseGeocode`.

## Fan-out

**`GeocodeAll(addresses)` fans out** — a workflow-level `andThen foreach addr in
$.addresses` that runs one `Geocode` per address in parallel across the fleet, yielding
`locations = geo.result` per iteration. It is a genuine [fan-out](fan-out-pattern.md), but
a lightweight one: each leaf is a cheap external call, not a PBF extract, and the
per-address output cache makes repeats free. The single-address workflows
(`GeocodeAddress`, `ReverseGeocodePoint`) are single-task.

## Filtering & attributes

**No OSM tag filtering** — Nominatim resolves the query and returns ranked matches;
the facet keeps the first (best) one. `forward` optionally accepts `countrycodes`
(comma-separated ISO codes) to constrain results at the tool layer, though the facet
calls it with `limit=1` and no country constraint. Reverse granularity is controlled by
`zoom`, not by tags.

## External libraries / binaries

- **Nominatim** (HTTP service) — the geocoding backend; public
  `nominatim.openstreetmap.org` by default, overridable via **`FW_NOMINATIM_URL`** to a
  self-hosted instance. Not a binary/pip dependency — a network service.
- **stdlib only** — `_osm_tools.geocode` uses `urllib.request` (not `requests`), `json`,
  `threading`, `time`. No `osmium`, no `shapely`. The primitives are written to
  generalise across Nominatim / Photon / Pelias.
- **Client-side rate limiter** — a module-level lock + minimum inter-call interval
  enforces Nominatim's public usage policy (~1 request/second); a self-hosted instance can
  relax it.

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `Geocode(address)` | event (external, cheap) | Forward geocode → best-ranked `GeoCoordinate` |
| `ReverseGeocode(lat, lon, zoom=18)` | event (external, cheap) | Reverse geocode → nearest address/place |
| `GeocodeAddress(address)` | workflow | Single address → coordinates |
| `ReverseGeocodePoint(lat, lon, zoom=18)` | workflow | Single coordinate → nearest location |
| `GeocodeAll(addresses: Json)` | workflow | **Fan-out** — geocode many addresses in parallel |

Both facets carry `with Effect(kind = "external") with Cost(tier = "cheap")`. Schema:
`osm.geocode.GeoCoordinate`.

## Cache / output

- **Output cache**: the domain `cached_result` / `save_result_meta` store, keyed on the
  synthetic `geocode:fwd:…` / `geocode:rev:…` path + query params. No file artifact — the
  result is the scalar `GeoCoordinate` typed return (this feature produces no GeoJSON /
  HTML / tiles).
- Because results are scalars, there is no MinIO/site output — only the cache and the
  step return value.

## Gotchas & notes

- **Respect the rate limit.** The public Nominatim allows ~1 req/s; the tool enforces it
  in-process, but a large `GeocodeAll` fanned across many runners can still exceed the
  *aggregate* policy — self-host (`FW_NOMINATIM_URL`) for bulk work.
- **Explicit failure, by design.** No result → `GeocodeError` → the step fails; the
  handler never returns empty coordinates. Handle unresolved addresses upstream, or the
  workflow errors.
- **Coordinates are Strings.** `GeoCoordinate.lat`/`lon` are typed `String` (Nominatim
  returns them as strings); cast before arithmetic.
- **Cache makes repeats free**, so a re-run or a `foreach` with duplicate addresses does
  not re-hit Nominatim — good for the rate limit, but a stale cache won't reflect
  Nominatim updates (clear the cache to refresh).
- **`zoom` is reverse-only** — it sets reverse-geocode granularity and is ignored by
  forward geocoding.

## Related specs

- [fan-out-pattern](fan-out-pattern.md) — `GeocodeAll`'s per-address parallelism.
- [composed-workflows](composed-workflows.md) — how a geocoded coordinate feeds into
  region/layer pipelines.
