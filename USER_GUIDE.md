# OSM Geocoder — User Guide

> See also: [Examples Guide](../doc/GUIDE.md) | [README](README.md)

## When to Use This Example

Use this as your starting point if you are:
- Building a **production-scale agent** with hundreds of event facets
- Organizing handlers across **many modules and namespaces**
- Working with **geographic data**, OSM APIs, or spatial operations
- Understanding how to structure a **large FFL project** with 40+ source files

## What You'll Learn

1. How to organize a large FFL project with namespace-per-domain architecture
2. How to build handler modules for different categories of operations
3. How factory-built handlers work with geographic registries
4. How to use the `AgentPoller` for a standalone agent service
5. How to write integration tests for composed workflows
6. How to **encapsulate low-level operations** behind simple composed facets for library reuse
7. How to use the **Geofabrik mirror** for offline and CI workflows

## Overview

This is the largest example in the repository:
- **42 FFL files** organized into 16 functional categories
- **580+ handler dispatch keys** across ~80 handler modules
- **36 category test files** plus integration tests
- **16 README files** — one per handler category (moved from root-level `.md` docs)

## Project Structure

The `handlers/` package is organized into functional subdirectories. Each category contains its own handler modules, FFL source files, tests, and documentation:

```
handlers/
├── __init__.py              # backward-compatible facade + register_all_handlers()
├── shared/                  # _output.py, downloader.py, region_resolver.py
├── amenities/               # amenity_handlers, amenity_extractor, airquality_handlers
│   ├── afl/                 #   osmamenities.ffl, osmairquality.ffl
│   ├── tests/               #   test_paris_amenities, test_school_airquality
│   └── README.md            #   (was AMENITIES.md)
├── boundaries/              # boundary_handlers, boundary_extractor
├── buildings/               # building_handlers, building_extractor
├── cache/                   # cache_handlers (~250 facets), region_handlers
│   └── afl/                 #   osmcache, osmtypes, osmregion, 11 regional files
├── composed_workflows/      # workflow composition examples
├── downloads/               # operations_handlers, postgis_handlers, postgis_importer
├── filters/                 # filter_handlers, radius_filter, osm_type_filter, osmose_*, validation_*
├── graphhopper/             # graphhopper_handlers (~200 facets)
├── parks/                   # park_handlers, park_extractor
├── poi/                     # poi_handlers
├── population/              # population_handlers, population_filter
├── roads/                   # road_handlers, road_extractor, zoom_* (6 modules)
├── routes/                  # route_handlers, elevation_handlers, routing_handlers, gtfs_*
├── shapefiles/              # shapefile download workflows (reuses downloads)
├── visualization/           # visualization_handlers, map_renderer
└── voting/                  # tiger_handlers, tiger_downloader
```

The core geocoding FFL file remains at `afl/geocoder.ffl`.

### Backward Compatibility

All old flat imports (e.g. `from handlers.cache_handlers import REGION_REGISTRY`) continue to work. The `handlers/__init__.py` facade installs a custom `_AliasImporter` that redirects old module paths to new subpackage locations.

## Step-by-Step Walkthrough

### 1. The Core Pattern — Geocoding

The simplest operation in this example:

```afl
namespace osm.geocode {
    event facet Geocode(address: String) => (result: GeoCoordinate)

    workflow GeocodeAddress(address: String) => (location: GeoCoordinate) andThen {
        geo = Geocode(address = $.address)
        yield GeocodeAddress(location = geo.result)
    }
}
```

The handler calls the Nominatim API:

```python
def _geocode_handler(payload):
    address = payload["address"]
    response = requests.get("https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1})
    data = response.json()[0]
    return {"result": {"lat": data["lat"], "lon": data["lon"], "display_name": data["display_name"]}}
```

### 2. Factory-Built Cache Handlers

The cache system uses a geographic registry:

```python
REGION_REGISTRY = {
    "osm.cache.Africa": {
        "Algeria": "https://download.geofabrik.de/africa/algeria-latest.osm.pbf",
        "Angola": "https://download.geofabrik.de/africa/angola-latest.osm.pbf",
        # ... 50+ countries
    },
    "osm.cache.Europe": { ... },
    # ... 11 namespaces
}

# Factory generates one handler per region per namespace
for namespace, regions in REGION_REGISTRY.items():
    for region_name, url in regions.items():
        _DISPATCH[f"{namespace}.{region_name}"] = _make_cache_handler(region_name, url)
```

### 3. Running the Offline Test

```bash
source .venv/bin/activate
pip install -r examples/osm-geocoder/requirements.txt

# No network required — uses mock handler
PYTHONPATH=. python examples/osm-geocoder/test_geocoder.py
```

### 4. Running the Live Agent

```bash
# Starts polling for all OSM event facets
PYTHONPATH=. python examples/osm-geocoder/agent.py
```

### 5. Compile Checking All FFL

```bash
# Recursively finds FFL files in handler subdirectories
find examples/osm-geocoder -name '*.ffl' -not -path '*/tests/*' \
    -exec python -m afl.cli {} --check \; -exec echo "OK: {}" \;
```

### 6. Geofabrik Mirror for Offline Workflows

In CI or air-gapped environments, use the local mirror to avoid network requests:

```bash
# Prefetch all regions (or a subset) into a mirror directory
scripts/osm-prefetch --mirror-dir /data/osm-mirror --include "europe/"

# Activate the mirror — the downloader reads from it before hitting the network
export AFL_GEOFABRIK_MIRROR=/data/osm-mirror

# Run the agent as usual — downloads are served from the mirror
PYTHONPATH=. python examples/osm-geocoder/agent.py
```

See [downloads README](handlers/downloads/README.md) for all prefetch options and the full check order (cache → mirror → download).

## Key Concepts

### Namespace-Per-Domain Architecture

Each category of operations gets its own namespace:

```
osm.geocode          — core geocoding
osm.cache.*       — per-region caching (11 geographic namespaces)
osm.ops — data processing
osm.Boundaries — boundary extraction
osm.Routes     — route extraction
osm.Filters    — spatial filtering
```

This keeps namespaces focused and allows selective imports.

### Geographic Registry Pattern

Instead of hand-coding 250+ handler functions, use a registry:

```python
# Registry: namespace -> {region -> URL}
REGISTRY = {"osm.cache.Africa": {"Algeria": "...", "Angola": "...", ...}, ...}

# Factory: one handler per entry
def _make_handler(name, url):
    def handler(payload):
        return {"cache": {"url": url, "path": f"/cache/{name}.osm.pbf", ...}}
    return handler

# Build dispatch table from registry
_DISPATCH = {}
for ns, regions in REGISTRY.items():
    for name, url in regions.items():
        _DISPATCH[f"{ns}.{name}"] = _make_handler(name, url)
```

### Facet Encapsulation — Hiding Data Pipeline Complexity

The raw event facets (`Cache`, `Download`, `Tile`, `RoutingGraph`, `PostGisImport`, etc.) are low-level operations that require OSM domain expertise to chain correctly. Instead, **wrap multi-step data pipelines in composed facets** that expose a simple, domain-focused interface:

```afl
namespace osm.library {
    use osm.types

    // Composed facet: encapsulates cache + download + tile generation.
    // Users never see the three-step chain — they just call PrepareRegion.
    facet PrepareRegion(region: String) => (cache: OSMCache,
            tile_path: String) andThen {

        cached = osm.ops.CacheRegion(region = $.region)

        downloaded = osm.ops.DownloadPBF(cache = cached.cache)

        tiled = osm.ops.Tile(cache = downloaded.downloadCache)

        yield PrepareRegion(
            cache = downloaded.downloadCache,
            tile_path = tiled.tiles.path)
    }

    // Composed facet: encapsulates cache + download + routing graph build.
    // Hides PBF downloads and GraphHopper configuration behind one call.
    facet BuildRoutingData(region: String) => (cache: OSMCache,
            graph_path: String) andThen {

        cached = osm.ops.CacheRegion(region = $.region)

        downloaded = osm.ops.DownloadPBF(cache = cached.cache)

        graph = osm.ops.RoutingGraph(cache = downloaded.downloadCache)

        yield BuildRoutingData(
            cache = downloaded.downloadCache,
            graph_path = graph.graph.path)
    }

    // Composed facet: encapsulates full GIS import pipeline.
    // Cache → download → PostGIS import in one call.
    facet ImportToPostGIS(region: String) => (cache: OSMCache,
            import_status: String) andThen {

        cached = osm.ops.CacheRegion(region = $.region)

        downloaded = osm.ops.DownloadPBF(cache = cached.cache)

        imported = osm.ops.PostGisImport(cache = downloaded.downloadCache)

        yield ImportToPostGIS(
            cache = downloaded.downloadCache,
            import_status = "complete")
    }

    // Workflow: clean and simple — users call composed facets, not raw operations
    workflow PrepareEuropeRouting(countries: Json) => (graph_path: String,
            region: String) andThen foreach country in $.countries {

        routable = BuildRoutingData(region = $.country.name)

        yield PrepareEuropeRouting(
            graph_path = routable.graph_path,
            region = $.country.name)
    }
}
```

**Why this matters:**

| Layer | What the User Sees | What's Hidden |
|-------|-------------------|---------------|
| Event facets | `Cache`, `Download`, `Tile`, `RoutingGraph`, `PostGisImport` | Handler implementations, PBF/tile formats |
| Composed facets | `PrepareRegion(region)`, `BuildRoutingData(region)`, `ImportToPostGIS(region)` | Cache configuration, download URLs, tool-specific parameters |
| Workflows | `PrepareEuropeRouting(countries)` | The entire data pipeline structure |

This is the **library facet** pattern — the GIS team defines `PrepareRegion` and `BuildRoutingData` with correct operation ordering; application teams call them without needing to understand cache semantics, PBF file formats, or GraphHopper configuration.

### Composed Workflows

Regional workflows compose cache + download steps:

```afl
// Africa workflow composes cache lookups with download operations
namespace osm.africa {
    use osm.types
    workflow DownloadAfrica() => (...) andThen {
        algeria = osm.cache.Africa.Algeria()
        angola = osm.cache.Africa.Angola()
        // ... download each country
    }
}
```

## Adapting for Your Use Case

### Add a new handler category

1. Create `handlers/newcategory/` directory with `__init__.py`
2. Add `handlers/newcategory/ffl/osm_newcategory.ffl` with event facets
3. Add `handlers/newcategory/newcategory_handlers.py` with dispatch adapter
4. Add `handlers/newcategory/tests/test_newcategory.py`
5. Add `handlers/newcategory/README.md` documenting the category
6. Wire into `handlers/__init__.py` (add to `_MODULE_MAP` and registration functions)

### Build a focused agent from a subset

You don't need to register all 580+ handlers. Use topic filtering:

```bash
AFL_USE_REGISTRY=1 AFL_RUNNER_TOPICS=osm.geocode,osm.cache.Europe \
    PYTHONPATH=. python examples/osm-geocoder/agent.py
```

### Use as a base for your own geographic agent

Fork the handler structure but replace the OSM-specific logic with your own data source.

## Documentation Index

Each handler category has a README in its directory:

| Category | README | Content |
|----------|--------|---------|
| cache | [handlers/cache/](handlers/cache/README.md) | Cache system, namespaces, region registry |
| downloads | [handlers/downloads/](handlers/downloads/README.md) | Download operations, PBF/shapefile formats |
| poi | [handlers/poi/](handlers/poi/README.md) | Point-of-interest extraction |
| boundaries | [handlers/boundaries/](handlers/boundaries/README.md) | Administrative/natural boundaries |
| filters | [handlers/filters/](handlers/filters/README.md) | Radius, OSM type, and validation filtering |
| routes | [handlers/routes/](handlers/routes/README.md) | Bicycle, hiking, train, bus, city routing |
| population | [handlers/population/](handlers/population/README.md) | Population-based filtering |
| parks | [handlers/parks/](handlers/parks/README.md) | National parks, protected areas |
| buildings | [handlers/buildings/](handlers/buildings/README.md) | Building footprint extraction |
| amenities | [handlers/amenities/](handlers/amenities/README.md) | Amenity extraction, air quality |
| roads | [handlers/roads/](handlers/roads/README.md) | Road network extraction, zoom builder |
| visualization | [handlers/visualization/](handlers/visualization/README.md) | Map rendering with Leaflet |
| graphhopper | [handlers/graphhopper/](handlers/graphhopper/README.md) | Routing graph operations |
| voting | [handlers/voting/](handlers/voting/README.md) | US Census TIGER data |
| shapefiles | [handlers/shapefiles/](handlers/shapefiles/README.md) | Shapefile downloads |
| composed_workflows | [handlers/composed_workflows/](handlers/composed_workflows/README.md) | Workflow composition examples |

## Next Steps

- **[continental-lz](../continental-lz/USER_GUIDE.md)** — run OSM pipelines at continental scale with Docker
- **[jenkins](../jenkins/USER_GUIDE.md)** — mixin composition patterns
- **[genomics](../genomics/USER_GUIDE.md)** — foreach fan-out for batch processing
