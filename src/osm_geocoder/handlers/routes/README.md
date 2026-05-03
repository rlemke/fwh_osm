# OSM Route Extraction

Extract routes and related infrastructure from OpenStreetMap PBF files for various transport modes.

## Supported Route Types

| Type | Aliases | Description |
|------|---------|-------------|
| `bicycle` | bike, cycling, cycle | Cycle routes, cycleways, bike infrastructure |
| `hiking` | hike, walking, foot, trail | Hiking/walking trails, footpaths |
| `train` | rail, railway | Railway lines, stations |
| `bus` | - | Bus routes, stops |
| `public_transport` | transit, pt | All public transport modes |

## Event Facets

### Generic Extractors

#### ExtractRoutes
Extract routes by transport type from a PBF file.

```afl
event facet ExtractRoutes(
    cache: OSMCache,
    route_type: String,          // "bicycle", "hiking", "train", "bus"
    network: String = "*",       // Network level filter
    include_infrastructure: Boolean = true
) => (result: RouteFeatures)
```

#### FilterRoutesByType
Filter already-extracted GeoJSON by route type.

```afl
event facet FilterRoutesByType(
    input_path: String,
    route_type: String,
    network: String = "*"
) => (result: RouteFeatures)
```

#### RouteStatistics
Calculate statistics for extracted routes.

```afl
event facet RouteStatistics(input_path: String) => (stats: RouteStats)
```

### Convenience Facets

Pre-configured facets for specific route types:

```afl
event facet BicycleRoutes(cache: OSMCache, network: String = "*",
    include_infrastructure: Boolean = true) => (result: RouteFeatures)

event facet HikingTrails(cache: OSMCache, network: String = "*",
    include_infrastructure: Boolean = true) => (result: RouteFeatures)

event facet TrainRoutes(cache: OSMCache,
    include_infrastructure: Boolean = true) => (result: RouteFeatures)

event facet BusRoutes(cache: OSMCache,
    include_infrastructure: Boolean = true) => (result: RouteFeatures)

event facet PublicTransport(cache: OSMCache) => (result: RouteFeatures)
```

## Network Levels

### Bicycle Routes
| Code | Meaning |
|------|---------|
| `icn` | International Cycle Network |
| `ncn` | National Cycle Network |
| `rcn` | Regional Cycle Network |
| `lcn` | Local Cycle Network |

### Hiking/Walking Routes
| Code | Meaning |
|------|---------|
| `iwn` | International Walking Network |
| `nwn` | National Walking Network |
| `rwn` | Regional Walking Network |
| `lwn` | Local Walking Network |

## OSM Tags Extracted

### Bicycle

**Routes** (relations):
- `route=bicycle`
- `route=mtb`

**Ways**:
- `highway=cycleway`
- `cycleway=lane|track|opposite|opposite_lane|opposite_track|shared_lane`
- `bicycle=designated|yes`

**Infrastructure** (when `include_infrastructure=true`):
- `amenity=bicycle_parking|bicycle_rental|bicycle_repair_station`
- `shop=bicycle`

### Hiking

**Routes** (relations):
- `route=hiking|foot|walking`

**Ways**:
- `highway=path|footway|pedestrian|track`
- `foot=designated|yes`
- `sac_scale=hiking|mountain_hiking|demanding_mountain_hiking|alpine_hiking|demanding_alpine_hiking|difficult_alpine_hiking`

**Infrastructure**:
- `amenity=shelter|drinking_water`
- `tourism=alpine_hut|wilderness_hut|viewpoint|picnic_site|camp_site`
- `information=guidepost|map|board`

### Train

**Routes** (relations):
- `route=train|railway|light_rail|subway|tram`

**Ways**:
- `railway=rail|light_rail|subway|tram|narrow_gauge`

**Infrastructure**:
- `railway=station|halt|tram_stop|subway_entrance`
- `public_transport=station|stop_position|platform`

### Bus

**Routes** (relations):
- `route=bus|trolleybus`

**Ways**:
- `highway=bus_guideway`
- `bus=designated`

**Infrastructure**:
- `amenity=bus_station`
- `highway=bus_stop`
- `public_transport=stop_position|platform|station`

## Output Format

Routes are output as GeoJSON FeatureCollections with the following feature types:

### Route Features (from relations)
```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 12345,
    "osm_type": "relation",
    "feature_type": "route",
    "route_type": "bicycle",
    "member_count": 42,
    "name": "National Cycle Route 1",
    "network": "ncn",
    "ref": "1"
  },
  "geometry": null
}
```

### Way Features
```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 67890,
    "osm_type": "way",
    "feature_type": "way",
    "route_type": "bicycle",
    "highway": "cycleway",
    "surface": "asphalt"
  },
  "geometry": {
    "type": "LineString",
    "coordinates": [[-0.1, 51.5], [-0.2, 51.6]]
  }
}
```

### Infrastructure Features
```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 11111,
    "osm_type": "node",
    "feature_type": "infrastructure",
    "route_type": "bicycle",
    "amenity": "bicycle_parking",
    "capacity": "20"
  },
  "geometry": {
    "type": "Point",
    "coordinates": [-0.15, 51.55]
  }
}
```

## Result Schemas

### RouteFeatures
```afl
schema RouteFeatures {
    output_path: String        // Path to output GeoJSON file
    feature_count: Long        // Total features extracted
    route_type: String         // "bicycle", "hiking", etc.
    network_level: String      // Network filter used ("*" for all)
    include_infrastructure: Boolean
    format: String             // "GeoJSON"
    extraction_date: String    // ISO 8601 timestamp
}
```

### RouteStats
```afl
schema RouteStats {
    route_count: Long          // Number of route relations
    total_length_km: Double    // Total length of ways in km
    infrastructure_count: Long // Number of infrastructure POIs
    route_type: String         // Route type analyzed
}
```

## Python API

```python
from handlers.route_extractor import extract_routes, RouteType

# Extract bicycle routes from a PBF file
result = extract_routes(
    "region.osm.pbf",
    route_type="bicycle",
    network="ncn",  # National cycle network only
    include_infrastructure=True,
)

print(f"Extracted {result.feature_count} features to {result.output_path}")

# Calculate statistics
from handlers.route_extractor import calculate_route_stats

stats = calculate_route_stats(result.output_path)
print(f"Routes: {stats.route_count}")
print(f"Total length: {stats.total_length_km:.1f} km")
print(f"Infrastructure: {stats.infrastructure_count}")
```

## Requirements

- `pyosmium>=3.6` - For PBF file processing

The handlers gracefully degrade if pyosmium is not installed, returning empty results.
