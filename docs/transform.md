# Transform — combine / reduce / dissolve

**Namespace(s):** `osm.Transform` ·
**FFL:** `src/osm_geocoder/handlers/transform/ffl/osmtransform.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/transform/{transform_handlers,transform_ops}.py` ·
**Tools:** none (self-contained)

## Overview

`osm.Transform` is the **category-agnostic reduction layer** of the composable
library. Where the category-specific statistics facets (`AmenityStatistics`,
`ParkStatistics`, …) know about their domain, Transform knows nothing about tags —
it just combines, reduces, and dissolves GeoJSON layers. It closes item 3 of the
composable-library design ("combine, reduce, dissolve") and, crucially, it is the
**aggregator on the far side of a fan-out**: many per-region/per-category layers
come out of a `foreach`, `MergeLayers` concatenates them, then `Summarize` or a
renderer consumes the single merged layer.

All three facets take and produce the same GeoJSON-path I/O the Extract / Filter /
Spatial layers already speak, so they compose freely with no glue.

## How it works

Data shape: **N GeoJSON paths → 1 GeoJSON (or 1 summary JSON)**. Each facet streams
its inputs via the shared `geojson_writer` helpers and writes a collision-safe,
cache-addressed artifact.

- **`MergeLayers(inputs: [String])`** — `merge_layers`: concatenates the features
  of every input layer into one `FeatureCollection`, properties verbatim. Each
  input is localized (S3/HDFS→local) and streamed; a **missing or unlocalizable
  input is logged and skipped, not fatal** — a fan-out legitimately yields some
  empty leaves. `group_count` reports how many layers actually merged.
- **`Summarize(input_path, group_by, measure, op)`** — `summarize`: one streaming
  pass building per-group accumulators. With no `group_by` it is a single bucket
  (a plain count/aggregate); with `group_by` it buckets by `properties[group_by]`.
  `op ∈ {count, sum, avg, min, max}`; `count` ignores `measure`, the others
  aggregate `float(properties[measure])` (non-numeric values skipped). The full
  per-group breakdown is written as **JSON**, and the result carries a short
  headline `detail` (the top group by value, or the plain total).
- **`Dissolve(input_path, group_by)`** — `dissolve`: groups features by
  `properties[group_by]`, then replaces each group with **one** feature whose
  geometry is the `shapely.ops.unary_union` of the group's geometries (the PostGIS
  `ST_Union … GROUP BY` / turf-dissolve primitive). Each output feature carries the
  group key and a `dissolved_count`. Geometry is buffered in memory **per group**
  (not per whole layer); empty/malformed geometries are skipped with a warning.

## Fan-out

Single-task per invocation — **no fan-out inside this namespace**; Transform is the
thing you point a fan-out *at*. The canonical shape from the FFL header comment:

```
andThen foreach region -> Extract -> Filter   [per-leaf layers]
  -> MergeLayers(inputs=[…each leaf output…])  [the aggregator]
  -> Summarize / RenderMap
```

`MergeLayers` is deliberately tolerant of missing inputs precisely because a
fleet fan-out may produce some empty or absent leaves; it converges on whatever
arrived rather than failing the aggregation.

## Filtering & attributes

Does no tag filtering itself — it is tag-agnostic. It only *reads* the tag named
by `group_by` (Summarize / Dissolve) and the numeric property named by `measure`
(Summarize). Any tag works: `group_by="amenity"`, `group_by="admin_level"`,
`measure="population"`, etc. Narrowing to specific tags is the job of the
[filters](filters.md) layer upstream.

## External libraries / binaries

- **`shapely`** (`>=2.0`) — `shape` / `mapping` / `unary_union` for `Dissolve`
  only (`HAS_SHAPELY`-guarded; `Dissolve` raises `RuntimeError` if absent).
  `MergeLayers` and `Summarize` are pure-stdlib.
- **stdlib** — `json`, `tempfile`, `shutil`. No PBF, no osmium, no network.

## Facets & workflows

`osm.Transform` (`osmtransform.ffl`) — all `event`, all `with Effect(kind="pure")`
`with Cost(tier="cheap")`, all → a `TransformResult`:

| Facet | Kind | Purpose |
|---|---|---|
| `MergeLayers(inputs: [String])` | event | Concatenate several GeoJSON layers into one FeatureCollection (the fan-out aggregator) |
| `Summarize(input_path, group_by="", measure="", op="count")` | event | Reduce a layer to a count/sum/avg/min/max, optionally grouped by a tag; breakdown written as JSON |
| `Dissolve(input_path, group_by)` | event | Union each group's geometries into one feature per group, with `dissolved_count` |

`TransformResult` fields: `output_path`, `feature_count`, `original_count`,
`group_count`, `operation`, `detail`, `format`, `extraction_date`.

## Cache / output

- Artifacts are written under the **`osm-transform`** output namespace
  (`derive_output_path("osm-transform", …)`), with collision-safe names encoding
  the operation and its params (`merged`/`summary`/`dissolved`).
- `MergeLayers` and `Dissolve` emit **GeoJSON**; `Summarize` emits **JSON**
  (`format="JSON"`).
- Outputs stream to a local temp file then finalize into the `FW_STORAGE` backend
  (local disk or MinIO/S3), the same staged-write pattern the filters use.

## Gotchas & notes

- **`Summarize` non-count ops require a `measure`** — `op != "count"` with an
  empty `measure` raises `ValueError`. Non-numeric `measure` values are silently
  skipped per feature (they don't abort the pass).
- **`Dissolve` needs `group_by`** and `shapely>=2.0`; both are hard requirements
  (raise on absence), unlike the soft-skip of a missing merge input.
- **`Dissolve` memory** — geometry is buffered per group; a single enormous group
  (e.g. dissolving a whole country's buildings under one key) holds all those
  geometries in memory at once.
- **`MergeLayers` does not dedupe** — overlapping inputs produce duplicate
  features; dedupe upstream if that matters.

## Related specs

- [filters](filters.md) — the narrowing layer that produces the per-leaf inputs.
- [spatial](spatial.md) — geometric ops (buffer/union/clip) that also reduce
  layers, the geometry-heavy counterpart to Transform's tabular reductions.
- [population](population.md) — a category layer commonly summarized/merged.
