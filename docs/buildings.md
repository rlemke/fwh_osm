# Buildings

**Namespace:** `osm.Buildings` ·
**FFL:** `src/osm_geocoder/handlers/buildings/ffl/osmbuildings.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/buildings/{building_extractor,building_handlers}.py` ·
**Plugin:** `src/osm_geocoder/handlers/combined/plugins/building_plugin.py` ·
**Tests:** `src/osm_geocoder/handlers/buildings/tests/test_berlin_buildings.py`

## Overview

The buildings feature extracts **building footprints as GeoJSON polygons** from a
region PBF, classified by use (residential / commercial / industrial / retail /
office / public / religious) and enriched with `height` / `building:levels` for 3D
visualisation. Unlike amenities (which collapse to points), buildings keep their
polygon geometry and their footprint **area**, so the layer answers "how much of
this area is built up, and of what kind".

It sits in the analysis half of the pipeline: PBF in (from `osm.planet` /
`osm.cache`), classified-polygon GeoJSON out, rendered by `osm.viz`.

## How it works

`extract_buildings(pbf_path, building_type)` (`building_extractor.py`) is a single
pyosmium `apply_file` pass with `locations=True, idx="flex_mem"`, hooking the
**`area`** callback (osmium assembles closed ways and multipolygon relations into
areas):

1. **Area pass** — keep areas carrying a `building` tag; classify with
   `classify_building(tags)`; unless `building_type="all"`, keep only the matching
   class.
2. **Geometry** — `WKBFactory.create_multipolygon` → `shapely.wkb` →
   `mapping()` yields a Polygon/MultiPolygon. Areas whose geometry can't be
   assembled are counted (`dropped_geometry`) and skipped.
3. **Attributes** — every raw tag is preserved, plus derived `building_type` and
   `osm_id`; footprint area (m²) is summed per feature (`calculate_building_area`,
   an approximate degrees→metres scaling at the feature's mid-latitude), and
   features carrying `height` or `building:levels` are counted (`with_height`).
4. **Stream + finalize** — features stream through `GeoJSONStreamWriter` to a temp
   file, then `finalize_output_file` moves it into place (S3/HDFS-aware).

`BuildingStatistics` and `FilterBuildingsByType` then work GeoJSON→GeoJSON with no
PBF touch.

## Fan-out

**Single-task per region — no fan-out inside this namespace.** One `apply_file`
pass per PBF; the FFL has no `foreach`. As the extractor docstring warns, a
full-region building extract is genuinely large (millions of areas), so fleet
parallelism is orchestrated *above* (a per-region atlas fan-out), and the
single-pass `osm.Combined.CombinedScan` is the amortised path when buildings are
wanted alongside other categories from the same PBF.

## Filtering & attributes

Filtering is a **Python predicate over the parsed tag dict** — `"building" in tags`
keeps an area, then `classify_building(tags)` buckets it. There is no osmium
`tags-filter` on this path.

`BUILDING_TYPE_MAP` (the `building=*` value → class):

| Class | `building=` values (plus fallbacks) |
|---|---|
| residential | `house`, `residential`, `apartments`, `detached`, `semidetached_house`, `terrace`, `dormitory` |
| commercial | `commercial`, `hotel` |
| office | `office`; fallback `office=*` tag present |
| industrial | `industrial`, `warehouse`, `factory`, `manufacture` |
| retail | `retail`, `supermarket`, `kiosk`; fallback `shop=*` tag present |
| public | `public`, `civic`, `government`, `hospital`, `school`, `university`, `kindergarten`; fallback `amenity=hospital\|school\|university\|library\|townhall` |
| religious | `church`, `chapel`, `cathedral`, `mosque`, `temple`, `synagogue` |
| other | tagged `building` but unclassified |

Height/levels are read from `height` / `building:height` (`parse_height` strips
`m`/`ft` units) and `building:levels` (`parse_levels`). The `Buildings3D` and
`LargeBuildings(min_area_m2)` facets are the height-filtered and area-thresholded
variants declared in FFL.

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — PBF reader + area assembly; `WKBFactory`.
- **`shapely`** — WKB → geometry and `geom.area` for footprint m². Extraction guards
  on `HAS_SHAPELY`; area calc returns `0.0` if shapely is absent.
- No osmium-tool **binary** dependency on this path — pure pyosmium.

## Facets & workflows

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `ExtractBuildings(cache, building_type="all")` | event | external / expensive | All buildings, optionally one class |
| `ResidentialBuildings(cache)` | event | external / expensive | `building_type=residential` |
| `CommercialBuildings(cache)` | event | external / expensive | `building_type=commercial` |
| `IndustrialBuildings(cache)` | event | external / expensive | `building_type=industrial` |
| `RetailBuildings(cache)` | event | external / expensive | shops/malls |
| `Buildings3D(cache)` | event | external / expensive | Buildings carrying height/levels (for 3D) |
| `LargeBuildings(cache, min_area_m2=1000)` | event | external / expensive | Buildings above an area threshold |
| `BuildingStatistics(input_path)` | event | pure / cheap | Counts by type + total km² + avg levels |
| `FilterBuildingsByType(input_path, building_type)` | event | pure / cheap | Subset a GeoJSON by class |

Schemas: `BuildingFeatures` (output_path, feature_count, building_type,
total_area_km2, with_height_data, format, extraction_date) and `BuildingStats`
(counts per class, total_area_km2, avg_levels, with_height).

**Handler-wiring caveat (grounded in the code):** `building_handlers.py` registers
**only** the pure post-processing facets — `BuildingStatistics` and
`FilterBuildingsByType`. The extraction event facets (`ExtractBuildings`,
`ResidentialBuildings`, …) are declared in FFL and served at runtime by the
**source adapters** (`osm.Source.PBF.ExtractBuildings` in `sources/pbf_source.py`
calls `extract_buildings`; PostGIS/Overture adapters have their own) and by the
**combined scanner** (`BuildingPlugin` reuses `classify_building` / `parse_height`
for `osm.Combined.CombinedScan`). `test_berlin_buildings.py` registers a mock
`ExtractBuildings` for its end-to-end test.

## Cache / output

- Extraction writes GeoJSON to `resolve_output_dir("osm-buildings")` — local under
  `FW_OUTPUT_BASE`, `s3://afl-cache/...` on the fleet. Filenames:
  `<pbf-stem>_buildings_<building_type>.geojson`.
- The combined-scan path writes `<pbf-stem>_buildings.geojson` under `osm-combined`
  (its plugin also records `height`, `levels`, per-feature `area_km2`, and
  per-class counts in metadata).
- Result reuse via the sidecar `output_cache` keyed on input path + size + params.
- Maps come from `osm.viz.RenderMap` (HTML), not this namespace.

## Gotchas & notes

- **Area is approximate.** `calculate_building_area` scales degrees→metres by
  `111320·cos(lat)` at the footprint's mid-latitude — fine for aggregate km² but
  not survey-grade. (The parks namespace uses a proper equal-area projection when
  `pyproj` is present; buildings do not.)
- **`BuildingStats` reads `area_m2`/`levels` properties**, but the extractor writes
  `area` into the summed `total_area_km2` return field and does not persist a
  per-feature `area_m2` property — so `calculate_building_stats` total area can read
  `0` on extractor-produced files while the extractor's own `total_area_km2` is
  correct. The combined-scan plugin *does* write per-feature `area_km2`. Know which
  producer fed your GeoJSON.
- **Expensive by design.** Full-region building extraction is the heaviest analysis
  facet — prefer a `building_type` filter or the combined scan; the extractor
  docstring flags this explicitly.
- Incomplete-geometry areas are dropped (not failed) and logged as a WARNING count.

## Related specs

- [planet-extraction](planet-extraction.md) — produces the region PBFs consumed here.
- [amenities](amenities.md) — point-based POI sibling (ways collapse to centroids).
- [parks](parks.md) — the other area-based extractor (shares `calculate_area_km2`).
- [poi](poi.md), [boundaries](boundaries.md) — the remaining analysis namespaces.
