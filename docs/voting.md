# Voting Districts (Census TIGER/Line)

**Namespace(s):** `census.types`, `census.tiger.Districts`, `census.tiger.Processing`,
`census.tiger.Workflows` ·
**FFL:** `handlers/voting/ffl/osmvoting.ffl` ·
**Handlers:** `handlers/voting/tiger_handlers.py`, `handlers/voting/tiger_downloader.py`

## Overview

The `voting` namespace downloads and processes **US electoral boundary data** from the
US Census Bureau's TIGER/Line shapefiles: Congressional Districts, State Legislative
Districts (upper/lower chamber), and Voting Precincts. It is the non-OSM data source in
the domain — Census.gov shapefiles rather than OSM PBFs — that produces the same GeoJSON
that OSM analysis facets consume, so electoral boundaries can be joined against OSM
admin boundaries and rendered on the same maps.

Note the namespace is `census.tiger.*` (not `osm.*`): this is a distinct data provider
that lives in the OSM domain package because electoral maps compose with OSM data.

## How it works

Two stages — download, then convert:

1. **Download** (`census.tiger.Districts.*`): given a `state` (name / abbreviation / FIPS
   code) and `year`, resolve the state FIPS and build the TIGER/Line URL under
   `https://www2.census.gov/geo/tiger/TIGER<year>/...`, download the `.zip` with
   filesystem caching, and return a `TIGERCache` (url, local path, date, size,
   `wasInCache`, year, district_type, state). URL shapes by type:
   - **Congressional** — `CD/tl_<year>_us_cd<congress>.zip` pre-2023 (nationwide),
     `CD/tl_<year>_<fips>_cd<congress>.zip` for 2023+ (per-state).
   - **State Senate / House** — `SLDU/tl_<year>_<fips>_sldu.zip` /
     `SLDL/tl_<year>_<fips>_sldl.zip`.
   - **Voting Precincts** — `VTD/tl_<year>_<fips>_vtd*.zip` (decennial census only).
2. **Convert** (`census.tiger.Processing.ShapefileToGeoJSON`): unzip the shapefile and
   convert it to GeoJSON via **`ogr2ogr` (GDAL)** when available, falling back to
   **`geopandas`**; returns a `VotingDistrictResult` (output_path, feature_count,
   district_type, state, year, format, extraction_date).

The convenience workflows chain the two: `GetCongressionalDistricts` downloads +
converts CDs; `GetStateVotingBoundaries` downloads senate + house + precincts (precincts
always 2020) and converts all three. Two `Processing` facets refine results:
`JoinWithOSMBoundaries` (spatial join against OSM admin boundaries) and `FilterDistricts`
(by a named attribute/value). `StateFIPS` resolves a state name/abbrev to its 2-digit
FIPS.

## Fan-out

**Single-task per district download — no `foreach` fan-out** in the shipped FFL. Each
download/convert is one task; the multi-district workflow `GetStateVotingBoundaries`
issues three parallel *steps* (senate/house/precincts) within one workflow, but there is
no per-state or per-district `foreach`. Downloads are individually cheap and
cache-backed, so the atlas-style fleet fan-out ([fan-out pattern](fan-out-pattern.md)) is
not needed here; a multi-state map would fan at the caller by invoking the workflow per
state.

## Filtering & attributes

- **Selection is by district type + year + state FIPS**, encoded in the TIGER URL — not
  by OSM tag filtering. The Census shapefile *is* the district set.
- `congress_number` is auto-derived from `year` when omitted (known mappings:
  2020–2022→116, 2023→118, 2024→119).
- `FilterDistricts(input_path, attribute, value)` post-filters converted GeoJSON by a
  named shapefile attribute (e.g. district number or name).
- `JoinWithOSMBoundaries` spatially relates districts to OSM `boundary=administrative`
  geometry.

## External libraries / binaries

- **`ogr2ogr` (GDAL)** — primary shapefile → GeoJSON converter. **Binary** dependency,
  probed at import (`ogr2ogr --version`); `HAS_OGR2OGR` gates its use.
- **`geopandas`** — pip fallback converter when `ogr2ogr` is absent (`HAS_GEOPANDAS`).
- **`requests`** — downloading the TIGER `.zip` files.
- **`zipfile`** (stdlib) — unpacking the shapefile archive.
- No `osmium`/`pyosmium` — this source never touches a PBF (works in both full and lite
  agents).

## Facets & workflows

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `CongressionalDistricts(state, year, congress_number)` | event (external) | Download CD boundary shapefile → `TIGERCache` |
| `StateSenateDistricts(state, year)` | event (external) | Download upper-chamber (SLDU) shapefile |
| `StateHouseDistricts(state, year)` | event (external) | Download lower-chamber (SLDL) shapefile |
| `VotingPrecincts(state, year)` | event (external) | Download voting-precinct (VTD) shapefile (decennial) |
| `AllStateDistricts(state, year)` | event (external) | Senate + house + precincts in one call |
| `ShapefileToGeoJSON(cache)` | event (io) | Convert TIGER shapefile → GeoJSON (ogr2ogr/geopandas) |
| `JoinWithOSMBoundaries(districts, osm_boundaries_path)` | event (pure) | Spatial join districts × OSM admin boundaries |
| `FilterDistricts(input_path, attribute, value)` | event (pure) | Filter districts by attribute |
| `StateFIPS(state)` | facet | Look up 2-digit FIPS for a state name/abbrev |
| `GetCongressionalDistricts(state, year, congress_number)` | workflow | Download + convert CDs → GeoJSON |
| `GetStateVotingBoundaries(state, year)` | workflow | Download + convert senate/house/precincts |

Download facets carry `Effect(kind="external") Cost(tier="expensive")`;
`ShapefileToGeoJSON` is `io`/`moderate`; the join/filter facets are `pure`/`cheap`.
Schemas: `TIGERCache`, `VotingDistrictResult` in `census.types`.

## Cache / output

- **Download cache**: `<output_base>/census/tiger-cache/` (filesystem cache of the raw
  `.zip`; `wasInCache` reports a hit). Handlers also use the domain's `cached_result` /
  `save_result_meta` output cache.
- **Output**: GeoJSON FeatureCollections (`VotingDistrictResult.output_path`) written via
  the shared `_output` helpers, so they land on local disk or MinIO per `FW_STORAGE`.
  Format is always `GeoJSON`.

## Gotchas & notes

- **Not every district type exists for every year.** Congressional 2020–2024 (per-state
  files only from 2023); State Legislative 2020–2024; Voting Precincts **2020 only**
  (decennial). Requesting an unavailable combination fails at download.
- **Congress-number ↔ year coupling.** For year ≥ 2023 CD files are per-state and need
  the state FIPS; the auto-derived congress number must match the year or the URL 404s.
- **Converter availability.** With neither `ogr2ogr` nor `geopandas` present,
  `ShapefileToGeoJSON` cannot convert — install GDAL (preferred) or geopandas on the
  runner. `fw install check` surfaces the gap.
- **State input is flexible** — name ("California"), abbreviation ("CA"), or FIPS ("06");
  `StateFIPS` / `resolve_state_fips` normalise it.
- **Framework PostGIS view naming.** The framework's `afl_postgis_query` tool exposes
  osm2pgsql-style `planet_osm_*` views for OSM data; TIGER districts are a *separate*
  GeoJSON product and are not loaded into those views by this namespace.

## Related specs

- [postgis-db](postgis-db.md) — OSM persistence/query backends these boundaries can be
  joined against.
- [composed-workflows](composed-workflows.md) — how a district GeoJSON composes with OSM
  boundary extraction + rendering.
- [planet-extraction](planet-extraction.md) — TIGER shapefiles are also the county/state
  polygon fallback for self-generating region boundaries.
