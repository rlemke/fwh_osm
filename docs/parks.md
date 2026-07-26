# Parks & Protected Areas

**Namespace:** `osm.Parks` ·
**FFL:** `src/osm_geocoder/handlers/parks/ffl/osmparks.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/parks/{park_extractor,park_handlers}.py` ·
**Plugin:** `src/osm_geocoder/handlers/combined/plugins/park_plugin.py` ·
**Tests:** `src/osm_geocoder/handlers/parks/tests/{test_parks,test_brazil_parks}.py`

## Overview

The parks feature extracts **national parks, state/regional parks, nature
reserves, and other protected areas** from a region PBF as GeoJSON polygons,
classified by protection type and measured in km². It reads OSM's protected-area
vocabulary — `boundary=national_park`, `leisure=park`/`nature_reserve`,
`boundary=protected_area`, and the IUCN `protect_class` scale — and normalises the
long tail of tagging conventions (tags, designations, protect classes) into one
`park_type` per feature.

It sits in the analysis half of the pipeline: PBF in (from `osm.planet` /
`osm.cache`), classified-polygon GeoJSON out, rendered by `osm.viz`.

## How it works

`extract_parks(pbf_path, park_type, protect_classes)` (`park_extractor.py`) is a
single pyosmium `apply_file` pass with `locations=True, idx="flex_mem"` on the
**`area`** callback:

1. **Area pass** — keep areas where `matches_park_type(tags, park_type,
   protect_set)` is true; areas whose geometry can't be assembled are skipped.
2. **Geometry** — `WKBFactory.create_multipolygon` → `shapely.wkb` → `mapping()`
   yields a Polygon/MultiPolygon.
3. **Classify + measure** — every raw tag is preserved plus a derived `park_class`
   (`classify_park`), and each feature's area (km²) is summed via
   `calculate_area_km2` — a **geodesic** Albers-Equal-Area computation via `pyproj`
   when available, falling back to an approximate spherical calc otherwise.
4. **Stream + finalize** — features stream through `GeoJSONStreamWriter` to a temp
   file, then `finalize_output_file` moves it into place (S3/HDFS-aware).

`ParkStatistics` and `FilterParksByType` then work GeoJSON→GeoJSON with no PBF touch.

## Fan-out

**Single-task per region — no fan-out inside this namespace.** One `apply_file`
pass per PBF; no `foreach` in the FFL. Fleet parallelism across regions is
orchestrated by composed atlas workflows (e.g. `NationalParksAnalysis` in
`composed_workflows`); the single-pass `osm.Combined.CombinedScan` amortises the
cost when parks are wanted alongside other categories from the same PBF.

## Filtering & attributes

Filtering is a **Python predicate over the parsed tag dict** — `matches_park_type`,
not an osmium `tags-filter`. The keep test is: a feature is "any kind of park" if
`boundary ∈ {national_park, protected_area}` **or** `leisure ∈ {park,
nature_reserve}` **or** it carries a non-empty `protect_class`; then it is narrowed
to the requested `park_type` and, if given, an allowed `protect_classes` set.

`classify_park` precedence (first match wins):

| Class | Signals |
|---|---|
| national | `boundary=national_park`; `protect_class=2`; `designation` containing "national park" |
| state | `protect_class=5`; `designation` with "state/regional/provincial park" |
| nature_reserve | `leisure=nature_reserve`; `protect_class ∈ {1a,1b}`; `designation` with "nature reserve" |
| protected_area | `boundary=protected_area`; default fallback |
| park | `leisure=park` |

IUCN `protect_class` sets (`park_extractor.py`): national `{2}`, state `{5}`,
strict-reserve `{1a,1b}`, all `{1a,1b,2,3,4,5,6}`. `protect_classes` is parsed by
`parse_protect_classes` (`"*"`/`"all"` → all; else comma-separated). Preserved
per-feature properties include the raw tags plus `park_class`, `protect_class`,
`designation`, `operator`, `area_km2`, `osm_id`.

`FilterParksByType` is dual-mode: it matches on a pre-classified `park_type`
property when present (combined-scan output) and falls back to raw-tag
`matches_park_type` otherwise.

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — PBF reader + area assembly; `WKBFactory`.
- **`shapely`** — WKB → geometry; area calc guards on `HAS_SHAPELY`.
- **`pyproj`** (pip, optional) — accurate geodesic (Albers Equal Area) km²; absent →
  approximate spherical area. This is the one analysis namespace that uses `pyproj`.
- No osmium-tool **binary** dependency on this path.

## Facets & workflows

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `NationalParks(cache)` | event | external / expensive | `boundary=national_park` or `protect_class=2` |
| `StateParks(cache)` | event | external / expensive | `protect_class=5` / state designation |
| `NatureReserves(cache)` | event | external / expensive | `leisure=nature_reserve` |
| `ProtectedAreas(cache, protect_classes="*")` | event | external / expensive | All protected areas, optional class filter |
| `ExtractParks(cache, park_type="all", protect_classes="*")` | event | external / expensive | Configurable type + class |
| `LargeParks(cache, min_area_km2=100, park_type="all")` | event | external / expensive | Parks above an area threshold |
| `FilterParksByType(input_path, park_type, protect_classes="*")` | event | pure / cheap | Subset a GeoJSON by type/class |
| `ParkStatistics(input_path)` | event | pure / cheap | Counts by type + total km² |

Schemas: `ParkFeatures`, `ParkStats` (national/state/reserve/other counts +
total_area_km2), `ParkFeature` (per-park detail row).

**Handler-wiring caveat (grounded in the code):** `park_handlers.py` registers
**only** the pure post-processing facets — `FilterParksByType` and `ParkStatistics`.
The extraction event facets (`NationalParks`, `StateParks`, `ExtractParks`, …) are
declared in FFL and served at runtime by the **source adapters**
(`osm.Source.PBF.ExtractParks` in `sources/pbf_source.py` calls `extract_parks`;
PostGIS/Overture adapters have their own) and by the **combined scanner**
(`ParkPlugin` reuses `matches_park_type` / `classify_park` / `calculate_area_km2`
for `osm.Combined.CombinedScan`). `composed_workflows` wires
`NationalParksAnalysis`.

## Cache / output

- Extraction writes GeoJSON to `resolve_output_dir("osm-parks")` — local under
  `FW_OUTPUT_BASE`, `s3://afl-cache/...` on the fleet. Filenames:
  `<pbf-stem>_parks_<park_type>.geojson`.
- The combined-scan path writes `<pbf-stem>_parks.geojson` under `osm-combined`,
  with per-feature `area_km2` and per-type counts / total area in metadata.
- Result reuse via the sidecar `output_cache` keyed on input path + size + params.
- Maps come from `osm.viz.RenderMap` (HTML), not this namespace.

## Gotchas & notes

- **Area accuracy depends on `pyproj`.** With `pyproj` you get a per-feature
  equal-area projection centred on the polygon (accurate); without it you get a
  crude mid-latitude spherical approximation. Install `pyproj` on runner hosts if
  area figures matter.
- **`protect_class` is a scale, not a boolean.** The keep test treats *any*
  non-empty `protect_class` as a candidate park — so a `ProtectedAreas` extract with
  no class filter is broad. Narrow with `protect_classes` (e.g. `"2"` for national).
- **Classification is designation-aware.** Regions that tag parks via `designation`
  text ("Provincial Park", "State Park") rather than `protect_class` still classify
  correctly — but the string matching is English-centric.
- **`park_type` reuse in stats/filter** relies on the extractor having written a
  `park_type`/`park_class` property; feeding a raw-OSM GeoJSON that lacks it falls
  back to tag matching (handled, but slower/less precise).

## Related specs

- [planet-extraction](planet-extraction.md) — produces the region PBFs consumed here.
- [buildings](buildings.md) — the other area-based extractor (shares
  `calculate_area_km2` via the boundary plugin).
- [boundaries](boundaries.md) — overlaps on `boundary=national_park` / natural
  "park" boundaries; different geometry-assembly strategy (osmium-tool).
- [amenities](amenities.md), [poi](poi.md) — the remaining analysis namespaces.
