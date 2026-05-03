# Shapefile Downloads

Geofabrik provides free shapefile (`.shp.zip`) extracts alongside the standard PBF format. This agent supports downloading both formats through the same cache-lookup pipeline — the format choice happens at the download step, not at the cache step.

## How it works

The shapefile download flow reuses the existing ~250 cache event facets. Each cache facet resolves a region's Geofabrik URL (e.g. `https://download.geofabrik.de/europe/albania-latest.osm.pbf`). The `DownloadShapefile` handler then extracts the region path from that URL and re-requests it in shapefile format.

```
Cache facet (Albania)          DownloadShapefile handler
────────────────────           ──────────────────────────
Resolves region URL    ──►     Extracts region path ("europe/albania")
(same as PBF flow)             Requests: europe/albania-latest.free.shp.zip
                               Caches locally under /tmp/osm-cache/
```

### URL patterns

| Format | URL pattern | Example |
|--------|------------|---------|
| PBF (default) | `{region}-latest.osm.pbf` | `europe/albania-latest.osm.pbf` |
| Shapefile | `{region}-latest.free.shp.zip` | `europe/albania-latest.free.shp.zip` |

Both formats are cached independently in the local filesystem cache (`/tmp/osm-cache/`).

## FFL facets

Two event facets are defined in `osmoperations.ffl` under the `osm.ops` namespace:

```afl
event facet DownloadShapefile(cache:OSMCache) => ()
event facet DownloadShapefileBatch(cache:OSMCache) => ()
```

These mirror the existing `Download` and `DownloadBatch` facets. The `cache` parameter receives an `OSMCache` struct from a preceding cache-lookup step. The return type is `()` (no return value) — the handler performs the download as a side effect.

## FFL workflow example

`osmshapefiles.ffl` defines a `EuropeShapefiles` workflow that downloads shapefiles for all European countries. It follows the same two-phase pattern as the PBF workflows:

1. **Cache lookup** — call the region's cache facet to resolve the Geofabrik URL
2. **Shapefile download** — pass the cache result to `DownloadShapefile`

```afl
namespace osm.Europe.shapefiles {
  use osm.ops
  use osm.cache.Europe

  facet EuropeShapefiles () => (cache: [OSMCache]) andThen {
    albania = Albania()
    austria = Austria()
    // ... all European countries

    shp_albania = DownloadShapefile(cache = albania.cache)
    shp_austria = DownloadShapefile(cache = austria.cache)
    // ... all European countries

    yield EuropeShapefiles(cache = shp_albania.cache ++ shp_austria.cache ++ ...)
  }
}
```

The workflow uses the same `osm.cache.Europe` cache facets as `osmeurope.ffl`. Only the download step differs — `DownloadShapefile` instead of `Download`.

## Python handler

The shapefile handler in `operations_handlers.py` works by:

1. Receiving the `cache` payload from the preceding cache-lookup step
2. Extracting the region path from the Geofabrik URL using a regex (`_extract_region_path`)
3. Calling `download(region_path, fmt="shp")` to fetch the `.free.shp.zip` file

```
Cache payload URL:  https://download.geofabrik.de/europe/albania-latest.osm.pbf
Extracted path:     europe/albania
Shapefile URL:      https://download.geofabrik.de/europe/albania-latest.free.shp.zip
```

## Downloader API

The `handlers/downloader.py` module supports both formats through a `fmt` parameter:

```python
from handlers.downloader import download, geofabrik_url, cache_path

# PBF (default)
download("europe/albania")
geofabrik_url("europe/albania")          # .../albania-latest.osm.pbf
cache_path("europe/albania")             # /tmp/osm-cache/europe/albania-latest.osm.pbf

# Shapefile
download("europe/albania", fmt="shp")
geofabrik_url("europe/albania", fmt="shp")  # .../albania-latest.free.shp.zip
cache_path("europe/albania", fmt="shp")     # /tmp/osm-cache/europe/albania-latest.free.shp.zip
```

The `FORMAT_EXTENSIONS` dict maps format keys to file extensions:

| Key | Extension |
|-----|-----------|
| `"pbf"` | `osm.pbf` |
| `"shp"` | `free.shp.zip` |

## Geofabrik availability

Geofabrik does not provide free shapefiles for all regions. Shapefiles are typically available for country-level and smaller extracts but not for continent-level aggregates. The handler will receive an HTTP 404 error for unavailable regions, which propagates as a `requests.HTTPError` and marks the step as failed.

## Adding shapefile workflows for other regions

To add shapefile downloads for another region (e.g. Africa):

1. Create a new namespace in `osmshapefiles.ffl` (or a new file):
   ```afl
   namespace osm.Africa.shapefiles {
     use osm.ops
     use osm.cache.Africa

     facet AfricaShapefiles () => (cache: [OSMCache]) andThen {
       algeria = Algeria()
       shp_algeria = DownloadShapefile(cache = algeria.cache)
       // ... remaining countries
       yield AfricaShapefiles(cache = shp_algeria.cache ++ ...)
     }
   }
   ```

2. No handler changes are needed — `DownloadShapefile` is already registered and works for any region.

## Tests

Shapefile-specific tests are in `test_downloader.py`:

```bash
PYTHONPATH=. python -m pytest examples/osm-geocoder/test_downloader.py -v -k shapefile
```

Tests cover URL generation, cache path construction, cache hits, cache misses, and correct URL requests for the shapefile format.
