# National and State Parks Extraction

Extract park boundaries and protected areas from OpenStreetMap data with classification support for national parks, state parks, nature reserves, and other protected areas.

## Park Types

| Type | OSM Tags | IUCN Category |
|------|----------|---------------|
| `national` | `boundary=national_park`, `protect_class=2` | Category II |
| `state` | `protect_class=5`, `designation=state_park` | Category V |
| `nature_reserve` | `leisure=nature_reserve`, `protect_class=1a/1b` | Category Ia/Ib |
| `protected_area` | `boundary=protected_area` | Any |
| `all` | Any park/protected area | All |

## IUCN Protection Classes

The `protect_class` tag in OSM corresponds to IUCN protected area categories:

| Class | IUCN Category | Description |
|-------|---------------|-------------|
| `1a` | Strict Nature Reserve | Strictly protected for biodiversity |
| `1b` | Wilderness Area | Large unmodified areas |
| `2` | **National Park** | Ecosystem protection and recreation |
| `3` | Natural Monument | Specific natural features |
| `4` | Habitat/Species Management | Active intervention for conservation |
| `5` | **Protected Landscape** | Often state/regional parks |
| `6` | Sustainable Use | Natural resources with sustainable use |

## Event Facets

### Typed Extraction

```afl
// Extract national parks (boundary=national_park or protect_class=2)
event facet NationalParks(cache: OSMCache) => (result: ParkFeatures)

// Extract state/regional parks (protect_class=5 or state-designated)
event facet StateParks(cache: OSMCache) => (result: ParkFeatures)

// Extract nature reserves (leisure=nature_reserve)
event facet NatureReserves(cache: OSMCache) => (result: ParkFeatures)
```

### Configurable Extraction

```afl
// Extract all protected areas with optional protect_class filter
// protect_classes: comma-separated like "1a,1b,2" or "*" for all
event facet ProtectedAreas(cache: OSMCache,
    protect_classes: String = "*") => (result: ParkFeatures)

// Extract parks by type
// park_type: "national", "state", "nature_reserve", "protected_area", "all"
event facet ExtractParks(cache: OSMCache, park_type: String = "all",
    protect_classes: String = "*") => (result: ParkFeatures)

// Extract parks with minimum area threshold (in km²)
event facet LargeParks(cache: OSMCache, min_area_km2: Double = 100,
    park_type: String = "all") => (result: ParkFeatures)
```

### Filtering and Statistics

```afl
// Filter existing GeoJSON by park type
event facet FilterParksByType(input_path: String, park_type: String,
    protect_classes: String = "*") => (result: ParkFeatures)

// Get park statistics
event facet ParkStatistics(input_path: String) => (stats: ParkStats)
```

## Result Schemas

### ParkFeatures
```afl
schema ParkFeatures {
    output_path: String      // Path to output GeoJSON file
    feature_count: Long      // Number of parks extracted
    park_type: String        // Park type filter used
    protect_classes: String  // Protect classes filter used
    total_area_km2: Double   // Total area of all parks
    format: String           // "GeoJSON"
    extraction_date: String  // ISO 8601 timestamp
}
```

### ParkStats
```afl
schema ParkStats {
    total_parks: Long        // Total number of parks
    total_area_km2: Double   // Combined area in km²
    national_parks: Long     // Count of national parks
    state_parks: Long        // Count of state/regional parks
    nature_reserves: Long    // Count of nature reserves
    other_protected: Long    // Count of other protected areas
    park_type: String        // Dominant park type
}
```

## Output Format

Parks are output as GeoJSON with detailed properties:

```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 123456,
    "osm_type": "relation",
    "name": "Yellowstone National Park",
    "park_type": "national",
    "protect_class": "2",
    "designation": "national_park",
    "operator": "National Park Service",
    "area_km2": 8983.18,
    "boundary": "national_park",
    "wikidata": "Q351"
  },
  "geometry": {
    "type": "MultiPolygon",
    "coordinates": [...]
  }
}
```

## Examples

### Extract All US National Parks
```afl
workflow USNationalParks() => (result: ParkFeatures) andThen {
    cache = osm.cache.NorthAmerica.US.UnitedStates()
    parks = osm.Parks.NationalParks(cache = cache.cache)
    yield USNationalParks(result = parks.result)
}
```

### Extract Large Parks with Statistics
```afl
workflow LargeParksWithStats() => (map_path: String, total_area: Double) andThen {
    cache = osm.cache.NorthAmerica.US.California()

    // Extract parks >= 100 km²
    parks = osm.Parks.LargeParks(cache = cache.cache, min_area_km2 = 100)

    // Get statistics
    stats = osm.Parks.ParkStatistics(input_path = parks.result.output_path)

    // Visualize
    map = osm.viz.RenderMap(
        geojson_path = parks.result.output_path,
        title = "Large Parks",
        color = "#27ae60"
    )

    yield LargeParksWithStats(
        map_path = map.result.output_path,
        total_area = stats.stats.total_area_km2
    )
}
```

### Filter by Protection Class
```afl
workflow StrictReserves() => (result: ParkFeatures) andThen {
    cache = osm.cache.Europe.Germany()

    // Extract only strict nature reserves (IUCN 1a, 1b)
    reserves = osm.Parks.ProtectedAreas(
        cache = cache.cache,
        protect_classes = "1a,1b"
    )

    yield StrictReserves(result = reserves.result)
}
```

## Python API

```python
from handlers.park_extractor import (
    extract_parks,
    filter_parks_by_type,
    calculate_park_stats,
    ParkType,
)

# Extract national parks from PBF
result = extract_parks(
    "region.osm.pbf",
    park_type="national",
)
print(f"Found {result.feature_count} national parks")
print(f"Total area: {result.total_area_km2:,.0f} km²")

# Filter existing GeoJSON for state parks only
filtered = filter_parks_by_type(
    "parks.geojson",
    park_type="state",
)

# Calculate statistics
stats = calculate_park_stats("parks.geojson")
print(f"National parks: {stats.national_parks}")
print(f"State parks: {stats.state_parks}")
print(f"Nature reserves: {stats.nature_reserves}")
print(f"Total area: {stats.total_area_km2:,.0f} km²")
```

## Classification Logic

Parks are classified using the following priority:

1. **National Parks**
   - `boundary=national_park`
   - `protect_class=2`
   - `designation` contains "national_park" or "national park"

2. **State/Regional Parks**
   - `protect_class=5`
   - `designation` contains "state_park", "regional_park", or "provincial_park"

3. **Nature Reserves**
   - `leisure=nature_reserve`
   - `protect_class=1a` or `protect_class=1b`
   - `designation` contains "nature_reserve"

4. **Generic Protected Area**
   - `boundary=protected_area`

## Requirements

- `pyosmium>=3.6` - For PBF file extraction
- `shapely>=2.0` - For geometry processing and area calculation
- `pyproj>=3.0` - For accurate geodesic area measurement (optional, falls back to spherical approximation)

The handlers gracefully degrade if dependencies are not installed, returning empty results for unsupported operations.
