# Population-Based Filtering

Filter OSM data by population for cities, towns, villages, states, countries, counties, and other administrative divisions.

## Supported Place Types

| Type | OSM Tags | Description |
|------|----------|-------------|
| `city` | `place=city` | Large urban areas |
| `town` | `place=town` | Medium-sized settlements |
| `village` | `place=village` | Small settlements |
| `hamlet` | `place=hamlet` | Very small settlements |
| `suburb` | `place=suburb/neighbourhood/quarter` | Urban subdivisions |
| `country` | `admin_level=2` | Countries/nations |
| `state` | `admin_level=4` | States/provinces |
| `county` | `admin_level=6` | Counties/regions |
| `municipality` | `admin_level=8` | Municipalities |
| `all` | Any with `population` tag | All places with population data |

## Event Facets

### Generic Filters

#### FilterByPopulation
Filter GeoJSON by population threshold.

```afl
event facet FilterByPopulation(
    input_path: String,
    min_population: Long,
    place_type: String = "all",     // "city", "town", "state", etc.
    operator: String = "gte"         // "gt", "gte", "lt", "lte", "eq", "ne"
) => (result: PopulationFilteredFeatures)
```

#### FilterByPopulationRange
Filter GeoJSON by population range (inclusive).

```afl
event facet FilterByPopulationRange(
    input_path: String,
    min_population: Long,
    max_population: Long,
    place_type: String = "all"
) => (result: PopulationFilteredFeatures)
```

#### ExtractPlacesWithPopulation
Extract places with population directly from PBF file.

```afl
event facet ExtractPlacesWithPopulation(
    cache: OSMCache,
    place_type: String = "all",
    min_population: Long = 0
) => (result: PopulationFilteredFeatures)
```

#### PopulationStatistics
Calculate population statistics for a GeoJSON file.

```afl
event facet PopulationStatistics(
    input_path: String,
    place_type: String = "all"
) => (stats: PopulationStats)
```

### Convenience Facets

Pre-configured facets for common place types:

```afl
// Urban places
event facet Cities(cache: OSMCache, min_population: Long = 0) => (result: PopulationFilteredFeatures)
event facet Towns(cache: OSMCache, min_population: Long = 0) => (result: PopulationFilteredFeatures)
event facet Villages(cache: OSMCache, min_population: Long = 0) => (result: PopulationFilteredFeatures)

// Administrative divisions
event facet Countries(cache: OSMCache) => (result: PopulationFilteredFeatures)
event facet States(cache: OSMCache) => (result: PopulationFilteredFeatures)
event facet Counties(cache: OSMCache) => (result: PopulationFilteredFeatures)

// All populated places
event facet AllPopulatedPlaces(cache: OSMCache, min_population: Long = 0) => (result: PopulationFilteredFeatures)
```

## Operators

| Operator | Aliases | Description |
|----------|---------|-------------|
| `gt` | `>` | Greater than |
| `gte` | `>=` | Greater than or equal |
| `lt` | `<` | Less than |
| `lte` | `<=` | Less than or equal |
| `eq` | `=`, `==` | Equal to |
| `ne` | `!=`, `<>` | Not equal to |
| `between` | `range` | Between min and max (inclusive) |

## Result Schemas

### PopulationFilteredFeatures
```afl
schema PopulationFilteredFeatures {
    output_path: String        // Path to output GeoJSON file
    feature_count: Long        // Number of features after filtering
    original_count: Long       // Number of features before filtering
    place_type: String         // Place type filtered
    min_population: Long       // Minimum population threshold
    max_population: Long       // Maximum population (for range filters)
    filter_applied: String     // Human-readable filter description
    format: String             // "GeoJSON"
    extraction_date: String    // ISO 8601 timestamp
}
```

### PopulationStats
```afl
schema PopulationStats {
    total_places: Long         // Number of places with population
    total_population: Long     // Sum of all populations
    min_population: Long       // Smallest population
    max_population: Long       // Largest population
    avg_population: Long       // Average population
    place_type: String         // Place type analyzed
}
```

## Population Data in OSM

Population data in OSM comes from the `population` tag. The parser handles various formats:

- Simple integers: `1234`, `1000000`
- Comma separators: `1,234,567` (English format)
- Period separators: `1.234.567` (European format)
- Approximate values: `~1000`, `≈1000`
- Plus suffix: `1000+`

## Examples

### Extract Large Cities
```afl
workflow LargeCities() => (map_path: String) andThen {
    cache = osm.cache.Europe.Germany()
    cities = osm.Population.Cities(cache = cache.cache, min_population = 100000)
    map = osm.viz.RenderMap(
        geojson_path = cities.result.output_path,
        title = "Cities over 100,000",
        color = "#e74c3c"
    )
    yield LargeCities(map_path = map.result.output_path)
}
```

### Filter by Population Range
```afl
workflow MediumCities() => (result: PopulationFilteredFeatures) andThen {
    cache = osm.cache.Europe.France()
    all_cities = osm.Population.Cities(cache = cache.cache)
    filtered = osm.Population.FilterByPopulationRange(
        input_path = all_cities.result.output_path,
        min_population = 50000,
        max_population = 500000,
        place_type = "city"
    )
    yield MediumCities(result = filtered.result)
}
```

### Get Population Statistics
```afl
workflow CityStats() => (total: Long, largest: Long, average: Long) andThen {
    cache = osm.cache.NorthAmerica.US.California()
    cities = osm.Population.Cities(cache = cache.cache)
    stats = osm.Population.PopulationStatistics(
        input_path = cities.result.output_path,
        place_type = "city"
    )
    yield CityStats(
        total = stats.stats.total_population,
        largest = stats.stats.max_population,
        average = stats.stats.avg_population
    )
}
```

## Python API

```python
from handlers.population_filter import (
    extract_places_with_population,
    filter_geojson_by_population,
    calculate_population_stats,
    PlaceType,
    Operator,
)

# Extract cities from PBF
result = extract_places_with_population(
    "region.osm.pbf",
    place_type="city",
    min_population=50000,
)
print(f"Found {result.feature_count} cities")

# Filter existing GeoJSON
filtered = filter_geojson_by_population(
    "places.geojson",
    min_population=100000,
    max_population=1000000,
    place_type="city",
    operator="between",
)

# Calculate statistics
stats = calculate_population_stats("cities.geojson", place_type="city")
print(f"Total population: {stats.total_population:,}")
print(f"Average: {stats.avg_population:,}")
print(f"Range: {stats.min_population:,} - {stats.max_population:,}")
```

## Requirements

- `pyosmium>=3.6` - For PBF file extraction (optional, GeoJSON filtering works without it)

The handlers gracefully degrade if pyosmium is not installed, returning empty results for PBF operations.
