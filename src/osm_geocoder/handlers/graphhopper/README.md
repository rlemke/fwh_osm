# GraphHopper Routing Graphs

This module provides event facets for building and managing [GraphHopper](https://www.graphhopper.com/) routing graphs from OSM cache data.

## Overview

GraphHopper is an open-source routing engine that builds optimized routing graphs from OpenStreetMap data. This integration:

- Takes downloaded OSM cache data as input
- Builds routing graphs for various profiles (car, bike, foot, etc.)
- Caches built graphs to avoid unnecessary rebuilds
- Supports a `recreate` flag to force rebuilding

## Schema

```afl
schema GraphHopperCache {
    osmSource: String      // Path to source OSM file
    graphDir: String       // Directory containing routing graph
    profile: String        // Routing profile (car, bike, foot, etc.)
    date: String           // Build/modification date (ISO format)
    size: Long             // Graph directory size in bytes
    wasInCache: Boolean    // True if returned from cache
    version: String        // GraphHopper version used
    nodeCount: Long        // Number of nodes in graph
    edgeCount: Long        // Number of edges in graph
}
```

## Operation Facets

The `osm.ops.GraphHopper` namespace provides core operations:

| Facet | Parameters | Returns | Description |
|-------|-----------|---------|-------------|
| `BuildGraph` | `cache: OSMCache, profile: String = "car", recreate: Boolean = false` | `graph: GraphHopperCache` | Build routing graph from OSM cache |
| `BuildMultiProfile` | `cache: OSMCache, profiles: [String], recreate: Boolean = false` | `graphs: [GraphHopperCache]` | Build graphs for multiple profiles |
| `BuildGraphBatch` | `cache: OSMCache, profile: String = "car", recreate: Boolean = false` | `graph: GraphHopperCache` | Bulk variant for batch processing |
| `ImportGraph` | `cache: OSMCache, profile: String = "car", recreate: Boolean = false` | `graph: GraphHopperCache` | Import/load existing graph (builds if not found) |
| `ValidateGraph` | `graph: GraphHopperCache` | `valid: Boolean, nodeCount: Long, edgeCount: Long` | Validate graph and return statistics |
| `CleanGraph` | `graph: GraphHopperCache` | `deleted: Boolean` | Delete routing graph directory |

### Recreate Flag

The `recreate` parameter controls caching behavior:

- `recreate = false` (default): Returns cached graph if it exists
- `recreate = true`: Deletes existing graph and rebuilds from scratch

## Cache Facets

Per-region cache facets are organized by geographic namespace. Each facet takes an OSM cache as input and returns a GraphHopper routing graph.

### Namespaces

| Namespace | Regions |
|-----------|---------|
| `osm.cache.GraphHopper.Africa` | 54 countries |
| `osm.cache.GraphHopper.Asia` | 43 countries |
| `osm.cache.GraphHopper.Australia` | 16 countries/territories |
| `osm.cache.GraphHopper.Europe` | 48 countries |
| `osm.cache.GraphHopper.NorthAmerica` | 4 countries |
| `osm.cache.GraphHopper.SouthAmerica` | 12 countries |
| `osm.cache.GraphHopper.CentralAmerica` | 10 countries |
| `osm.cache.GraphHopper.UnitedStates` | 51 states/territories |
| `osm.cache.GraphHopper.Canada` | 13 provinces/territories |

### Example: Europe

```afl
namespace osm.cache.GraphHopper.Europe {
    event facet Germany(cache: OSMCache, profile: String = "car", recreate: Boolean = false) => (graph: GraphHopperCache)
    event facet France(cache: OSMCache, profile: String = "car", recreate: Boolean = false) => (graph: GraphHopperCache)
    event facet Spain(cache: OSMCache, profile: String = "car", recreate: Boolean = false) => (graph: GraphHopperCache)
    // ... 45 more countries
}
```

## Routing Profiles

Supported routing profiles:

| Profile | Description |
|---------|-------------|
| `car` | Standard car routing (default) |
| `bike` | Bicycle routing |
| `foot` | Pedestrian routing |
| `motorcycle` | Motorcycle routing |
| `truck` | Truck/HGV routing |
| `hike` | Hiking trails |
| `mtb` | Mountain bike trails |
| `racingbike` | Road cycling |

## Workflow Compositions

Pre-built workflows for common regional graph building:

### Europe

```afl
namespace osm.GraphHopper.Europe {
    // Build graphs for 10 major European countries
    workflow BuildMajorEuropeGraphs(profile: String = "car", recreate: Boolean = false)
        => (graphs: [GraphHopperCache])

    // Build graph for a single country
    workflow BuildGermanyGraph(profile: String = "car", recreate: Boolean = false)
        => (graph: GraphHopperCache)
}
```

### North America

```afl
namespace osm.GraphHopper.NorthAmerica {
    // Build graphs for USA, Canada, Mexico
    workflow BuildNorthAmericaGraphs(profile: String = "car", recreate: Boolean = false)
        => (graphs: [GraphHopperCache])
}
```

### United States

```afl
namespace osm.GraphHopper.UnitedStates {
    // West Coast: California, Oregon, Washington
    workflow BuildWestCoastGraphs(profile: String = "car", recreate: Boolean = false)
        => (graphs: [GraphHopperCache])

    // East Coast: New York, New Jersey, Massachusetts, Pennsylvania, Florida
    workflow BuildEastCoastGraphs(profile: String = "car", recreate: Boolean = false)
        => (graphs: [GraphHopperCache])
}
```

## Usage Example

### Build a Single Country Graph

```afl
namespace myapp {
    uses osm.cache.Europe
    uses osm.cache.GraphHopper.Europe

    workflow BuildGermanyRouting() => (graph: GraphHopperCache) andThen {
        // Get the OSM cache for Germany
        osm = osm.cache.Europe.Germany()

        // Build the routing graph (uses cache if available)
        gh = osm.cache.GraphHopper.Europe.Germany(
            cache = osm.cache,
            profile = "car",
            recreate = false
        )

        yield BuildGermanyRouting(graph = gh.graph)
    }
}
```

### Build Multiple Profile Graphs

```afl
namespace myapp {
    uses osm.cache.Europe
    uses osm.ops.GraphHopper

    workflow BuildMultiModalRouting() => (graphs: [GraphHopperCache]) andThen {
        osm = osm.cache.Europe.Germany()

        // Build car, bike, and foot routing graphs
        gh = BuildMultiProfile(
            cache = osm.cache,
            profiles = ["car", "bike", "foot"],
            recreate = false
        )

        yield BuildMultiModalRouting(graphs = gh.graphs)
    }
}
```

### Force Rebuild

```afl
// Force rebuild even if cached graph exists
gh = osm.cache.GraphHopper.Europe.Germany(
    cache = osm.cache,
    profile = "car",
    recreate = true  // Always rebuild
)
```

## Handler Implementation

The Python handler checks for existing graphs before building:

```python
def build_graph_handler(payload: dict) -> dict:
    cache = payload.get("cache", {})
    profile = payload.get("profile", "car")
    recreate = payload.get("recreate", False)

    osm_path = cache.get("path", "")
    graph_dir = _get_graph_dir(osm_path, profile)

    # Return cached graph if exists and not recreating
    if _graph_exists(graph_dir) and not recreate:
        return {"graph": _make_graph_result(osm_path, graph_dir, profile, True)}

    # Remove existing graph if recreating
    if recreate and os.path.exists(graph_dir):
        shutil.rmtree(graph_dir)

    # Build the graph
    success = _run_graphhopper_import(osm_path, graph_dir, profile)
    return {"graph": _make_graph_result(osm_path, graph_dir, profile, False)}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRAPHHOPPER_JAR` | `~/.graphhopper/graphhopper-web.jar` | Path to GraphHopper JAR |
| `GRAPHHOPPER_GRAPH_DIR` | `~/.graphhopper/graphs` | Base directory for routing graphs |

## Graph Storage

Routing graphs are stored in:
```
~/.graphhopper/graphs/{osm-basename}-{profile}/
```

For example, Germany with car profile:
```
~/.graphhopper/graphs/germany-latest-car/
```

Each graph directory contains:
- `nodes` - Node data
- `edges` - Edge data
- `geometry` - Way geometries
- `properties` - Graph metadata and statistics
