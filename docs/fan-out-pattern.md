# The Fleet Fan-out Pattern

**Namespace(s):** cross-cutting (`osm.heatmap`, `osm.Cities.fanout`, `osm.Cities.routes`,
`osm.emergency.flows`, `osm.planet`) ·
**FFL:** `handlers/visualization/ffl/osmheatmap.ffl`,
`handlers/cities/ffl/osmcities_fanout.ffl`, `handlers/cities/ffl/osmcities_routes_fanout.ffl`,
`handlers/emergency/ffl/osm_emergency.ffl`, `handlers/planet/ffl/osmplanet.ffl` ·
**Handlers:** none of its own — it is a *composition* pattern over existing facets

> This is the reference spec for the fan-out idiom the domain uses everywhere. The
> feature specs (emergency-atlas, cities, planet-extraction) link here instead of
> re-explaining it.

## Overview

Most interesting OSM questions are asked at a scale that does not fit one machine:
"Tesla superchargers across **North America**", "cities-by-zoom for a **continent**",
"emergency-access readiness for **every region**". The naive shape — download one
continental PBF (~14–19 GB) and scan it on a single runner — needs a big host, OOMs
`osmium` on a modest box, and runs **serially**: wall-clock is the *sum* of the work.

The fan-out pattern turns that one big job into **N small, self-contained jobs, one
per region leaf**, dispatched as independent distributed tasks that the runner fleet
executes **in parallel**. Each leaf PBF (a US state, a Canadian province) is small
enough to extract on a modest host, and because the tasks are disjoint, **wall-clock
collapses to ≈ the slowest single leaf, not the sum** — adding runners adds throughput
directly. It is the same idea the flagship [planet-extraction](planet-extraction.md)
uses to build county extracts 51 states at a time.

## How it works

Every fan-out in the domain is one `andThen foreach` over a **region list**, wrapped so
the runtime can (a) spawn one task per element and (b) aggregate the per-element results
back into a list. The canonical shape (`osm.heatmap.SubregionChargers`):

```
facet ExtractPlacesAcrossSubregions(region_names: [String], ...) => (place_paths: [String])
andThen {
    resolved = osm.Region.ResolveRegions(names = $.region_names, expand = "subregions")
               andThen foreach r in $.regions {
        cache  = osm.cache.Download(region = $.r, cache_policy = $$.cache_policy)
        places = osm.Population.AllPopulatedPlaces(cache = cache.cache, min_population = $$.min_population)
        yield ExtractPlacesAcrossSubregions(place_paths = [places.result.output_path])
    }
}
```

1. **Enumerate the leaves.** A driver facet returns the region list to fan over.
   Two drivers are used:
   - **`osm.Region.ResolveRegions(names, expand = "subregions")`** — hierarchical
     expansion: one parent name (`"north-america"`) becomes its finest-grained
     Geofabrik leaves (all US states + Canadian provinces + Mexico + Greenland);
     an already-leaf name passes through unchanged, so lists may be mixed freely.
   - **`osm.planet.ListExtracts(prefix)`** — lists the direct-child extract keys
     already published under a prefix in the object store (the planet fan-out driver).
2. **`andThen foreach r in $.regions`** on that step's body spawns **one task per
   leaf**. Each iteration is a complete, independent sub-pipeline (download → extract
   → filter) that runs wherever a runner claims it.
3. **List-typed `yield` merge.** Each iteration yields a **1-element list**
   (`place_paths = [places.result.output_path]`); the runtime concatenates every
   iteration's list into one aggregated list at fan-in. That aggregated list is the
   facet's return, which a downstream *linear* block then consumes (typically
   `osm.Transform.MergeLayers(inputs = ...)` → one merged GeoJSON → render).

Because FFL has **no post-`foreach` aggregation block** in a workflow, the foreach is
placed inside a **facet that returns the aggregated list**, and the workflow's linear
block calls that facet then merges/renders. `osmcities_routes_fanout.ffl` calls this out
explicitly as "the genomics fan-out / fan-in split": the foreach lives in
`ExtractRoadsAndCitiesAcrossSubregions` (returning two path lists), and
`CitiesAndRoutesByZoomFanout` consumes both.

### The step-body foreach scoping rule

The foreach attaches to the **step body** of the resolve step, not to a sibling block,
and this is deliberate — it is what makes the references legal under relative scoping:

- **`$.regions`** on `andThen foreach r in $.regions` names the **containing step**
  (`resolved`)'s own return surface — `resolved` is in scope as the immediate
  container, so `$.regions` reads *its* `regions` output. Writing it as a bare sibling
  reference would trip `REF_CROSS_BLOCK_STEP` (a block may not name another block's
  step). Attaching the foreach to `resolved`'s body keeps it same-block-legal.
- **`$.r`** inside the loop is the loop variable (`foreach r in ...`), bound on the
  loop body's `$` surface — the per-iteration region.
- **`$$.cache_policy`** / **`$$.min_population`** reach **one level up**, to the
  enclosing facet/workflow's parameters. The loop body's `$` is the loop; `$$` is its
  container (the facet), so workflow params are read with the double-dollar hop.

The comment in `osmheatmap.ffl` states the rule verbatim: *"The per-leaf fan-out is
`resolved`'s STEP BODY, so `resolved.regions` names its containing step (in scope)
rather than a sibling block — required by REF_CROSS_BLOCK_STEP. Same fan-out, legal
scoping."* See the framework's relative-scoping rules (`REF_CROSS_BLOCK_STEP`,
`REF_DOLLAR_OVERFLOW`).

### Nested fan-out

Fan-outs compose. [emergency-atlas](emergency-atlas.md) nests **four** levels — regions
→ cities → categories → route-pairs — by making each level a facet whose body is a
`foreach` that yields a 1-element list, each fan-in carried by the child facet's return.
The same three rules (`$.list` names the container, loop var via `$.<var>`, params via
`$$`) hold at every depth.

## Fan-out

This spec *is* the fan-out. The unit and driver per feature:

| Feature | Fan-out unit | Driver | Fan-in |
|---|---|---|---|
| `osm.heatmap.ContinentHeatmap` | per subregion (state/province) | `ResolveRegions(expand="subregions")` | `MergeLayers` → `RenderHeatmap` |
| `osm.Cities.fanout.CitiesByZoomTiledMapFanout` | per subregion | `ResolveRegions(expand="subregions")` | `MergeLayers` → tier → tile → render |
| `osm.Cities.routes.CitiesAndRoutesByZoomFanout` | per subregion | `ResolveRegions(expand="subregions")` | two lists → merge → network → route → render |
| `osm.emergency.flows.ContinentalEmergencyAtlas` | per region, then city, category, route-pair (4 nested) | `region_names` param → nested foreachs | child-facet returns at each level |
| `osm.planet.BuildAdminFanout` | per direct-child extract (e.g. per state) | `ListExtracts(prefix)` | `published: [Long]` |

Observed live at **8 states across 8 hosts at once** on the planet fan-out; the whole
list runs concurrently across the `osm` task-list's runners.

### When to fan out vs single-task

The decision rule is stated in the FFL comments themselves:

- **Fan out** when the input is a **continent or large country** and there are **several
  `osm` runners**: the per-leaf PBFs are small and embarrassingly parallel, so N leaves
  finish in ≈ one leaf's time. `osmcities_fanout.ffl`:
  `>= several osm runners → fan out per subregion`.
- **Single-task (monolithic)** when there is **one runner** (the foreach iterations
  would serialise anyway, and one continental scan avoids N download/merge overheads),
  or the region is **already a single small leaf** (`AmenityHeatmap`,
  `CitiesByZoomTiledMap` — the non-fanout siblings), or the job is **atomic and needs no
  cross-host handoff** (`osm.planet.BuildAdminSet` is single-atomic precisely because a
  multi-step workflow passing local file paths breaks on the fleet's per-host scratch
  disk — see [planet-extraction](planet-extraction.md)).

The fanout and monolithic workflows are kept as **siblings** with the same params and
outputs (`CitiesByZoomTiledMap` vs `...Fanout`), so switching strategy is a workflow
choice, not a rewrite.

## Filtering & attributes

The pattern itself filters nothing — it is a scheduling shape. The per-leaf sub-pipeline
does whatever filtering its facets do: `osm.Filters.ByScript` predicates over `props`
(heatmaps), `place=city|town` + population tags (cities/emergency), `highway` road-class
filters (routes). See the per-feature specs.

## External libraries / binaries

None introduced by the pattern. The leaves call the same facets as the single-task path:
`osmium` (extraction), `shapely`/`pyproj` (geometry), the in-process `osm.Network` graph
search (routing) — documented in the feature specs. The only runtime machinery the
pattern leans on is the **list-typed yield-merge** and **per-task claiming** in the
framework runtime, not a library.

## Facets & workflows

| Facet / Workflow | Kind | Role in the pattern |
|---|---|---|
| `osm.Region.ResolveRegions(names, expand)` | event (pure) | Fan-out driver — expands parents to leaves |
| `osm.planet.ListExtracts(prefix)` | event (io) | Fan-out driver — lists published child extracts |
| `osm.Transform.MergeLayers(inputs: [String])` | event (pure) | Canonical fan-in — concatenates per-leaf GeoJSONs |
| `...AcrossSubregions` facets | facet | Hold the `foreach`, return the aggregated list(s) |
| `ContinentHeatmap` / `...Fanout` / `ContinentalEmergencyAtlas` / `BuildAdminFanout` | workflow | Entry points that fan out then fan in |

`ResolveRegions` and `MergeLayers` are `with Effect(kind = "pure") with Cost(tier =
"cheap")`; the per-leaf extract/download facets carry heavier effect/cost.

## Cache / output

The pattern adds no cache namespace of its own. Each leaf writes into its facet's normal
cache (e.g. `osm/pbf` for downloads, `osm-cities`, `emergency`) and the merged result is
written once by the fan-in facet. On the fleet, per-leaf artifacts are portable
`s3://afl-cache/...` URIs so any runner can produce a leaf and any other runner can merge
them — no shared disk needed. Keeping outputs as URIs is what lets the fan-in run on a
different host than the leaves.

## Gotchas & notes

- **Yield a 1-element list, not a scalar.** The list yield-merge is what aggregates the
  fan-out; yielding `place_paths = [x]` (not `= x`) is load-bearing. Every fan-out facet
  in the domain wraps its per-iter output in `[...]`.
- **`$$` depth is exact.** From a loop body, workflow params are `$$` (one hop past the
  loop). Nest another level and it becomes `$$$`. Overshooting the outermost container is
  `REF_DOLLAR_OVERFLOW`.
- **One runner → don't fan out.** With a single `osm` runner the foreach serialises and
  you pay N download/merge overheads for no parallelism — use the monolithic sibling.
- **Bounded concurrency for disk-heavy tiers.** The download/extract tier is disk-heavy;
  `osm_emergency.ffl` notes continent-scale runs should bound region concurrency, and the
  flagship's county fan-out surfaced a scratch-collision bug fixed with per-task UUID
  scratch dirs. A wide fan-out can also fill a runner's Docker VM disk — serialise or
  disk-guard the heaviest ones.
- **Empty leaves.** A leaf that legitimately has zero features still yields an (empty)
  path; downstream `MergeLayers` and renderers tolerate sparse/empty inputs so one barren
  state doesn't fail the run. The emergency atlas goes further with per-region `catch` →
  `RegionFailure` so a bad region degrades to a disclosed "excluded" row.

## Related specs

- [planet-extraction](planet-extraction.md) — `BuildAdminFanout`, the flagship fan-out
  (per-state county builds) and the single-atomic vs fan-out trade-off in depth.
- [emergency-atlas](emergency-atlas.md) — the 4-level nested fan-out/fan-in.
- [cities](cities.md) — the cities-by-zoom fanout vs monolithic sibling pair.
- [composed-workflows](composed-workflows.md) — linear (non-fan-out) composition, the
  other half of the composition story.
