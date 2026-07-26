# Filtering & Data-Quality

**Namespace(s):** `osm.Filters` · `osm.ops.OSMOSE` · `osm.ops.Validation` ·
**FFL:** `src/osm_geocoder/handlers/filters/ffl/{osmfilters,osmosmose,osmvalidation}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/filters/{filter_handlers,osm_type_filter,radius_filter,osmose_handlers,osmose_verifier,validation_handlers}.py` ·
**Tools:** `tools/_osm_tools/geojson_filter.py` (the `ByScript` engine, shared with the `tools/filter_geojson.py` CLI)

## Overview

The filters layer is the **narrow** step of the pipeline: it takes the full
GeoJSON (or PBF) a source adapter produced and keeps only the features a request
cares about, then hands a smaller GeoJSON path downstream to Transform / Spatial /
render. It also carries the **data-quality** siblings — `osm.ops.OSMOSE` (a
network-free local re-implementation of Osmose-style QA) and `osm.ops.Validation`
— which do not narrow the data but score it and emit an issues layer.

Three filter mechanisms coexist, in increasing expressiveness: **radius** (a
geometric property of boundary polygons), **type/tag** (osmium/PBF or GeoJSON
`key=value` and its prefix/contains/regex variants), and **`ByScript`** — an
arbitrary sandboxed Python predicate over each feature's tags. Everything
preserves properties, so a filtered layer feeds any downstream Filter / Spatial /
RenderMap unchanged.

## How it works

Data shape at each stage: **PBF → GeoJSON → filtered GeoJSON**. Filters split by
whether they read raw PBF or already-extracted GeoJSON.

- **Radius** (`radius_filter.py`) — streams a GeoJSON of boundary polygons,
  computes each polygon's **equivalent circular radius** `r = sqrt(area / π)`
  (area via a pyproj Albers-equal-area projection centered on the polygon's
  centroid; spherical fallback if pyproj is absent), and keeps features whose `r`
  satisfies an operator (`gt/gte/lt/lte/eq/ne/between`) against a threshold in
  meters/kilometers/miles. Kept features are annotated with `equivalent_radius_m`
  / `equivalent_radius_km`. `ExtractAndFilterByRadius` fuses a boundary extraction
  (osmium) and the radius filter into one PBF-in step.
- **Type / tag on PBF** (`osm_type_filter.py`) — a `pyosmium` `SimpleHandler`
  walks nodes/ways/relations, keeps those matching an element type
  (`node`/`way`/`relation`/`*`) and optional `tag_key`/`tag_value`, and builds
  GeoJSON. Ways become a `Polygon` when the way is closed **and** carries an
  area-ish tag (`building`/`landuse`/`natural`/`leisure`/`amenity`/`boundary`, or
  `area=yes`), else a `LineString`. `include_dependencies` runs a second pass to
  collect referenced node coordinates so way geometry can be reconstructed.
- **Type / tag on GeoJSON** (`osm_type_filter.py`) — streaming predicate filters
  over `properties`: exact (`FilterGeoJSONByOSMType`, matches `osm_type` and/or a
  tag equal to a value), **prefix** (`filter_geojson_by_tag_prefix`), **contains**
  (`filter_geojson_by_tag_contains`, case-insensitive by default), and **regex**
  (`filter_geojson_by_tag_regex`, `re.search`). These stream features to a local
  temp file (multi-GB safe, avoids VirtioFS write stalls) and finalize into the
  storage backend.
- **`ByScript`** — the full-Python escape hatch, detailed below.

Data-quality handlers make a single pyosmium pass (`osmose_verifier.py`,
`validation_handlers.py`) and write an issues GeoJSON plus a summary — no
narrowing of the input.

### The `ByScript` mechanism

`osm.Filters.ByScript(input_path, script) => (output_path, feature_count, total)`
is the most expressive filter. The `script` string is interpreted by
`_osm_tools.geojson_filter.compile_filter` in one of two ways:

1. **Boolean expression** (default) — compiled with `compile(src, …, "eval")` and
   evaluated **once per feature**, with these names in scope:
   - `feature` — the whole GeoJSON feature dict
   - `props` / `tags` — its `properties` dict (both names alias the same dict)
   - `geom` — its `geometry` dict

   Example (from the FFL docstring — Tesla charging stations):

   ```python
   props.get("amenity") == "charging_station" and (
       "tesla" in props.get("operator", "").lower()
       or "tesla" in props.get("brand", "").lower()
       or any(k.startswith("socket:tesla") for k in props)
   )
   ```

2. **`def keep(feature):`** — if the literal `"def keep"` appears in the script,
   the whole script is `exec`'d and must define a callable `keep(...)` returning a
   truthy value to keep the feature:

   ```python
   def keep(feature):
       p = feature["properties"]
       return p.get("amenity") == "charging_station" and "Tesla" in str(p)
   ```

The predicate is compiled **once** and applied per feature. Features stream in
(brace-matched, truncation-tolerant reader — a partially written producer only
loses its final partial feature) and kept features stream out to a
`{"type":"FeatureCollection","features":[…]}` document, so tens-of-MB / hundreds
of thousands of features never sit in memory at once. The output path is the input
path with a `_filtered_<sha1(script)[:8]>` suffix, so different scripts over one
input do not collide. It is storage-native: an `s3://` / `hdfs://` input is
localized and the output finalized back through the `Storage` abstraction (works
against MinIO). The identical implementation backs the `filter-geojson` CLI, so
there is exactly one filtering code path.

`tag_predicate(spec)` is a convenience layer on the same engine: `key=value` for
exact, `key~substr` for case-insensitive substring, `|` for OR — it compiles down
to a `ByScript` expression.

### Sandbox

Scripts run in a **restricted namespace** — `__builtins__` is replaced by a small
allow-list (`abs, all, any, bool, dict, enumerate, filter, float, int, isinstance,
len, list, map, max, min, round, set, sorted, str, sum, tuple, zip, range`, plus
`True/False/None`) with `re` and `math` available. There is **no** `open`, `eval`,
`exec`, `import`/`__import__`, or introspection helper — no file/process/network
access, the same spirit as FFL `script python` blocks. A predicate that **raises**
on a given feature simply **drops that feature** and increments an `errors`
counter rather than aborting the whole filter. A **syntax** error, by contrast,
raises `FilterError` up front.

## Fan-out

Single-task per invocation — **no fan-out inside this namespace**. Each filter is
one event task over one input path. Fan-out is expressed *above* filters: an
`andThen foreach` at the workflow level runs one Extract→Filter per region/leaf,
then `osm.Transform.MergeLayers` aggregates the per-leaf outputs (see
[transform](transform.md)). Filters are pure, cheap, and idempotent, which is what
makes them safe to fan out.

## Filtering & attributes

Concrete attributes these facets key on:

- **Radius** — polygon geometry only (no tag); operates on boundary polygons
  (admin areas, natural features) by their equivalent radius.
- **Element type** — `node` / `way` / `relation` (osmium element kind), or
  `osm_type` stored in GeoJSON `properties`.
- **Tags, exact** — any `key=value`: `amenity=hospital`, `shop=supermarket`,
  `highway=motorway`, `building=*`, `boundary=administrative` (+ `admin_level`).
- **Tags, prefix** — e.g. `ref` starts-with `"I "` keeps Interstate freeways
  (`I 5`, `I 80`) and drops `US 101` / `CA 1`.
- **Tags, contains** — e.g. `name` contains `"Starbucks"` (case-insensitive
  unless `case_sensitive`).
- **Tags, regex** — e.g. `cuisine` matches `pizza|italian` (`re.search`).
- **`ByScript`** — any Python predicate over the whole tag dict: multi-tag
  conditions, `any(k.startswith(...))` over keys, numeric comparisons, etc.
- **Polygon-vs-line heuristic** (PBF → GeoJSON) — a closed way is a `Polygon`
  only if it also carries `building`/`landuse`/`natural`/`leisure`/`amenity`/
  `boundary` or `area=yes`.

The OSMOSE / Validation quality checks read tag *presence*, not values: named
feature types (`amenity`, `shop`, `tourism`, `leisure`, `office`, `building`,
`highway`, `railway`, `aeroway`, `waterway`, `place`, `historic`, `natural`) are
flagged when missing a `name`; polygon-tagged (`building`/`landuse`/`natural`/
`leisure`/`amenity`/`area`/`boundary`/`place`) ways are flagged when unclosed.

## External libraries / binaries

- **`pyosmium`** (pip `osmium`) — PBF reading for `FilterByOSMType`/`FilterByOSMTag`,
  `ExtractAndFilterByRadius`, and the OSMOSE/Validation single-pass verifiers. A
  **pip** dependency (the C++ libosmium is bundled in the wheel); no separate
  osmium-tool binary is invoked here.
- **`shapely`** — geometry parsing (`shape`) and area for the radius filter;
  guarded by `HAS_SHAPELY`.
- **`pyproj`** — Albers-equal-area projection for accurate geodesic area in the
  radius filter (optional; spherical fallback logs a warning). Guarded by
  `HAS_PYPROJ`.
- **stdlib** — `re` / `math` (exposed to `ByScript`), `json`, `hashlib`.

No network, no external service — all three sub-namespaces are local/offline.

## Facets & workflows

`osm.Filters` (`osmfilters.ffl`) — all `event`, all `with Effect`/`with Cost`:

| Facet | Kind | Effect/Cost | Purpose |
|---|---|---|---|
| `FilterByRadius` | event | pure / cheap | Keep GeoJSON polygons by equivalent radius + operator |
| `FilterByRadiusRange` | event | pure / cheap | Keep polygons whose radius is in an inclusive range |
| `FilterByTypeAndRadius` | event | pure / cheap | Radius filter restricted to a boundary type |
| `ExtractAndFilterByRadius` | event | external / expensive | Extract boundaries from PBF then radius-filter, one step |
| `FilterByOSMType` | event | external / expensive | PBF → GeoJSON by element type (+ optional deps) |
| `FilterByOSMTag` | event | external / expensive | PBF → GeoJSON by tag key/value (+ optional deps) |
| `FilterGeoJSONByOSMType` | event | pure / cheap | GeoJSON by `osm_type` and/or exact tag value |
| `FilterGeoJSONByTagPrefix` | event | pure / cheap | GeoJSON where a tag value starts-with a prefix |
| `FilterGeoJSONByTagContains` | event | pure / cheap | GeoJSON where a tag value contains a substring |
| `FilterGeoJSONByTagRegex` | event | pure / cheap | GeoJSON where a tag value matches a regex |
| `ByScript` | event | pure / cheap | GeoJSON by an arbitrary sandboxed Python predicate |

`osm.ops.OSMOSE` (`osmosmose.ffl`) — local QA verifier, all `event` pure/cheap:
`VerifyAll`, `VerifyGeometry`, `VerifyTags`, `VerifyGeoJSON`, `ComputeVerifySummary`
→ a `VerifyResult` + `VerifySummary` (severity-graded issue counts: level 1 error,
2 warning, 3 info).

`osm.ops.Validation` (`osmvalidation.ffl`) — quality validation, all `event`
pure/cheap: `ValidateCache`, `ValidateGeometry`, `ValidateTags`, `ValidateBounds`,
`ValidationSummary` → `ValidationStats` + `ValidationResult`.

## Cache / output

- Every `osm.Filters` handler wraps a **result cache** keyed on the input
  file (path + size) and the filter params (`shared.output_cache.cached_result` /
  `save_result_meta`) — a re-run with identical inputs returns the cached result.
- Filtered GeoJSON is written under the **`osm-filtered`** output namespace
  (`resolve_output_dir("osm-filtered")` / `derive_output_path("osm-filtered", …)`),
  with collision-safe names encoding the filter params.
- `ByScript` writes `<input>_filtered_<scripthash>.<ext>` next to its input.
- OSMOSE issues → **`osm-osmose`** (`verify-issues.geojson`); Validation →
  `FW_OUTPUT_BASE/osm/validation`.
- Outputs go wherever `FW_STORAGE` points — local disk or MinIO/S3 (streamed to a
  local temp then finalized). Format is always **GeoJSON** (or JSON for summaries).

## Gotchas & notes

- **`ByScript` empty-input is fatal by design.** An empty `input_path` raises
  rather than silently succeeding — a missing upstream `output_path` otherwise
  produces a silent empty layer (and an empty map). A *per-feature* predicate
  error is non-fatal (dropped + counted).
- **Sandbox is allow-list, not a jail.** It blocks imports/IO but is not a
  hardened security boundary — treat filter scripts as trusted-author code.
- **`pyosmium` gate.** PBF filters degrade to a zero-feature result (not an error)
  when `osmium`/`shapely` is missing, so a mis-provisioned runner returns empty
  rather than failing loudly — check counts.
- **Radius accuracy.** Without `pyproj`, area falls back to a spherical
  approximation (logged) — fine for coarse thresholds, not survey-grade.
- **OSMOSE is *not* the hosted Osmose service.** It is a self-contained local
  re-implementation of the same class of checks (reference integrity, coordinate
  range, degenerate/unclosed geometry, duplicate IDs, missing-name, empty tag
  values) over a PBF/GeoJSON — no network dependency.

## Related specs

- [transform](transform.md) — the merge/summarize/dissolve layer filters feed.
- [spatial](spatial.md) — distance relations that consume filtered layers.
- [population](population.md) — the population-specific filter family.
- [vocab](vocab.md) — NL term → `key=value` to drive the tag filters.
- [planet-extraction](planet-extraction.md) — the upstream PBF source.
