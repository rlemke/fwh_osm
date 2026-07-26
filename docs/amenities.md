# Amenities & Air Quality

**Namespaces:** `osm.Amenities`, `osm.AirQuality`, `osm.SchoolAirQuality` ·
**FFL:** `src/osm_geocoder/handlers/amenities/ffl/{osmamenities,osmairquality}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/amenities/{amenity_extractor,amenity_handlers,airquality_handlers}.py` ·
**Tests:** `src/osm_geocoder/handlers/amenities/tests/{test_paris_amenities,test_school_airquality}.py`

## Overview

The amenities feature turns a cached region PBF into a clickable **point layer of
POIs** — restaurants, shops, banks, hospitals, schools, cinemas, and the rest of
the `amenity=*` / `shop=*` world. Every OSM tag survives onto the feature so a map
popup or a downstream filter can read `name`, `cuisine`, `brand`, `opening_hours`,
etc. It sits in the analysis half of the pipeline: `osm.planet`/`osm.cache` deliver
the PBF, this namespace extracts and classifies, and `osm.viz` renders.

The paired `osm.AirQuality` / `osm.SchoolAirQuality` namespaces are a worked
composition on top of amenity extraction: extract `education` amenities (schools),
fetch OpenAQ sensor readings, correlate each school to its nearest sensor, and
classify PM2.5 exposure against WHO thresholds — an end-to-end "which schools sit
in polluted air" map.

## How it works

`extract_amenities(pbf_path, category)` (`amenity_extractor.py`) is a single
pyosmium `apply_file` pass with `locations=True, idx="flex_mem"`:

1. **Node pass** — a node is kept if it carries `amenity`, `shop`, or a recognised
   `tourism` value; it becomes a `Point` at its lon/lat.
2. **Way pass** — a way carrying the same tags is reduced to its **centroid**
   `Point` (via `shapely.wkb` on `WKBFactory.create_linestring`), so the output is
   a uniform point layer regardless of source geometry. Ways whose geometry can't
   be assembled (missing nodes) are counted in `features_dropped` and skipped.
3. **Classify + filter** — each kept element is run through `classify_amenity`
   (tag → category); unless `category="all"`, only the requested category is
   written.
4. **Stream + finalize** — features stream to a temp file via
   `GeoJSONStreamWriter` (multi-GB safe), then `finalize_output_file` moves it into
   place (S3/HDFS-aware).

Data shape: `PBF → GeoJSON Points`. The stats/search/filter facets then operate
GeoJSON→GeoJSON with no PBF touch.

`SchoolAirQualityMap` chains: `ResolveRegion → osm.cache.Download →
ExtractAmenities(category="education") → FetchAirQuality → CorrelateSchoolAirQuality
→ ExposureStatistics → RenderMap`. Correlation is O(schools × stations) haversine
nearest-neighbour with a `max_distance_km` cutoff.

## Fan-out

**Single-task per region — no fan-out inside this namespace.** Extraction is one
`apply_file` pass over one PBF; `SchoolAirQualityMap` is a linear `andThen` chain
with no `foreach`. Fleet-scale parallelism comes from *above*: a composed atlas
workflow fans a per-region amenity/school pipeline out across the fleet, and the
single-pass `osm.Combined.CombinedScan` amortises the cost when several categories
(amenities + buildings + parks…) are wanted from the same PBF at once.

## Filtering & attributes

Filtering is a **Python predicate over the parsed tag dict**, not an osmium
`tags-filter` — `_is_amenity(tags)` keeps an element, `classify_amenity(tags)`
buckets it. The keep test is `"amenity" in tags or "shop" in tags or
tags["tourism"] in {hotel, motel, hostel, guest_house, museum, gallery, zoo,
theme_park}`.

Concrete tag → category map (`AMENITY_CATEGORIES` / the typed sets in
`amenity_extractor.py`):

| Category | OSM tags |
|---|---|
| food | `amenity=restaurant\|cafe\|bar\|pub\|fast_food\|food_court\|ice_cream\|biergarten` |
| shopping | any `shop=*` (`supermarket`, `convenience`, `mall`, `department_store`, `clothes`, `shoes`, `electronics`, …) |
| services | `amenity=bank\|atm\|post_office\|fuel\|car_wash\|car_rental\|charging_station\|parking`; `tourism=hotel\|motel\|hostel\|guest_house` |
| healthcare | `amenity=hospital\|clinic\|doctors\|dentist\|pharmacy\|veterinary` |
| education | `amenity=school\|university\|college\|library\|kindergarten` |
| entertainment | `amenity=cinema\|theatre\|nightclub\|casino\|arts_centre`; `tourism=museum\|gallery\|zoo\|theme_park` |
| transport | `amenity=bus_station\|ferry_terminal\|taxi` |
| other | matched keep-test but unclassified |

Every raw tag is preserved as a feature property, plus a derived `category` and
`osm_id`. `AmenityStatistics` additionally counts `with_name` and
`with_opening_hours` coverage. `SearchAmenities` filters an existing GeoJSON by a
case-insensitive **regex over the `name` property**.

**Air quality:** `FetchAirQuality` queries the OpenAQ v3 `/locations` endpoint
(default `parameter=pm25`, `radius_m=25000`), using the bbox centre as the query
coordinate. Exposure is classified `_classify_exposure`: `high ≥ 35`, `medium
15–35`, `low < 15` µg/m³ (WHO PM2.5 thresholds `PM25_HIGH=35`, `PM25_MEDIUM=15`).

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — the PBF reader; `WKBFactory` for way centroids.
- **`shapely`** — WKB → geometry / centroid.
- **`requests`** (pip) — OpenAQ v3 HTTP calls; air-quality handlers degrade to an
  empty result if `requests` is missing or `OPENAQ_API_KEY` is unset.
- No osmium-tool **binary** here (unlike boundaries) — extraction is pure pyosmium.

## Facets & workflows

| Facet / Workflow | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `osm.Amenities.ExtractAmenities(cache, category="all")` | event | external / expensive | Extract all amenities, optionally one category |
| `Restaurants`/`Cafes`/`Bars`/`FastFood`/`FoodAndDrink` | event | external / expensive | Typed food-and-drink extractors |
| `Supermarkets`/`ShoppingMalls`/`ConvenienceStores`/`AllShopping` | event | external / expensive | Typed shopping extractors |
| `Banks`/`ATMs`/`PostOffices`/`FuelStations`/`EVCharging`/`Parking` | event | external / expensive | Typed service extractors |
| `Hospitals`/`Pharmacies`/`Doctors`/`AllHealthcare` | event | external / expensive | Typed healthcare extractors |
| `Schools`/`Universities`/`Libraries`/`AllEducation` | event | external / expensive | Typed education extractors |
| `Cinemas`/`Theatres`/`Nightclubs`/`AllEntertainment` | event | external / expensive | Typed entertainment extractors |
| `AmenityStatistics(input_path)` | event | pure / cheap | Aggregate counts + name/hours coverage |
| `FilterAmenitiesByCategory(input_path, category)` | event | pure / cheap | Subset a GeoJSON by category |
| `SearchAmenities(input_path, name_pattern)` | event | pure / cheap | Regex-search features by name |
| `osm.AirQuality.FetchAirQuality(bbox, parameter="pm25", radius_m=25000)` | event | external / moderate | OpenAQ readings in a bbox |
| `CorrelateSchoolAirQuality(schools_path, air_quality_path, max_distance_km=10)` | event | pure / cheap | Nearest-sensor + exposure class per school |
| `ExposureStatistics(input_path)` | event | pure / cheap | Aggregate exposure stats + WHO percentages |
| `osm.SchoolAirQuality.SchoolAirQualityMap(region, …)` | workflow | — | End-to-end school air-quality exposure map |

**Handler-wiring caveat (grounded in the code):** `amenity_handlers.py` registers
**only** the pure post-processing facets — `AmenityStatistics`, `SearchAmenities`,
and `FilterByCategory`. The *extraction* event facets (`ExtractAmenities`,
`Restaurants`, …) are declared in FFL as the capability surface but are served at
runtime through the **source adapters** (`osm.Source.PBF.ExtractAmenities` in
`sources/pbf_source.py` calls `extract_amenities`) and the **combined scanner**
(`AmenityPlugin` reuses `classify_amenity` for `osm.Combined.CombinedScan`). Unit
tests register their own mock handlers for the typed facets. `airquality_handlers.py`
registers all three `osm.AirQuality` facets directly.

## Cache / output

- Extraction writes GeoJSON to `resolve_output_dir("osm-amenities")` — local disk
  under `FW_OUTPUT_BASE` in single-box mode, `s3://afl-cache/...` on the fleet.
  Filenames: `<pbf-stem>_amenities_<category>.geojson`.
- The combined-scan path writes `<pbf-stem>_amenities.geojson` under `osm-combined`.
- Air-quality outputs land under `get_output_base()/osm/airquality/` —
  `airquality-<parameter>.geojson` and `school-exposure.geojson`.
- Result reuse is via the sidecar `output_cache` (`cached_result` /
  `save_result_meta`), keyed on input path + size + facet params — a re-run with an
  unchanged input short-circuits.
- Maps are produced by `osm.viz.RenderMap` (HTML), not this namespace.

## Gotchas & notes

- **Ways collapse to centroids.** A shopping mall polygon becomes a single point;
  this is intentional (uniform clickable layer) but means area is lost — use
  `osm.Buildings` if you need footprints.
- **Full-region "all" extraction is expensive** (`Cost(tier="expensive")`) —
  prefer a `category` filter, or run `CombinedScan` once if you want several
  categories.
- **OpenAQ needs a key.** No `OPENAQ_API_KEY` (or no `requests`) → `FetchAirQuality`
  logs a warning and returns an empty result rather than failing; the downstream
  map then shows zero matched schools. Set the key on the runner host.
- **`FetchAirQuality` uses the bbox centre**, not the full polygon — a large region
  is sampled from one point + `radius_m`, so coverage is coarse by design.
- Non-ASCII names in tags flow through fine in GeoJSON, but see the framework note
  on non-ASCII **FFL literals** (avoid them in `.ffl` source).

## Related specs

- [planet-extraction](planet-extraction.md) — produces the region PBFs consumed here.
- [buildings](buildings.md) — footprint (polygon) extraction, the area-based sibling.
- [poi](poi.md) — settlement/place extraction, which rides the same combined scanner.
- [parks](parks.md), [boundaries](boundaries.md) — the other analysis namespaces.
