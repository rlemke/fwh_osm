# FFL Examples — `osm-geocoder`

Every numbered scenario is a **complete, compilable FFL file**. Copy one into
`my.ffl` and run it against the domain's FFL:

```bash
fw ffl run --primary my.ffl \
  --library ~/fw_handlers/fwh_osm/src/osm_geocoder/ffl/geocoder.ffl \
  --library ~/fw_handlers/fwh_osm/src/osm_geocoder/handlers/amenities/ffl/osmamenities.ffl \
  --library ~/fw_handlers/fwh_osm/src/osm_geocoder/handlers/filters/ffl/osmfilters.ffl \
  --workflow my.osm.<WorkflowName> --task-list osm
```

There are ~84 FFL files in this domain, spread over `src/osm_geocoder/ffl/` and
`src/osm_geocoder/handlers/*/ffl/` — pass the ones your workflow uses, or
`fw ffl seed --include osm-geocoder` once and refer to the seeded namespaces. A
runner serving the `osm` namespace must be up
(`fw runner start --domain osm-geocoder`). Every block below is compile-checked
against the domain's FFL.

New to the language? Start with the
[FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md)
and the [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical).

---

## The building blocks

The largest domain in the fleet. Four things carry most of the weight:

| Declaration | Role |
|---|---|
| `osm.Region.ResolveRegion(name, prefer_continent) => (region: Region)` | Name → a canonical Geofabrik region (`ResolveRegions` for a list, `ListRegions` to enumerate) |
| `osm.cache.Download(region: Region, cache_policy = "prefer_cache") => (cache: OSMCache)` | Download-or-reuse the region's PBF — **idempotent**, `expensive` |
| `osm.Vocab.ResolveTag(term, key) => (result: TagResolution)` | NL term → `key=value` (the semantic-lookup primitive), `pure`/`free` |
| `osm.Amenities.*` / `osm.Buildings.*` / `osm.Boundaries.*` | Extract features from an `OSMCache` (`expensive`) |
| `osm.Filters.*` (`FilterByRadius`, `FilterGeoJSONByOSMType`, `FilterByOSMTag`, `ByScript`, …) | Refine an extracted GeoJSON — mostly `pure`/`cheap` |
| `osm.geocode.Geocode` / `ReverseGeocode` | Address ⇄ coordinate |
| `osm.cache.<Continent>.<Region>()` | Named per-region cache facets (`osm.cache.UnitedStates.UnitedStates()`, …) |
| `osm.RegionMap.*`, `osm.heatmap.*`, `osm.emergency.*`, `osm.planet.*` | Shipped map/atlas/planet pipelines |

The **cost tiers are the design**: pull the PBF once (`expensive`), then chain
`pure`/`cheap` filters over the extracted GeoJSON. `fw_capabilities(max_cost=…)`
searches on exactly those mixins.

---

## 1. Run what ships — no FFL to write

```bash
fw ffl seed --include osm-geocoder

fw ffl run --workflow osm.geocode.GeocodeAddress \
  --inputs '{"address": "1600 Pennsylvania Ave NW, Washington DC"}' --task-list osm

fw ffl run --workflow osm.RegionMap.HikingElevationMapByRegion \
  --inputs '{"region": "Oregon", "min_elevation_ft": 3000}' --task-list osm
```

Write FFL when you want a different *shape* — your own extraction + filter chain,
a fan-out over regions, or a new map over the same cached PBFs.

## 2. The smallest workflow you can write

Every FFL workflow needs a `namespace`, a `use` per namespace it calls into, and a
`yield` back to itself.

```ffl
namespace my.osm {

    use osm.geocode

    /** Geocode one address. */
    workflow FindIt(address: String) => (lat: Double, lon: Double) andThen {

        loc = osm.geocode.Geocode(address = $.address)

        yield FindIt(lat = loc.result.lat, lon = loc.result.lon)
    }
}
```

Rules visible above: `=>` sits on the **same line** as the closing `)`; references
are always `step.field` and schema results nest one level (`loc.result.lat`);
`$.address` reads the workflow's parameter.

## 3. Resolve → download → extract: the core chain

`ResolveRegion` is `pure`, `Download` is `expensive` and idempotent, and the
extraction reads the cache. Each step references the previous one, which is what
orders them.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache
    use osm.Amenities

    /** Every restaurant in a named region. */
    workflow RestaurantsIn(region: String = "Oregon") => (path: String, count: Long) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region)

        cached = osm.cache.Download(region = resolved.region, cache_policy = "prefer_cache")

        food = osm.Amenities.Restaurants(cache = cached.cache)

        yield RestaurantsIn(path = food.result.output_path, count = food.result.feature_count)
    }
}
```

Re-running is cheap: `Download` returns the existing cache entry with
`wasInCache = true` instead of re-fetching the PBF.

## 4. Extract once, filter many times

Extraction is `expensive`; the filters are `pure` and `cheap`. Chain them instead
of re-extracting, and note that the three filters below are independent, so they
run **concurrently**.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache
    use osm.Amenities
    use osm.Filters

    /** One extraction, three cheap refinements in parallel. */
    workflow AmenitySlices(region: String = "Oregon") => (cafes: String, bars: String) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region)
        cached = osm.cache.Download(region = resolved.region)

        all = osm.Amenities.ExtractAmenities(cache = cached.cache, category = "all")

        cafes = osm.Amenities.FilterAmenitiesByCategory(
            input_path = all.result.output_path, category = "cafe")

        bars = osm.Amenities.FilterAmenitiesByCategory(
            input_path = all.result.output_path, category = "bar")

        tagged = osm.Filters.FilterGeoJSONByOSMType(
            input_path = all.result.output_path, osm_type = "node")

        yield AmenitySlices(
            cafes = cafes.result.output_path,
            bars = bars.result.output_path)
    }
}
```

## 5. Natural language → OSM tag

`osm.Vocab.ResolveTag` turns "coffee shop" into `amenity=cafe` — the lookup half
of the *lookup-then-compose* pattern that lets an LLM (or a user) name a thing
without knowing OSM's tagging scheme.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache
    use osm.Amenities
    use osm.Filters
    use osm.Vocab

    /** "coffee shop" → amenity=cafe → filtered GeoJSON. */
    workflow FindByTerm(region: String = "Oregon", term: String = "coffee shop") => (path: String, tag: String) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region)
        cached = osm.cache.Download(region = resolved.region)

        tag = osm.Vocab.ResolveTag(term = $.term)

        all = osm.Amenities.ExtractAmenities(cache = cached.cache, category = "all")

        matched = osm.Filters.FilterByOSMTag(
            input_path = all.result.output_path,
            tag_key = tag.result.osm_key,
            tag_value = tag.result.osm_value)

        yield FindByTerm(
            path = matched.result.output_path,
            tag = tag.result.osm_key ++ "=" ++ tag.result.osm_value)
    }
}
```

## 6. Fan out over regions — `foreach`

`andThen foreach v in <list>` turns one step into N runtime steps that the fleet
claims in parallel. `ResolveRegions` returns the resolved list; because the
`foreach` hangs off that **step**, `$` inside the body is the resolve step and
`$$` reaches the workflow.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache
    use osm.Amenities

    /** One download+extract per region, in parallel across the fleet. */
    workflow RestaurantsAcross(region_names: [String], prefer_continent: String = "") => (paths: [String]) andThen {

        resolved = osm.Region.ResolveRegions(
            names = $.region_names,
            prefer_continent = $.prefer_continent) andThen foreach r in $.regions {

            cached = osm.cache.Download(region = $.r, cache_policy = "prefer_cache")

            food = osm.Amenities.Restaurants(cache = cached.cache)

            yield RestaurantsAcross(paths = [food.result.output_path])
        }
    }
}
```

```bash
fw ffl run --primary my.ffl --library … --workflow my.osm.RestaurantsAcross \
  --inputs '{"region_names": ["Oregon", "Washington", "Idaho"]}' --task-list osm
```

> ⚠️ PBF work is heavy. Wide OSM fan-outs are capability-gated onto the `heavy`
> server group and have filled a Docker VM's disk before — pace them, and see
> [server groups](https://github.com/rlemke/facetwork/blob/main/docs/architecture/server-groups.md).

## 7. Guard the expensive step — `when`

A `when` block hangs off the step it inspects: inside a case `$` is that step and
`$$` reaches the workflow. Resolution is free; the download is not. Every `when`
needs a default case, last.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache

    /** Only pull a PBF for a region that actually resolved. */
    workflow GuardedDownload(region: String = "Oregon") => (status: String, path: String) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region) andThen when {
            case $.region.canonical != "" => {
                cached = osm.cache.Download(region = $.region, cache_policy = "prefer_cache")
                yield GuardedDownload(status = "cached", path = cached.cache.pbf_path)
            }
            case _ => {
                yield GuardedDownload(status = "unresolved_region", path = "")
            }
        }
    }
}
```

## 8. Call-time mixins and `catch`

Continent-sized PBFs need hours, and a mirror can be down. Both are call-site
concerns.

```ffl
namespace my.osm {

    use osm.Region
    use osm.cache

    /** A big download: long clock, retries, clean failure. */
    workflow PatientDownload(region: String = "Europe") => (status: String, path: String) andThen {

        resolved = osm.Region.ResolveRegion(name = $.region)

        cached = osm.cache.Download(
            region = resolved.region,
            cache_policy = "prefer_cache") with Timeout(minutes = 240) with Retry(maxAttempts = 3, backoffSeconds = 300) catch {
            yield PatientDownload(status = "download_failed", path = "")
        }

        yield PatientDownload(status = "cached", path = cached.cache.pbf_path)
    }
}
```

## 9. Reuse the shipped workflows

Workflows compose like facets — wrap them instead of forking them.

```ffl
namespace my.osm {

    use osm.RegionMap

    /** Wrap the shipped hiking-map workflow. */
    workflow HikingHeadline(region: String = "Oregon") => (headline: String, map_path: String) andThen {

        built = osm.RegionMap.HikingElevationMapByRegion(
            region = $.region, min_elevation_ft = 3000)

        yield HikingHeadline(
            headline = "trails mapped in " ++ built.region_name,
            map_path = built.map_path)
    }
}
```

---

## Cheat sheet

| You want to… | Write |
|---|---|
| Read a workflow/step parameter | `$.name` (`$$.name` one level out) |
| Read a previous step's result | `stepname.field` — schema results nest: `food.result.output_path` |
| Resolve a place name | `osm.Region.ResolveRegion(name = …)` → `resolved.region` |
| Pull a PBF once | `osm.cache.Download(region = resolved.region)` (idempotent) |
| Turn a phrase into a tag | `osm.Vocab.ResolveTag(term = "coffee shop")` |
| Fan out from a resolved list | `resolved = ResolveRegions(…) andThen foreach r in $.regions { … }` (`$$` = workflow) |
| Run steps in parallel | write them with no reference between them |
| More time / retries for one call | `… with Timeout(minutes = 240) with Retry(maxAttempts = 3, backoffSeconds = 300)` |
| Handle a step failure | `step = Facet(…) catch { yield … }` |
| Branch | `step = Facet(…) andThen when { case <bool> => { … } case _ => { … } }` |

**Validate before you run:** `afl my.ffl --check` or MCP `fw_validate`. Every error
carries a `rule_id` — fetch `fw://docs/rules/{rule_id}` for a wrong/right pair.

## See also

- [`docs/README.md`](README.md) — per-feature specs for this domain
- [`CLAUDE.md`](../CLAUDE.md) — the source-adapter contract (PBF / PostGIS / GeoJSON
  namespaces producing one shared schema)
- [Emergency-Access Atlas](https://github.com/rlemke/facetwork/blob/main/docs/architecture/emergency-access-atlas.md)
  — the 4-level fan-out/fan-in showcase built on these facets
- [FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md) ·
  [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical) ·
  [relative `$`-scoping](https://github.com/rlemke/facetwork/blob/main/docs/architecture/ffl-relative-scoping.md)
- The domain's FFL under `src/osm_geocoder/ffl/` and `src/osm_geocoder/handlers/*/ffl/` — the source of truth for every signature above
