# Administrative & Natural Boundaries

**Namespace:** `osm.Boundaries` ·
**FFL:** `src/osm_geocoder/handlers/boundaries/ffl/osmboundaries.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/boundaries/{boundary_extractor,boundary_handlers}.py` ·
**Plugin:** `src/osm_geocoder/handlers/combined/plugins/boundary_plugin.py` ·
**Tests:** `src/osm_geocoder/handlers/boundaries/tests/{test_boundaries,test_canada_boundaries}.py`

## Overview

The boundaries feature extracts **administrative borders** (country / state /
county / city, keyed by `admin_level`) and **natural boundaries** (water, forest,
park) from a *single region PBF* as GeoJSON polygons. It answers "draw the outline
of this state / lake / forest" from data already clipped to one extract.

This is the **consumer-side, per-extract** counterpart to the planet pipeline's
boundary *generation*. `osm.planet`'s `boundary_gen` / `GenerateRegionPolygons`
build the *global set* of region polygons used to **split** the planet into
extracts (see [planet-extraction](planet-extraction.md) → "Boundaries"); this
namespace instead extracts the boundary geometry that already lives *inside* one
downloaded extract, for rendering and analysis. Same OSM vocabulary
(`boundary=administrative` + `admin_level`), opposite direction of the pipeline.

## How it works

`extract_boundaries(pbf_path, admin_levels, natural_types)`
(`boundary_extractor.py`) has a **primary path and a fallback**:

1. **Primary — osmium-tool + osm2geojson.** `osmium tags-filter` writes a filtered
   `.osm` (admin: `r/boundary=administrative`; natural: the `_NATURAL_TAG_FILTERS`
   expressions), then `osm2geojson.xml2geojson` assembles geometries — crucially
   including `type=boundary` relations that pyosmium/GDAL cannot close. This is the
   only path that yields proper geometry for all relation types.
2. **Fallback — pyosmium.** If osmium-tool or osm2geojson are unavailable, a
   `SimpleHandler` walks `area` + `relation`; multipolygon relations get geometry,
   but bare `type=boundary` relations are emitted **without geometry**.
3. **Region self-filtering.** For admin boundaries, features are filtered to the one
   whose `name` matches the PBF filename (`_region_name_from_pbf`) so a
   `california-latest.osm.pbf` extract doesn't also emit neighbouring states that
   overlap the clip. Natural boundaries are filtered by **centroid-in-bbox** using
   the PBF header bbox (`osmium fileinfo -j`).

Data shape: `PBF → filtered .osm → GeoJSON (Multi)Polygon`, written via
`open_output` to `resolve_output_dir("osm-boundaries")`.

## Fan-out

**Single-task per region — no fan-out inside this namespace.** No `foreach` in the
FFL; one extraction pass per PBF. (Contrast the planet pipeline's
`BuildAdminFanout`, which fans one admin-set build per child region across the
fleet — that is boundary *set generation*, a different feature; see
[planet-extraction](planet-extraction.md).) Fleet parallelism over many regions is
orchestrated by higher-level atlas workflows, and `osm.Combined.CombinedScan`
amortises boundaries alongside other categories from the same PBF.

## Filtering & attributes

Two mechanisms, depending on path:

- **osmium-tool `tags-filter`** (primary) — real CLI tag filtering:
  - admin: `r/boundary=administrative`, then `_matches_admin` keeps
    `int(admin_level) ∈ requested set`.
  - natural (`_NATURAL_TAG_FILTERS`):
    - water → `r/natural=water`, `r/water=lake,reservoir,pond`
    - forest → `r/natural=wood`, `r/landuse=forest`
    - park → `r/leisure=park,nature_reserve`, `r/boundary=national_park`
- **Python predicate** (pyosmium fallback + result filtering) — `_matches_admin` /
  `_matches_natural` over the tag dict.

Admin-level constants: country=2, state=4, county=6, city=8 (`_describe_boundary_type`
names them). Preserved properties: `osm_id`, `osm_type` (way/relation), `name`,
`boundary_type`, `admin_level` (admin only), plus all raw tags (so `ISO3166-2`,
etc. survive). The combined-scan `BoundaryPlugin` writes a slightly different
property shape (`admin_type`, `natural_type`, `area_km2`) and extracts admin levels
{2,4,6,8} + natural water/forest/park in one pass.

## External libraries / binaries

- **`osmium` (osmium-tool binary)** — `tags-filter` (primary extraction) and
  `fileinfo` (bbox for natural filtering). A **binary** dependency; `HAS_OSMIUM_TOOL`
  is `shutil.which("osmium")`.
- **`osm2geojson`** (pip) — assembles filtered `.osm` XML into GeoJSON, including
  `type=boundary` relations. Primary path needs both this **and** the binary.
- **`pyosmium`** (pip `osmium`) — the fallback reader (`SimpleHandler`); no geometry
  for bare boundary relations.
- **`shapely`** (pip) — geometry mapping + representative-point bbox test.
- `extract_boundaries` raises `ImportError` if neither pyosmium nor (osmium-tool +
  osm2geojson) is present.

## Facets & workflows

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `CountryBoundaries(cache)` | event | external / expensive | `admin_level=2` |
| `StateBoundaries(cache)` | event | external / expensive | `admin_level=4` |
| `CountyBoundaries(cache)` | event | external / expensive | `admin_level=6` |
| `CityBoundaries(cache)` | event | external / expensive | `admin_level=8` |
| `AdminBoundary(cache, admin_level=2)` | event | external / expensive | Any configurable admin level |
| `LakeBoundaries(cache)` | event | external / expensive | natural=water |
| `ForestBoundaries(cache)` | event | external / expensive | natural=wood / landuse=forest |
| `ParkBoundaries(cache)` | event | external / expensive | leisure=park/nature_reserve, boundary=national_park |
| `NaturalBoundary(cache, natural_type="water")` | event | external / expensive | Any configurable natural type |

Schema: `BoundaryFeatures` (output_path, feature_count, boundary_type, admin_levels,
format, extraction_date).

**Handler-wiring caveat (grounded in the code — important):**
`boundary_handlers.py` registers **nothing**. Its own docstring: *"All
extraction-based handlers have been removed. This module is retained for structural
compatibility."* `register_boundary_handlers` is a no-op and the RegistryRunner
`handle` raises `Unknown facet`. The `osm.Boundaries` extraction facets are
therefore declared in FFL as the capability surface but served at runtime **only**
through the **combined scanner** (`BoundaryPlugin` extracts admin+natural in the
single `CombinedScan` pass) and the **source adapters** (`osm.Source.PBF` /
`osm.Source.PostGIS` / `osm.Source.Overture`, which reuse the admin-level constants
and the extraction logic). `test_boundaries.py` registers a mock
`osm.Boundaries.CountryBoundaries` for its end-to-end test; `voting` tests use
`AdminBoundary` similarly.

## Cache / output

- Extraction writes GeoJSON to `resolve_output_dir("osm-boundaries")` — local under
  `FW_OUTPUT_BASE` (README default `/tmp/osm-boundaries/`), `s3://afl-cache/...` on
  the fleet. Filenames encode the PBF stem + `admin<levels>` + natural types, e.g.
  `california_admin4.geojson`.
- The combined-scan path writes `<pbf-stem>_boundaries.geojson` under `osm-combined`.
- Result reuse via the sidecar `output_cache` (on the served paths).
- Maps come from `osm.viz.RenderMap` (HTML), not this namespace.

## Gotchas & notes

- **`osm2geojson` is what makes admin boundaries work.** Without the osmium-tool
  binary + osm2geojson, the pyosmium fallback emits `type=boundary` relations with
  **no geometry** — a country/state outline may come back as a null-geometry
  feature. Install both on runner hosts that render boundaries.
- **Region self-filtering by name is load-bearing.** A Geofabrik extract overlaps
  its neighbours; `_name_matches_region` keeps only the region matching the PBF
  filename. This depends on the OSM `name` matching the de-slugged filename
  (`district-of-columbia` → `district of columbia`) — a renamed/aliased extract can
  drop the intended boundary.
- **This is not admin-set generation.** For building the *global* per-region polygon
  tree that splits the planet (the hard part, with the `complete_ways` /
  TIGER-fallback story), see [planet-extraction](planet-extraction.md) — do not
  conflate `osm.Boundaries.*` (extract-from-one-PBF) with `osm.planet.*`
  (generate-the-set).
- **Handlers module registers nothing** — do not expect `osm.Boundaries.*` to be
  served by `boundary_handlers.py`; route through `CombinedScan` or a source adapter.

## Related specs

- [planet-extraction](planet-extraction.md) — the *generation* side: builds the
  region-boundary polygons that split the planet, and the `BuildAdminFanout`
  fleet fan-out. Cross-linked because the two are easily confused.
- [parks](parks.md) — overlaps on `boundary=national_park` / natural "park".
- [buildings](buildings.md) — the other area-based extractor sharing the combined
  scanner's area plugins.
- [amenities](amenities.md), [poi](poi.md) — the remaining analysis namespaces.
