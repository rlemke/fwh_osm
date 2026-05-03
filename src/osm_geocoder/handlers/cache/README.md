# Cache System

The cache layer resolves geographic region names to Geofabrik download URLs and local file paths. It is the first step in every OSM data workflow — downstream operations like `Download`, `DownloadShapefile`, `Tile`, and `RoutingGraph` all receive their input from a cache facet.

## OSMCache schema

All cache facets return an `OSMCache` struct, defined in `afl/osmtypes.ffl`:

```afl
schema OSMCache {
    url: String
    path: String
    date: String
    size: Long
    wasInCache: Boolean
}
```

| Field | Description |
|-------|-------------|
| `url` | Geofabrik download URL for the region |
| `path` | Local filesystem path where the file is (or will be) cached |
| `date` | ISO 8601 timestamp of when the entry was created |
| `size` | File size in bytes (0 if not yet downloaded) |
| `wasInCache` | Whether the file was already present locally |

## FFL facets

Cache event facets are defined in `afl/osmcache.ffl` across 11 geographic namespaces. Each facet takes no parameters and returns an `OSMCache`:

```afl
namespace osm.cache.Africa {
    event facet AllAfrica() => (cache: OSMCache)
    event facet Algeria() => (cache: OSMCache)
    event facet Angola() => (cache: OSMCache)
    // ... ~55 African countries
}
```

### Namespaces

| Namespace | Regions | Description |
|-----------|---------|-------------|
| `osm.cache.Africa` | ~57 | African countries + continent aggregate |
| `osm.cache.Asia` | ~44 | Asian countries + continent aggregate |
| `osm.cache.Australia` | ~21 | Australia, New Zealand, Pacific islands |
| `osm.cache.Europe` | ~45 | European countries + continent aggregate |
| `osm.cache.NorthAmerica` | 4 | Canada, Mexico, United States, Greenland |
| `osm.cache.Canada` | ~12 | Canadian provinces and territories |
| `osm.cache.CentralAmerica` | ~13 | Central American and Caribbean countries |
| `osm.cache.SouthAmerica` | ~13 | South American countries |
| `osm.cache.UnitedStates` | ~52 | US states + District of Columbia |
| `osm.cache.Antarctica` | 1 | Antarctica |
| `osm.cache.Continents` | ~10 | Continent-level aggregates + planet |

Each namespace also includes an `All*` facet (e.g. `AllAfrica`, `AllEurope`) that resolves to the continent-level aggregate download.

## Python handler

Cache handlers are registered in `handlers/cache_handlers.py`. The module defines a `REGION_REGISTRY` dict that maps each namespace to its facets and their Geofabrik region paths:

```python
REGION_REGISTRY = {
    "osm.cache.Africa": {
        "AllAfrica": "africa",
        "Algeria": "africa/algeria",
        "Angola": "africa/angola",
        # ...
    },
    "osm.cache.Europe": {
        "AllEurope": "europe",
        "Albania": "europe/albania",
        # ...
    },
    # ... 11 namespaces total
}
```

Each handler calls the `download()` function from `handlers/downloader.py` with the region's Geofabrik path:

```python
def _make_handler(region_path: str):
    def handler(payload: dict) -> dict:
        return {"cache": download(region_path)}
    return handler
```

The handler downloads the `.osm.pbf` file (or returns it from the local cache) and wraps the result in a `{"cache": ...}` dict matching the facet's return signature.

## How workflows use cache facets

Regional workflows (e.g. `osmafrica.ffl`, `osmeurope.ffl`) follow a two-phase pattern:

1. **Cache lookup** — call each country's cache facet to get the `OSMCache` with the Geofabrik URL
2. **Operation** — pass the cache result to a processing facet like `Download` or `DownloadShapefile`

```afl
namespace osm.Africa.cache {
  use osm.ops
  use osm.cache.Africa

  facet AfricaIndividually() => (cache: [OSMCache]) andThen {
    algeria = Algeria()                              // phase 1: cache lookup
    dl_algeria = Download(cache = algeria.cache)     // phase 2: download

    // ... all countries

    yield AfricaIndividually(cache = dl_algeria.cache ++ ...)
  }
}
```

All cache lookups within a workflow execute in parallel (they are independent steps). Downloads also execute in parallel since each depends only on its own cache step.

## Local filesystem cache

Downloaded files are stored under the system temp directory:

```
/tmp/osm-cache/
├── africa/
│   ├── algeria-latest.osm.pbf
│   └── angola-latest.osm.pbf
├── europe/
│   ├── albania-latest.osm.pbf
│   └── austria-latest.osm.pbf
└── ...
```

The cache is keyed by region path and format. A file is considered cached if it exists on disk — there is no TTL or expiration. To force a re-download, delete the local file.

Concurrent downloads of the same file are safe — the downloader uses per-path locks and atomic temp-file writes so the cache file is always either absent or complete. See [downloads README](../downloads/README.md) for full details on the mirror and concurrency mechanisms.

## Geofabrik URL mapping

Region paths map directly to Geofabrik's directory structure:

| Facet | Region path | Download URL |
|-------|-------------|-------------|
| `Algeria` | `africa/algeria` | `https://download.geofabrik.de/africa/algeria-latest.osm.pbf` |
| `California` | `north-america/us/california` | `https://download.geofabrik.de/north-america/us/california-latest.osm.pbf` |
| `AllEurope` | `europe` | `https://download.geofabrik.de/europe-latest.osm.pbf` |
| `Planet` | `planet` | `https://download.geofabrik.de/planet-latest.osm.pbf` |

Some facets share the same underlying Geofabrik path. For example, `Malaysia` and `Singapore` both resolve to `asia/malaysia-singapore-brunei` because Geofabrik provides them as a combined extract.

## Adding a new region

To add a new region to the cache system:

1. Add the event facet to the appropriate namespace in `afl/osmcache.ffl`:
   ```afl
   event facet NewRegion() => (cache: OSMCache)
   ```

2. Add the region path to `REGION_REGISTRY` in `handlers/cache_handlers.py`:
   ```python
   "osm.cache.Europe": {
       # ...
       "NewRegion": "europe/new-region",
   }
   ```

No other changes are needed — the handler is generated automatically from the registry entry.
