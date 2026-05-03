# Composed Workflows

This document demonstrates the power of facet-based workflow composition. By combining cache, extraction, filtering, statistics, and visualization facets, you can build sophisticated data processing pipelines.

## Composition Patterns

### Pattern 1: Cache → Extract → Visualize

The basic three-stage pipeline for quick visualization.

```afl
workflow VisualizeBicycleRoutes(region: String = "Liechtenstein")
    => (map_path: String, route_count: Long) andThen {

    // Stage 1: Get cached region data
    cache = osm.ops.CacheRegion(region = $.region)

    // Stage 2: Extract bicycle routes
    routes = osm.Routes.BicycleRoutes(
        cache = cache.cache,
        include_infrastructure = true
    )

    // Stage 3: Visualize on map
    map = osm.viz.RenderMap(
        geojson_path = routes.result.output_path,
        title = "Bicycle Routes",
        color = "#27ae60"
    )

    yield VisualizeBicycleRoutes(
        map_path = map.result.output_path,
        route_count = routes.result.feature_count
    )
}
```

### Pattern 2: Cache → Extract → Statistics

Analysis pipeline without visualization.

```afl
workflow AnalyzeParks(region: String = "Liechtenstein")
    => (total_parks: Long, total_area: Double, national: Long, state: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    parks = osm.Parks.ExtractParks(cache = cache.cache, park_type = "all")
    stats = osm.Parks.ParkStatistics(input_path = parks.result.output_path)

    yield AnalyzeParks(
        total_parks = stats.stats.total_parks,
        total_area = stats.stats.total_area_km2,
        national = stats.stats.national_parks,
        state = stats.stats.state_parks
    )
}
```

### Pattern 3: Cache → Extract → Filter → Visualize

Four-stage pipeline with filtering.

```afl
workflow LargeCitiesMap(region: String = "Liechtenstein", min_pop: Long = 10000)
    => (map_path: String, city_count: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    cities = osm.Population.Cities(cache = cache.cache, min_population = 0)

    // Filter by population
    large = osm.Population.FilterByPopulation(
        input_path = cities.result.output_path,
        min_population = $.min_pop,
        place_type = "city",
        operator = "gte"
    )

    map = osm.viz.RenderMap(
        geojson_path = large.result.output_path,
        title = "Large Cities",
        color = "#e74c3c"
    )

    yield LargeCitiesMap(
        map_path = map.result.output_path,
        city_count = large.result.feature_count
    )
}
```

### Pattern 4: Parallel Extraction → Aggregated Statistics

Multiple extractions with combined analysis.

```afl
workflow TransportOverview(region: String = "Liechtenstein")
    => (bicycle_km: Double, hiking_km: Double, train_km: Double, bus_routes: Long) andThen {

    // Shared cache
    cache = osm.ops.CacheRegion(region = $.region)

    // Parallel extraction (conceptually)
    bicycle = osm.Routes.BicycleRoutes(cache = cache.cache, include_infrastructure = false)
    hiking = osm.Routes.HikingTrails(cache = cache.cache, include_infrastructure = false)
    train = osm.Routes.TrainRoutes(cache = cache.cache, include_infrastructure = false)
    bus = osm.Routes.BusRoutes(cache = cache.cache, include_infrastructure = false)

    // Statistics for each
    bicycle_stats = osm.Routes.RouteStatistics(input_path = bicycle.result.output_path)
    hiking_stats = osm.Routes.RouteStatistics(input_path = hiking.result.output_path)
    train_stats = osm.Routes.RouteStatistics(input_path = train.result.output_path)
    bus_stats = osm.Routes.RouteStatistics(input_path = bus.result.output_path)

    yield TransportOverview(
        bicycle_km = bicycle_stats.stats.total_length_km,
        hiking_km = hiking_stats.stats.total_length_km,
        train_km = train_stats.stats.total_length_km,
        bus_routes = bus_stats.stats.route_count
    )
}
```

### Pattern 5: Full Five-Stage Pipeline

Cache → Extract → Filter → Statistics → Visualize

```afl
workflow NationalParksAnalysis(region: String = "Liechtenstein")
    => (map_path: String, park_count: Long, total_area: Double, avg_area: Double) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    all_parks = osm.Parks.ExtractParks(cache = cache.cache, park_type = "all")

    // Filter to national parks only
    national = osm.Parks.FilterParksByType(
        input_path = all_parks.result.output_path,
        park_type = "national"
    )

    stats = osm.Parks.ParkStatistics(input_path = national.result.output_path)

    map = osm.viz.RenderMap(
        geojson_path = national.result.output_path,
        title = "National Parks",
        color = "#2ecc71"
    )

    yield NationalParksAnalysis(
        map_path = map.result.output_path,
        park_count = stats.stats.total_parks,
        total_area = stats.stats.total_area_km2,
        avg_area = stats.stats.total_area_km2
    )
}
```

### Pattern 6: Parameterized City Analysis

City extraction and statistics with configurable region and population threshold.

```afl
workflow CityAnalysis(region: String = "Liechtenstein", min_population: Long = 100000)
    => (map_path: String, large_cities: Long, total_pop: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    cities = osm.Population.Cities(cache = cache.cache, min_population = $.min_population)
    stats = osm.Population.PopulationStatistics(
        input_path = cities.result.output_path,
        place_type = "city"
    )
    map = osm.viz.RenderMap(
        geojson_path = cities.result.output_path,
        title = "Cities",
        color = "#3498db"
    )

    yield CityAnalysis(
        map_path = map.result.output_path,
        large_cities = stats.stats.total_places,
        total_pop = stats.stats.total_population
    )
}
```

### Pattern 7: Multi-Layer Visualization

Combining multiple extractions into one visual output.

```afl
workflow TransportMap(region: String = "Liechtenstein")
    => (map_path: String) andThen {

    cache = osm.ops.CacheRegion(region = $.region)

    // Extract different transport types
    bicycle = osm.Routes.BicycleRoutes(cache = cache.cache, include_infrastructure = false)
    hiking = osm.Routes.HikingTrails(cache = cache.cache, include_infrastructure = false)

    // Create map from primary layer
    bicycle_map = osm.viz.RenderMap(
        geojson_path = bicycle.result.output_path,
        title = "Bicycle Routes",
        color = "#27ae60"
    )

    yield TransportMap(map_path = bicycle_map.result.output_path)
}
```

### Pattern 8: Boundary Analysis Pipeline

Administrative boundary extraction with visualization.

```afl
workflow StateBoundariesWithStats(region: String = "Liechtenstein")
    => (map_path: String, state_count: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    boundaries = osm.Boundaries.StateBoundaries(cache = cache.cache)

    map = osm.viz.RenderMap(
        geojson_path = boundaries.result.output_path,
        title = "State Boundaries",
        color = "#9b59b6"
    )

    yield StateBoundariesWithStats(
        map_path = map.result.output_path,
        state_count = boundaries.result.feature_count
    )
}
```

### Pattern 9: POI Discovery Pipeline

Find and visualize points of interest at multiple settlement levels.

```afl
workflow DiscoverCitiesAndTowns(region: String = "Liechtenstein")
    => (map_path: String, cities: Long, towns: Long, villages: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)

    // Extract settlements at different levels
    city_data = osm.POIs.Cities(cache = cache.cache)
    town_data = osm.POIs.Towns(cache = cache.cache)
    village_data = osm.POIs.Villages(cache = cache.cache)

    map = osm.viz.RenderMap(
        geojson_path = city_data.cities.path,
        title = "Cities",
        color = "#e74c3c"
    )

    yield DiscoverCitiesAndTowns(
        map_path = map.result.output_path,
        cities = city_data.cities.size,
        towns = town_data.towns.size,
        villages = village_data.villages.size
    )
}
```

### Pattern 10: Complete Regional Analysis

Comprehensive analysis combining multiple feature types with aggregated statistics.

```afl
workflow RegionalAnalysis(region: String = "Liechtenstein")
    => (parks_count: Long, parks_area: Double, routes_km: Double,
        cities_count: Long, map_path: String) andThen {

    // Shared cache across all extractions
    cache = osm.ops.CacheRegion(region = $.region)

    // Extract multiple feature types
    parks = osm.Parks.ExtractParks(cache = cache.cache, park_type = "all")
    routes = osm.Routes.BicycleRoutes(cache = cache.cache, include_infrastructure = false)
    cities = osm.Population.Cities(cache = cache.cache, min_population = 0)

    // Calculate statistics for each
    park_stats = osm.Parks.ParkStatistics(input_path = parks.result.output_path)
    route_stats = osm.Routes.RouteStatistics(input_path = routes.result.output_path)
    city_stats = osm.Population.PopulationStatistics(
        input_path = cities.result.output_path,
        place_type = "city"
    )

    map = osm.viz.RenderMap(
        geojson_path = parks.result.output_path,
        title = "Regional Overview - Parks",
        color = "#2ecc71"
    )

    yield RegionalAnalysis(
        parks_count = park_stats.stats.total_parks,
        parks_area = park_stats.stats.total_area_km2,
        routes_km = route_stats.stats.total_length_km,
        cities_count = city_stats.stats.total_places,
        map_path = map.result.output_path
    )
}
```

### Pattern 11: Cache → Validate → Summary

Data quality validation pipeline.

```afl
workflow ValidateAndSummarize(region: String = "Liechtenstein", output_dir: String = "/tmp")
    => (total: Long, valid: Long, invalid: Long, output_path: String) andThen {

    cache = osm.ops.CacheRegion(region = $.region)

    // Run full validation on the cache
    validation = osm.ops.Validation.ValidateCache(
        cache = cache.cache,
        output_dir = $.output_dir,
        use_hdfs = false
    )

    // Compute summary statistics from validation output
    summary = osm.ops.Validation.ValidationSummary(
        input_path = validation.result.output_path
    )

    yield ValidateAndSummarize(
        total = summary.stats.total_entries,
        valid = summary.stats.valid_entries,
        invalid = summary.stats.invalid_entries,
        output_path = validation.result.output_path
    )
}
```

### Pattern 12: Cache → Local Verify → Summary

Standalone local PBF/GeoJSON quality analysis via the OSMOSE verifier.

```afl
workflow OsmoseQualityCheck(
    region: String = "Liechtenstein",
    output_dir: String = "/tmp"
) => (
    total_issues: Long,
    geometry_issues: Long,
    tag_issues: Long,
    reference_issues: Long,
    tag_coverage_pct: Double,
    output_path: String
) andThen {

    cache = osm.ops.CacheRegion(region = $.region)

    // Run full local verification on the PBF
    verify = osm.ops.OSMOSE.VerifyAll(
        cache = cache.cache,
        output_dir = $.output_dir
    )

    // Compute summary statistics from verification output
    summary = osm.ops.OSMOSE.ComputeVerifySummary(
        input_path = verify.result.output_path
    )

    yield OsmoseQualityCheck(
        total_issues = summary.summary.total_issues,
        geometry_issues = summary.summary.geometry_issues,
        tag_issues = summary.summary.tag_issues,
        reference_issues = summary.summary.reference_issues,
        tag_coverage_pct = summary.summary.tag_coverage_pct,
        output_path = verify.result.output_path
    )
}
```

### Pattern 13: GTFS Transit Analysis

Download a GTFS feed, extract stops and routes in parallel, then compute statistics.

```afl
workflow TransitAnalysis(
    gtfs_url: String
) => (
    agency_name: String,
    stop_count: Long,
    route_count: Long,
    trip_count: Long,
    has_shapes: Boolean,
    stops_path: String,
    routes_path: String
) andThen {

    // Download and cache the GTFS feed
    dl = osm.Transit.GTFS.DownloadFeed(url = $.gtfs_url)

    // Extract stops and routes in parallel
    stops = osm.Transit.GTFS.ExtractStops(feed = dl.feed)
    routes = osm.Transit.GTFS.ExtractRoutes(feed = dl.feed)

    // Compute aggregate statistics
    stats = osm.Transit.GTFS.TransitStatistics(feed = dl.feed)

    yield TransitAnalysis(
        agency_name = stats.stats.agency_name,
        stop_count = stats.stats.stop_count,
        route_count = stats.stats.route_count,
        trip_count = stats.stats.trip_count,
        has_shapes = stats.stats.has_shapes,
        stops_path = stops.result.output_path,
        routes_path = routes.result.output_path
    )
}
```

### Pattern 14: GTFS Transit Accessibility

Combine OSM building data with GTFS stops to compute walk-distance accessibility bands and detect coverage gaps.

```afl
workflow TransitAccessibility(
    gtfs_url: String,
    region: String = "Liechtenstein"
) => (
    pct_within_400m: Double,
    pct_within_800m: Double,
    coverage_pct: Double,
    gap_cells: Long,
    accessibility_path: String,
    coverage_path: String
) andThen {

    // Get OSM cache and download GTFS feed in parallel
    cache = osm.ops.CacheRegion(region = $.region)
    dl = osm.Transit.GTFS.DownloadFeed(url = $.gtfs_url)

    // Extract buildings from OSM and stops from GTFS in parallel
    buildings = osm.Buildings.ExtractBuildings(cache = cache.cache)
    stops = osm.Transit.GTFS.ExtractStops(feed = dl.feed)

    // Compute walk-distance accessibility bands
    access = osm.Transit.GTFS.StopAccessibility(
        osm_geojson_path = buildings.result.output_path,
        stops_geojson_path = stops.result.output_path,
        walk_threshold_m = 400
    )

    // Detect coverage gaps
    gaps = osm.Transit.GTFS.CoverageGaps(
        stops_geojson_path = stops.result.output_path,
        osm_geojson_path = buildings.result.output_path,
        cell_size_m = 500
    )

    yield TransitAccessibility(
        pct_within_400m = access.result.pct_within_400m,
        pct_within_800m = access.result.pct_within_800m,
        coverage_pct = gaps.result.coverage_pct,
        gap_cells = gaps.result.gap_cells,
        accessibility_path = access.result.output_path,
        coverage_path = gaps.result.output_path
    )
}
```

### Pattern 15: Low-Zoom Road Infrastructure Builder

Build a routing graph then compute per-edge minimum zoom levels (z2-z7) for cartographic generalization.

```afl
workflow RoadZoomBuilder(
    region: String = "Liechtenstein",
    output_dir: String = "/tmp/zoom-builder",
    max_concurrent: Long = 16
) => (
    total_edges: Long,
    selected_edges: Long,
    zoom_distribution: String,
    csv_path: String,
    metrics_path: String
) andThen {

    cache = osm.ops.CacheRegion(region = $.region)

    // Build GraphHopper graph for routing
    graph = osm.ops.GraphHopper.BuildGraph(cache = cache.cache)

    // Run full zoom builder pipeline
    zoom = osm.Roads.ZoomBuilder.BuildZoomLayers(
        cache = cache.cache,
        graph = graph.graph,
        output_dir = $.output_dir,
        max_concurrent = $.max_concurrent
    )

    yield RoadZoomBuilder(
        total_edges = zoom.result.total_logical_edges,
        selected_edges = zoom.result.selected_edges,
        zoom_distribution = zoom.result.zoom_distribution,
        csv_path = zoom.result.csv_path,
        metrics_path = zoom.result.metrics_path
    )
}
```

## Benefits of Facet Composition

1. **Reusability**: Cache facets are shared across workflows
2. **Modularity**: Each facet does one thing well
3. **Testability**: Facets can be tested in isolation
4. **Flexibility**: Combine facets in different ways for different use cases
5. **Parallelism**: Independent facets can execute concurrently
6. **Maintainability**: Changes to one facet don't affect others

## Available Facet Categories

| Category | Namespace | Description |
|----------|-----------|-------------|
| Cache | `osm.cache.*` | ~250 geographic region caches |
| Operations | `osm.ops` | Download, tile, validation, routing graph |
| OSMOSE | `osm.ops.OSMOSE` | Local PBF/GeoJSON quality verification |
| POI | `osm.POIs` | Points of interest |
| Boundaries | `osm.Boundaries` | Administrative/natural boundaries |
| Routes | `osm.Routes` | Transportation routes |
| Parks | `osm.Parks` | Parks and protected areas |
| Population | `osm.Population` | Population-based filtering |
| Buildings | `osm.Buildings` | Building footprints |
| Amenities | `osm.Amenities` | Services and facilities |
| Roads | `osm.Roads` | Road network |
| Zoom Builder | `osm.Roads.ZoomBuilder` | Low-zoom road layer generation (z2-z7) |
| Transit | `osm.Transit.GTFS` | GTFS transit feed analysis |
| Filters | `osm.Filters` | Radius and type filtering |
| Visualization | `osm.viz` | Map rendering |
| TIGER | `osm.TIGER` | US Census voting districts |

## See Also

- [Cache README](../cache/README.md) - Cache system documentation
- [Routes README](../routes/README.md) - Route extraction
- [Parks README](../parks/README.md) - Park extraction
- [Population README](../population/README.md) - Population filtering
- [Buildings README](../buildings/README.md) - Building extraction
- [Amenities README](../amenities/README.md) - Amenity extraction
- [Roads README](../roads/README.md) - Road extraction
- [Visualization README](../visualization/README.md) - Map rendering
