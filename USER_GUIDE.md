# OSM Geocoder — User Guide

> See also: [README](README.md) | [CLAUDE.md](CLAUDE.md)

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

The `src/osm_geocoder/handlers/` package is organized into functional subdirectories. Each category contains its own handler modules, FFL source files, tests, and documentation:

```
src/osm_geocoder/handlers/
├── __init__.py              # backward-compatible facade + register_all_handlers()
├── shared/                  # _output.py, region_resolver.py, pbf_cache.py, geojson_writer.py
├── amenities/               # amenity_handlers, amenity_extractor, airquality_handlers
│   ├── ffl/                 #   osmamenities.ffl, osmairquality.ffl
│   ├── tests/               #   test_paris_amenities, test_school_airquality
│   └── README.md            #   (was AMENITIES.md)
├── boundaries/              # boundary_handlers, boundary_extractor
├── buildings/               # building_handlers, building_extractor
├── cache/                   # cache_handlers (~250 facets), region_handlers
│   └── ffl/                 #   osmcache, osmtypes, osmregion, osmops, 11 regional files
├── composed_workflows/      # workflow composition examples
├── db/                      # import_handlers, osm_store (PostGIS bulk imports)
├── sources/                 # source adapters: pbf_source, postgis_source, geojson_source
├── filters/                 # filter_handlers, radius_filter, osm_type_filter, osmose_*, validation_*
├── graphhopper/             # graphhopper_handlers (~200 facets)
├── parks/                   # park_handlers, park_extractor
├── poi/                     # poi_handlers
├── population/              # population_handlers, population_filter
├── roads/                   # road_handlers, road_extractor, zoom_* (6 modules)
├── routes/                  # route_handlers, elevation_handlers, routing_handlers, gtfs_*
├── shapefiles/              # shapefile download workflows
├── visualization/           # visualization_handlers, map_renderer
└── voting/                  # tiger_handlers, tiger_downloader
```

The core geocoding FFL file lives at `src/osm_geocoder/ffl/geocoder.ffl`.

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
pip install -e .

# No network required — uses mock handler
PYTHONPATH=src python -m pytest tests/mocked/py/test_geocoder.py
```

### 4. Running the Live Agent

```bash
# Starts polling for all OSM event facets
PYTHONPATH=src python agent.py
```

### 5. Compile Checking All FFL

```bash
# Recursively finds FFL files in handler subdirectories
find src/osm_geocoder -name '*.ffl' -not -path '*/tests/*' \
    -exec fw ffl compile {} --check \; -exec echo "OK: {}" \;
```

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

1. Create `src/osm_geocoder/handlers/newcategory/` directory with `__init__.py`
2. Add `src/osm_geocoder/handlers/newcategory/ffl/osm_newcategory.ffl` with event facets
3. Add `src/osm_geocoder/handlers/newcategory/newcategory_handlers.py` with dispatch adapter
4. Add `src/osm_geocoder/handlers/newcategory/tests/test_newcategory.py`
5. Add `src/osm_geocoder/handlers/newcategory/README.md` documenting the category
6. Wire into `src/osm_geocoder/handlers/__init__.py` (add to `_MODULE_MAP` and registration functions)

### Build a focused agent from a subset

You don't need to register all 580+ handlers. Use topic filtering:

```bash
AFL_USE_REGISTRY=1 AFL_RUNNER_TOPICS=osm.geocode,osm.cache.Europe \
    PYTHONPATH=src python agent.py
```

### Use as a base for your own geographic agent

Fork the handler structure but replace the OSM-specific logic with your own data source.

## Documentation Index

Each handler category has a README in its directory:

| Category | README | Content |
|----------|--------|---------|
| cache | [src/osm_geocoder/handlers/cache/](src/osm_geocoder/handlers/cache/README.md) | Cache system, namespaces, region registry |
| cities | [src/osm_geocoder/handlers/cities/](src/osm_geocoder/handlers/cities/README.md) | City extraction and lookup |
| poi | [src/osm_geocoder/handlers/poi/](src/osm_geocoder/handlers/poi/README.md) | Point-of-interest extraction |
| boundaries | [src/osm_geocoder/handlers/boundaries/](src/osm_geocoder/handlers/boundaries/README.md) | Administrative/natural boundaries |
| filters | [src/osm_geocoder/handlers/filters/](src/osm_geocoder/handlers/filters/README.md) | Radius, OSM type, and validation filtering |
| routes | [src/osm_geocoder/handlers/routes/](src/osm_geocoder/handlers/routes/README.md) | Bicycle, hiking, train, bus, city routing |
| population | [src/osm_geocoder/handlers/population/](src/osm_geocoder/handlers/population/README.md) | Population-based filtering |
| parks | [src/osm_geocoder/handlers/parks/](src/osm_geocoder/handlers/parks/README.md) | National parks, protected areas |
| buildings | [src/osm_geocoder/handlers/buildings/](src/osm_geocoder/handlers/buildings/README.md) | Building footprint extraction |
| amenities | [src/osm_geocoder/handlers/amenities/](src/osm_geocoder/handlers/amenities/README.md) | Amenity extraction, air quality |
| roads | [src/osm_geocoder/handlers/roads/](src/osm_geocoder/handlers/roads/README.md) | Road network extraction, zoom builder |
| visualization | [src/osm_geocoder/handlers/visualization/](src/osm_geocoder/handlers/visualization/README.md) | Map rendering with Leaflet |
| graphhopper | [src/osm_geocoder/handlers/graphhopper/](src/osm_geocoder/handlers/graphhopper/README.md) | Routing graph operations |
| voting | [src/osm_geocoder/handlers/voting/](src/osm_geocoder/handlers/voting/README.md) | US Census TIGER data |
| shapefiles | [src/osm_geocoder/handlers/shapefiles/](src/osm_geocoder/handlers/shapefiles/README.md) | Shapefile downloads |
| composed_workflows | [src/osm_geocoder/handlers/composed_workflows/](src/osm_geocoder/handlers/composed_workflows/README.md) | Workflow composition examples |

## Next Steps

- **[osm-lz](https://github.com/rlemke/fwh_osm_lz)** — run OSM pipelines at continental scale (pure-FFL catalog over this package)
- **[jenkins](https://github.com/rlemke/fwh_jenkins)** — mixin composition patterns
- **[genomics](https://github.com/rlemke/fwh_genomics)** — foreach fan-out for batch processing
