# Spatial — distance & geometry relations

**Namespace(s):** `osm.Spatial` · `osm.Spatial.workflows` ·
**FFL:** `src/osm_geocoder/handlers/spatial/ffl/{osmspatial,osmspatial_workflows}.ffl` ·
**Handlers:** `src/osm_geocoder/handlers/spatial/{spatial_handlers,spatial_ops}.py` ·
**Tools:** none (self-contained)

## Overview

`osm.Spatial` is the **"universal verb"** of the OSM toolchain: relate two GeoJSON
layers — a SUBJECT and a REFERENCE — by distance or topology. The same operation
shows up everywhere as Overpass `around`, routing-engine tables/isochrones, turf's
`nearestPoint`/`pointsWithinPolygon`, and PostGIS `ST_DWithin`/`ST_Distance`/KNN;
this namespace makes it a composable primitive over the GeoJSON-path I/O the
Extract/Filter layers already produce.

It is what unlocks compound requests: "food deserts" (population points beyond
1 mi of any supermarket), "schools without a pharmacy within 1 mi", "parks within
a city". The `osm.Spatial.workflows` FFL proves the design claim that such a
request is *composed* from orthogonal primitives, not hand-coded —
`PlacesBeyondReach` is the worked decomposition.

## How it works

Contract, uniform with the rest of the library: **subject GeoJSON path + reference
GeoJSON path (+ distance) → GeoJSON path**, tags preserved so the result feeds any
downstream Filter/Render.

Distances are metric: geometries are reprojected into a **local
azimuthal-equidistant projection centered on the combined data centroid**
(via pyproj `Transformer`), the relation is computed in meters, and outputs are
reprojected back to WGS84. This is accurate at the regional (state/metro) extents
these primitives compose at; continental inputs should be clipped first. The
reference layer is indexed with a **shapely `STRtree`** so nearest/within queries
are sub-linear rather than all-pairs.

The verbs (`spatial_ops.py`):

- **`WithinDistance`** — keep SUBJECT features within `distance` of ANY REFERENCE
  feature; annotate each kept feature with `nearest_distance_m` /
  `nearest_distance` (Overpass `around` / `ST_DWithin`).
- **`BeyondDistance`** — the complement: keep SUBJECT features beyond `distance`
  from EVERY REFERENCE feature (the "X desert" primitive).
- **`Nearest`** — keep ALL SUBJECT features, annotating each with the distance to
  its nearest REFERENCE (`nearest_distance_m/_nearest_distance`, plus
  `nearest_ref_name` when the reference carries a `name`); `distance > 0` caps the
  search radius (PostGIS `<->` KNN / turf `nearestPoint`).
- **`SpatialJoin`** — attach matching REFERENCE properties onto each SUBJECT
  feature by a topological predicate (`intersects`/`within`/`contains`); first
  match's props copied under `prefix` (+ `<prefix>joined_count`); `how="inner"`
  keeps only matched, `how="left"` keeps all (point-in-polygon join /
  `ST_Within` join / GeoPandas `sjoin`).
- **`Buffer`** — expand every feature by `distance` into polygons (`ST_Buffer` /
  turf buffer), in the local equidistant projection then back to WGS84.
- **`Intersect`** — clip each SUBJECT geometry to a CLIP layer (unioned into one
  mask); overlapping part kept, non-overlapping dropped (`ST_Intersection` — the
  geometric cookie-cutter, distinct from `SpatialJoin` which never cuts geometry).
- **`Union`** — merge all geometries into ONE feature (`ST_Union` aggregate);
  with `other_path`, merges both layers (distinct from `Dissolve`, which unions
  per group).
- **`Centroid`** — replace each geometry with its centroid `Point`, 1:1, props
  preserved (`ST_Centroid`).
- **`Simplify`** — Douglas-Peucker simplify at a metric `tolerance` in the local
  equidistant projection (`ST_Simplify`), topology-preserving, props preserved.

## Fan-out

The primitive verbs are single-task (one subject × one reference per call). Fan-out
lives in the **composed workflows**: `osm.Spatial.workflows.PlacesBeyondReach`
decomposes into `CacheRegion → ExtractCategory(amenities) → Filter(tag=value)
[reference] ‖ ExtractCategory(population) [subject] → BeyondDistance → RenderMap`.
The FFL notes that a whole request *family* fans out with an `andThen foreach` at
the workflow — one region per leaf — so N regions relate concurrently across the
fleet; `PlacesBeyondReachFromCache` is the cache-dependent body a foreach spawns
per region. `PlacesBeyondReach` itself parameterizes the family: any
amenity/shop value at any radius over any region ("food deserts" = `shop`,
`supermarket`; "healthcare deserts" = `amenity`, `hospital`).

## Filtering & attributes

The verbs are **tag-agnostic** — they relate by geometry, not by tag. They read/write
these properties:

- **Annotations added** — `nearest_distance_m`, `nearest_distance` (in the chosen
  unit), and `nearest_ref_name` (from the reference's `name`).
- **`SpatialJoin`** — copies the matched reference's properties under `prefix`
  (default `ref_`) and adds `<prefix>joined_count`.
- **`Dissolve`-like output** — `Buffer`/`Union`/`Intersect`/`Centroid`/`Simplify`
  carry the subject's original properties through.

Narrowing the SUBJECT or REFERENCE to specific tags (`amenity=hospital`,
`shop=supermarket`) is done by a [filters](filters.md) step *before* the spatial
verb, exactly as `PlacesBeyondReachFromCache` does with `FilterGeoJSONByOSMType`.

## External libraries / binaries

- **`shapely`** (`>=2.0`) — geometry (`shape`/`mapping`), `unary_union`,
  `shapely.ops.transform` for reprojection, and `shapely.strtree.STRtree`
  (incl. `query_nearest`, hence the `>=2.0` requirement). `HAS_SHAPELY`-guarded.
- **`pyproj`** (`>=3.0`) — `CRS`/`Transformer` for the local azimuthal-equidistant
  projection. `HAS_PYPROJ`-guarded; the distance/buffer/simplify verbs raise
  `RuntimeError("shapely>=2.0 and pyproj>=3.0 are required …")` if either is
  missing. Both are **pip** dependencies — no osmium binary, no engine daemon, no
  network.
- **stdlib** — `math`, `json`, `tempfile`.

## Facets & workflows

`osm.Spatial` (`osmspatial.ffl`) — all `event`, all `with Effect(kind="pure")`
`with Cost(tier="cheap")`, all → a `SpatialResult`:

| Facet | Kind | Purpose |
|---|---|---|
| `WithinDistance(subject_path, reference_path, distance, unit="miles")` | event | Keep subjects within distance of any reference |
| `BeyondDistance(subject_path, reference_path, distance, unit="miles")` | event | Keep subjects beyond distance from every reference ("X desert") |
| `Nearest(subject_path, reference_path, unit="miles", distance=0.0)` | event | Annotate every subject with nearest-reference distance/name |
| `SpatialJoin(subject_path, reference_path, predicate="intersects", prefix="ref_", how="left")` | event | Attach reference props by topology (point-in-polygon join) |
| `Buffer(input_path, distance, unit="miles")` | event | Expand features into service-area polygons |
| `Intersect(subject_path, clip_path)` | event | Clip subjects to a clip mask (cookie-cutter) |
| `Union(input_path, other_path="")` | event | Merge all geometries into one feature |
| `Centroid(input_path)` | event | Replace geometries with their centroid points, 1:1 |
| `Simplify(input_path, tolerance, unit="meters")` | event | Douglas-Peucker simplify at a metric tolerance |

`osm.Spatial.workflows` (`osmspatial_workflows.ffl`):

| Facet / Workflow | Kind | Purpose |
|---|---|---|
| `PlacesBeyondReach(region="California", tag_key="amenity", tag_value="hospital", distance_miles=10.0)` | workflow | Parameterized "X desert" map: places beyond reach of an amenity/shop |
| `PlacesBeyondReachFromCache(cache, tag_key, tag_value, distance_miles)` | facet | Cache-dependent body (one region per leaf; fan out with `foreach`) |

`SpatialResult` fields: `output_path`, `feature_count`, `original_count`,
`reference_count`, `operation`, `distance`, `unit`, `format`, `extraction_date`.

## Cache / output

- Outputs are written under the **`osm-spatial`** output namespace
  (`derive_output_path("osm-spatial", …)`), collision-safe names encoding the
  operation and params.
- Format is **GeoJSON** (WGS84). Streamed to a local temp then finalized into the
  `FW_STORAGE` backend (local disk or MinIO/S3), the shared staged-write pattern.
- The composed `PlacesBeyondReach` workflow's terminal artifact is an HTML map from
  `osm.viz.RenderMap` (published via the render/visualization layer).

## Gotchas & notes

- **Regional accuracy, not continental.** The single centered azimuthal-equidistant
  projection is accurate at state/metro extents; over a continent the projection
  error grows — Clip (or run per-region via the foreach) before relating.
- **Hard dependency on shapely+pyproj.** Unlike some soft-degrading osmium filters,
  the spatial verbs raise if either geometry library is missing — a spatial-capable
  runner must have both.
- **`SpatialJoin` takes the first match only.** Its copied props are from the first
  matching reference feature; overlapping references beyond the first are counted
  (`joined_count`) but not merged.
- **`Union` vs `Dissolve`.** `Union` merges the *whole* layer (or two layers) into
  one feature; group-wise union is `osm.Transform.Dissolve` — pick by whether you
  want one feature or one-per-group.
- **Cost tier is "cheap" but O(n·log n) at best.** The STRtree keeps queries
  sub-linear, but a large subject × large reference relation is still real work;
  bound the subject first (e.g. [`TopNByPopulation`](population.md)).

## Related specs

- [filters](filters.md) — narrows subject/reference layers to the target tags
  before a spatial verb.
- [population](population.md) — the populated-places subject layer and the
  cost-bounding `TopNByPopulation`.
- [transform](transform.md) — `Dissolve` (per-group union) and `MergeLayers`, the
  tabular/aggregation counterparts to these geometric verbs.
- [source-adapters](source-adapters.md) — `ExtractCategory`, the source of both
  layers in the composed workflows.
