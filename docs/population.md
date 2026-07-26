# Population Filtering

**Namespace(s):** `osm.Population` ·
**FFL:** `src/osm_geocoder/handlers/population/ffl/osmfilters_population.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/population/{population_handlers,population_filter}.py` ·
**Tools:** none (self-contained)

## Overview

`osm.Population` filters and ranks OSM places and administrative areas by their
`population` tag. It answers "the cities of California over 100 000", "the top 50
most-populous places in a band", or "every populated place with a population tag",
producing a GeoJSON of `Point` features (or filtered polygons) that downstream
Spatial / render steps consume.

It sits at the boundary between **extraction** (walk a regional PBF for populated
places) and **filtering** (narrow an already-extracted GeoJSON by population). The
`TopNByPopulation` facet exists specifically to **bound cost** for a downstream
all-pairs router: it caps a dense low-population band to its largest cities so an
O(n²) routing step stays tractable.

## How it works

Data shape: **PBF → GeoJSON places** (extraction) or **GeoJSON → GeoJSON**
(filter/rank).

- **Extraction** (`extract_places_with_population`) — a `pyosmium` `SimpleHandler`
  walks **nodes only**, keeping any node that carries a `place` **or** `population`
  tag and matches the requested place type. `population` is parsed leniently
  (`parse_population` strips `~`/`≈`/`+`, thousands separators in either English
  or European convention, and non-digits) and, when `min_population > 0`, only
  places at/above the threshold are kept. Every OSM tag is preserved; the parsed
  value is added as `population_value`. Output is a GeoJSON `Point`
  FeatureCollection, streamed to a temp file and finalized into storage.
- **Filter** (`filter_geojson_by_population`) — loads a GeoJSON, keeps features
  whose parsed `population` satisfies an operator (`gt/gte/lt/lte/eq/ne/between`)
  against `min_population` (and `max_population` for `between`), constrained to a
  place type.
- **Rank** (`top_n_by_population`) — sorts features by parsed population descending
  (`population_value` then `population`; unparseable sorts last) and keeps the top
  `n` (`n < 0` keeps all, sorted).
- **Stats** (`calculate_population_stats`) — totals/min/max/avg over the parsed
  populations of matching features.

Place-type matching (`matches_place_type` + `PLACE_TAGS`) maps a type to its OSM
tags — a `place` value and/or a `boundary=administrative` + `admin_level` pair.

## Fan-out

Single-task per invocation — **no `foreach` in this namespace's FFL**. Population
extraction/filtering runs as one event task per region. Fan-out across regions is
expressed at a workflow above it (an `andThen foreach` over regions →
`ExtractPlacesWithPopulation`/`AllPopulatedPlaces` → merge), the same aggregation
pattern the rest of the library uses. `TopNByPopulation` is the *cost-bounding*
counterpart: it shrinks the subject layer **before** an expensive all-pairs
`osm.Network.RouteLayer` fan-out so that step does not blow up to millions of
pairs.

## Filtering & attributes

Keys it reads:

- **`population`** — the primary attribute; parsed leniently (see above).
- **`place`** — `city`, `town`, `village`, `hamlet`, `suburb`
  (+ `neighbourhood`/`quarter`), `country`, `state`/`province`, `county`,
  `municipality`.
- **`boundary=administrative` + `admin_level`** — for admin-area place types:
  country `admin_level=2`, state/province `=4`, county `=6`, municipality `=8`.
- **`name`, `osm_id`** — preserved into output properties (and `population_value`
  added).

`place_type="all"` matches any node that simply has a `population` tag.

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — node scan for PBF place extraction; a **pip**
  dependency (probed via `importlib.util.find_spec`). No osmium-tool binary.
- **stdlib only otherwise** — `json`, `re` (population parsing). No shapely/pyproj,
  no network.

## Facets & workflows

`osm.Population` (`osmfilters_population.ffl`) — all `event`. GeoJSON-in facets are
`pure`/`cheap`; PBF-in extraction facets are `external`/`expensive`:

| Facet | Kind | Effect/Cost | Handler status |
|---|---|---|---|
| `FilterByPopulation(input_path, min_population, place_type="all", operator="gte")` | event | pure / cheap | **registered** |
| `FilterByPopulationRange(input_path, min_population, max_population, place_type="all")` | event | pure / cheap | **registered** |
| `TopNByPopulation(input_path, n=50)` | event | pure / cheap | **registered** (cost bound for routing) |
| `PopulationStatistics(input_path, place_type="all")` | event | pure / cheap | **registered** |
| `AllPopulatedPlaces(cache, min_population=0)` | event | external / expensive | **registered** (`timeout_ms=0`, long-running) |
| `ExtractPlacesWithPopulation(cache, place_type="all", min_population=0)` | event | external / expensive | **declared, not registered** |
| `Cities(cache, min_population=0)` | event | external / expensive | **declared, not registered** |
| `Towns(cache, min_population=0)` | event | external / expensive | **declared, not registered** |
| `Villages(cache, min_population=0)` | event | external / expensive | **declared, not registered** |
| `Countries(cache)` | event | external / expensive | **declared, not registered** |
| `States(cache)` | event | external / expensive | **declared, not registered** |
| `Counties(cache)` | event | external / expensive | **declared, not registered** |

Returns: filter/extract facets → `PopulationFilteredFeatures` (paths, counts, place
type, min/max population); `PopulationStatistics` → `PopulationStats`
(total_places, total/min/max/avg population).

## Cache / output

- Extraction/filter handlers wrap the shared **result cache** (`cached_result` /
  `save_result_meta`) keyed on the input + params.
- GeoJSON output is written under the **`osm-population`** output namespace
  (`resolve_output_dir("osm-population")`), e.g. `<stem>_places_<type>.geojson`,
  `<stem>_pop_<min>[_<max>].geojson`, `<stem>_top<n>.geojson`.
- Format is **GeoJSON** (`Point` features for extraction). Streamed to a local temp
  then finalized into `FW_STORAGE` (local disk or MinIO/S3).

## Gotchas & notes

- **Per-type facets are declared but unwired.** `Cities`, `Towns`, `Villages`,
  `Countries`, `States`, `Counties`, and `ExtractPlacesWithPopulation` appear in
  the FFL, and the handler factory (`_make_extract_places_handler`) already
  supports a `fixed_place_type`, but `POPULATION_FACETS` only registers
  `FilterByPopulation`, `FilterByPopulationRange`, `TopNByPopulation`,
  `PopulationStatistics`, and `AllPopulatedPlaces`. Use `AllPopulatedPlaces`
  (or `ExtractPlacesWithPopulation` once wired) with an explicit `place_type`
  instead of the aliases, or add the aliases to `POPULATION_FACETS`.
- **Location index is deliberately skipped.** Extraction reads `n.location`
  directly and does **not** enable the osmium flex-mem location index — that index
  is dead overhead for a node-only scan and dominated runtime on full regions
  (California ~25 min → ~2 min). Do not "fix" this by re-adding `locations=True`.
- **Lease keepalive.** `AllPopulatedPlaces` walks a multi-GB PBF in a blocking
  C++ loop whose in-loop heartbeat only fires per 5 000 *kept* nodes — a
  high-threshold filter keeps few, so a background keepalive thread ticks the task
  heartbeat every interval and the facet registers with `timeout_ms=0` (falls back
  to the runner's global execution timeout).
- **Lenient parsing is lossy.** `parse_population` guesses English vs European
  separators heuristically; ambiguous values (`1.234`) are interpreted as
  thousands only when they look like it. Unparseable populations are dropped from
  stats/filters and sort last in `TopNByPopulation`.

## Related specs

- [filters](filters.md) — the general tag/type/radius/script filters this
  specializes for population.
- [spatial](spatial.md) — `TopNByPopulation` bounds the input to `RouteLayer`
  and the distance verbs (e.g. "population beyond reach of a hospital").
- [transform](transform.md) — merge/summarize per-region population layers.
