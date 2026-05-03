# Road Network Extraction

Extract road networks from OSM data with classification and attributes.

## Features

- **Road classification**: Motorway, trunk, primary, secondary, tertiary, residential, service
- **Attribute extraction**: Speed limits, surface type, lane count, one-way status
- **Special features**: Bridge and tunnel detection
- **Surface filtering**: Paved vs unpaved roads
- **Length calculation**: Haversine-based road segment lengths

## FFL Facets

```afl
namespace osm.Roads {
    // General extraction
    event facet ExtractRoads(cache: OSMCache, road_class: String = "all") => (result: RoadFeatures)

    // By classification
    event facet Motorways(cache: OSMCache) => (result: RoadFeatures)
    event facet PrimaryRoads(cache: OSMCache) => (result: RoadFeatures)
    event facet SecondaryRoads(cache: OSMCache) => (result: RoadFeatures)
    event facet TertiaryRoads(cache: OSMCache) => (result: RoadFeatures)
    event facet ResidentialRoads(cache: OSMCache) => (result: RoadFeatures)

    // Combined extractions
    event facet MajorRoads(cache: OSMCache) => (result: RoadFeatures)  // motorway + trunk + primary + secondary

    // Special types
    event facet Bridges(cache: OSMCache) => (result: RoadFeatures)
    event facet Tunnels(cache: OSMCache) => (result: RoadFeatures)

    // By surface
    event facet PavedRoads(cache: OSMCache) => (result: RoadFeatures)
    event facet UnpavedRoads(cache: OSMCache) => (result: RoadFeatures)

    // With attributes
    event facet RoadsWithSpeedLimit(cache: OSMCache) => (result: RoadFeatures)

    // Statistics and filtering
    event facet RoadStatistics(input_path: String) => (stats: RoadStats)
    event facet FilterRoadsByClass(input_path: String, road_class: String) => (result: RoadFeatures)
    event facet FilterBySpeedLimit(input_path: String, min_speed: Long, max_speed: Long) => (result: RoadFeatures)
}
```

## Result Schemas

```afl
schema RoadFeatures {
    output_path: String
    feature_count: Long
    road_class: String
    total_length_km: Double
    with_speed_limit: Long
    format: String
    extraction_date: String
}

schema RoadStats {
    total_roads: Long
    total_length_km: Double
    motorway_km: Double
    primary_km: Double
    secondary_km: Double
    tertiary_km: Double
    residential_km: Double
    other_km: Double
    with_speed_limit: Long
    with_surface: Long
    with_lanes: Long
    one_way_count: Long
}
```

## Road Classifications

| Class | OSM Highway Tags |
|-------|------------------|
| Motorway | `motorway`, `motorway_link` |
| Trunk | `trunk`, `trunk_link` |
| Primary | `primary`, `primary_link` |
| Secondary | `secondary`, `secondary_link` |
| Tertiary | `tertiary`, `tertiary_link` |
| Residential | `residential`, `living_street` |
| Service | `service` |
| Unclassified | `unclassified` |
| Track | `track` |
| Path | `path`, `footway`, `cycleway`, `bridleway` |

## Surface Types

**Paved surfaces**: `asphalt`, `concrete`, `paved`, `concrete:plates`, `concrete:lanes`, `paving_stones`, `sett`, `cobblestone`

**Unpaved surfaces**: `unpaved`, `gravel`, `dirt`, `sand`, `grass`, `ground`, `earth`, `mud`, `compacted`, `fine_gravel`

## Example Workflow

```afl
workflow AnalyzeRoadNetwork(region: String = "Liechtenstein")
    => (total_km: Double, motorway_km: Double, primary_km: Double, with_speed: Long) andThen {

    // Stage 1: Get cached region data
    cache = osm.ops.CacheRegion(region = $.region)

    // Stage 2: Extract all roads
    roads = osm.Roads.ExtractRoads(cache = cache.cache, road_class = "all")

    // Stage 3: Calculate statistics
    stats = osm.Roads.RoadStatistics(input_path = roads.result.output_path)

    yield AnalyzeRoadNetwork(
        total_km = stats.stats.total_length_km,
        motorway_km = stats.stats.motorway_km,
        primary_km = stats.stats.primary_km,
        with_speed = stats.stats.with_speed_limit
    )
}
```

## Speed Limit Filtering

```afl
workflow HighSpeedRoads(region: String = "Liechtenstein")
    => (map_path: String, road_count: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    roads = osm.Roads.RoadsWithSpeedLimit(cache = cache.cache)

    // Filter to roads with speed > 80 km/h
    high_speed = osm.Roads.FilterBySpeedLimit(
        input_path = roads.result.output_path,
        min_speed = 80,
        max_speed = 999
    )

    map = osm.viz.RenderMap(
        geojson_path = high_speed.result.output_path,
        title = "High Speed Roads (>80 km/h)",
        color = "#e74c3c"
    )

    yield HighSpeedRoads(
        map_path = map.result.output_path,
        road_count = high_speed.result.feature_count
    )
}
```

## GeoJSON Output

Each road feature includes:

```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 12345678,
    "osm_type": "way",
    "road_class": "primary",
    "highway": "primary",
    "name": "Main Street",
    "ref": "A1",
    "maxspeed": 50,
    "lanes": 2,
    "surface": "asphalt",
    "oneway": false,
    "bridge": false,
    "tunnel": false,
    "length_km": 1.234
  },
  "geometry": {
    "type": "LineString",
    "coordinates": [[9.5209, 47.1410], [9.5220, 47.1420]]
  }
}
```

## Length Calculation

Road lengths are calculated using the Haversine formula for accurate geodesic distance measurement. The `length_km` property on each feature gives the segment length, and statistics aggregate these for total network length by classification.

## Dependencies

Road extraction requires:
- `pyosmium` - For parsing OSM PBF files

Optional; handlers return empty results if unavailable.
