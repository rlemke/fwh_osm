# OSM Filters

Filter OSM data by size (equivalent radius) or element type/tags.

## Radius Filtering

The **equivalent radius** converts any polygon to a comparable size metric by computing the radius of a circle with the same area:

```
radius = √(area / π)
```

This allows filtering boundaries by size regardless of shape complexity.

## Type/Tag Filtering

Filter by OSM element type (node, way, relation) and/or tag key/value pairs. Optionally include dependencies (referenced nodes for ways) for complete geometry reconstruction.

## FFL Facets

All filter facets are defined in `osmfilters.ffl` under the `osm.Filters` namespace.

### FilterByRadius

Filter GeoJSON features by equivalent radius.

```afl
event facet FilterByRadius(
    input_path: String,       // Path to input GeoJSON file
    radius: Double,           // Threshold value
    unit: String = "kilometers",  // meters, kilometers, miles
    operator: String = "gte"  // gt, gte, lt, lte, eq, ne
) => (result: FilteredFeatures)
```

### FilterByRadiusRange

Filter GeoJSON features within an inclusive radius range.

```afl
event facet FilterByRadiusRange(
    input_path: String,
    min_radius: Double,       // Lower bound (inclusive)
    max_radius: Double,       // Upper bound (inclusive)
    unit: String = "kilometers"
) => (result: FilteredFeatures)
```

### FilterByTypeAndRadius

Filter GeoJSON features by boundary type and radius.

```afl
event facet FilterByTypeAndRadius(
    input_path: String,
    boundary_type: String,    // water, forest, park, etc.
    radius: Double,
    unit: String = "kilometers",
    operator: String = "gte"
) => (result: FilteredFeatures)
```

### ExtractAndFilterByRadius

Extract boundaries from PBF and filter by radius in one step.

```afl
event facet ExtractAndFilterByRadius(
    cache: OSMCache,          // OSM cache with path to PBF file
    admin_levels: [Long],     // Admin levels to extract (2=country, 4=state, etc.)
    natural_types: [String],  // Natural types (water, forest, park)
    radius: Double,
    unit: String = "kilometers",
    operator: String = "gte"
) => (result: FilteredFeatures)
```

### FilterByOSMType

Filter PBF file by OSM element type (node, way, relation).

```afl
event facet FilterByOSMType(
    input_path: String,              // Path to input PBF file
    osm_type: String,                // node, way, relation, or * for all
    include_dependencies: Boolean = false  // Include referenced nodes for ways
) => (result: OSMFilteredFeatures)
```

### FilterByOSMTag

Filter PBF file by OSM tag key/value.

```afl
event facet FilterByOSMTag(
    input_path: String,              // Path to input PBF file
    tag_key: String,                 // Tag key to filter by (e.g., "amenity")
    tag_value: String = "*",         // Tag value, or "*" for any value
    osm_type: String = "*",          // Element type filter, or "*" for all
    include_dependencies: Boolean = false
) => (result: OSMFilteredFeatures)
```

### FilterGeoJSONByOSMType

Filter already-extracted GeoJSON by OSM type stored in feature properties.

```afl
event facet FilterGeoJSONByOSMType(
    input_path: String,              // Path to input GeoJSON file
    osm_type: String,                // node, way, relation, or * for all
    tag_key: String = "",            // Optional tag key filter
    tag_value: String = "*"          // Tag value, or "*" for any value
) => (result: OSMFilteredFeatures)
```

## OSMFilteredFeatures Schema

```afl
schema OSMFilteredFeatures {
    output_path: String              // Path to filtered GeoJSON
    feature_count: Long              // Features after filtering
    original_count: Long             // Elements scanned
    osm_type: String                 // Type filter applied
    filter_applied: String           // Human-readable filter description
    dependencies_included: Boolean   // Whether dependencies were included
    dependency_count: Long           // Number of dependency nodes collected
    format: String                   // Always "GeoJSON"
    extraction_date: String          // ISO timestamp
}
```

## OSM Type Aliases

| Type | Aliases |
|------|---------|
| `node` | `n` |
| `way` | `w` |
| `relation` | `rel`, `r` |
| `*` (all) | `all`, `any` |

## Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `gt` | Greater than | radius > 10km |
| `gte` | Greater than or equal | radius >= 10km |
| `lt` | Less than | radius < 10km |
| `lte` | Less than or equal | radius <= 10km |
| `eq` | Equal (1% tolerance) | radius ≈ 10km |
| `ne` | Not equal | radius ≠ 10km |
| `between` | Inclusive range | 5km ≤ radius ≤ 20km |

## Units

| Unit | Aliases |
|------|---------|
| `meters` | `m`, `meter` |
| `kilometers` | `km`, `kilometer` |
| `miles` | `mi`, `mile` |

## FilteredFeatures Schema

```afl
schema FilteredFeatures {
    output_path: String       // Path to filtered GeoJSON
    feature_count: Long       // Features after filtering
    original_count: Long      // Features before filtering
    boundary_type: String     // Type filter applied (or "all")
    filter_applied: String    // Human-readable filter description
    format: String            // Always "GeoJSON"
    extraction_date: String   // ISO timestamp
}
```

## Usage Examples

### Filter large lakes (>= 5km radius)

```afl
workflow LargeLakes(input: String) => (result: FilteredFeatures) andThen {
    filtered = FilterByRadius(
        input_path = $.input,
        radius = 5.0,
        unit = "kilometers",
        operator = "gte"
    )
    yield LargeLakes(result = filtered.result)
}
```

### Filter medium-sized parks (1-10km radius)

```afl
workflow MediumParks(input: String) => (result: FilteredFeatures) andThen {
    filtered = FilterByRadiusRange(
        input_path = $.input,
        min_radius = 1.0,
        max_radius = 10.0,
        unit = "kilometers"
    )
    yield MediumParks(result = filtered.result)
}
```

### Extract and filter state boundaries from PBF

```afl
workflow LargeStates() => (result: FilteredFeatures)
    with Germany()
andThen {
    filtered = ExtractAndFilterByRadius(
        cache = Germany.cache,
        admin_levels = [4],
        natural_types = [],
        radius = 50.0,
        unit = "kilometers",
        operator = "gte"
    )
    yield LargeStates(result = filtered.result)
}
```

### Extract all restaurants from PBF

```afl
workflow Restaurants(pbf_path: String) => (result: OSMFilteredFeatures) andThen {
    filtered = FilterByOSMTag(
        input_path = $.pbf_path,
        tag_key = "amenity",
        tag_value = "restaurant",
        osm_type = "node",          // Restaurants are usually nodes
        include_dependencies = false
    )
    yield Restaurants(result = filtered.result)
}
```

### Extract all highways with full geometry

```afl
workflow Highways(pbf_path: String) => (result: OSMFilteredFeatures) andThen {
    filtered = FilterByOSMTag(
        input_path = $.pbf_path,
        tag_key = "highway",
        tag_value = "*",            // Any highway type
        osm_type = "way",
        include_dependencies = true // Include nodes for geometry
    )
    yield Highways(result = filtered.result)
}
```

### Filter existing GeoJSON by type

```afl
workflow JustNodes(geojson_path: String) => (result: OSMFilteredFeatures) andThen {
    filtered = FilterGeoJSONByOSMType(
        input_path = $.geojson_path,
        osm_type = "node"
    )
    yield JustNodes(result = filtered.result)
}
```

## Dependency Inclusion

When `include_dependencies = true`:

- **Ways**: All referenced nodes are collected in a second pass and included
  in the output. This allows complete geometry reconstruction.
- **Relations**: Member information is stored in properties (full member
  resolution requires additional processing).
- **Nodes**: No effect (nodes have no dependencies).

This requires two passes over the PBF file:
1. First pass: Identify matching elements and collect needed node IDs
2. Second pass: Collect coordinates for needed nodes

## Area Calculation

The module uses pyproj with an Albers Equal Area projection for accurate geodesic area measurements. When pyproj is unavailable, it falls back to a spherical approximation using coordinate scaling.

The projection is centered on each polygon's centroid for maximum accuracy:

```
+proj=aea +lat_1={lat-5} +lat_2={lat+5} +lat_0={lat} +lon_0={lon}
```

## Dependencies

- `shapely>=2.0` - Required for radius-based filtering and geometry operations
- `pyproj>=3.0` - Recommended for accurate geodesic area (optional, falls back to approximation)
- `pyosmium>=3.6` - Required for PBF file filtering (FilterByOSMType, FilterByOSMTag)

## Running Tests

```bash
# From repo root
pytest examples/osm-geocoder/test_filters.py -v
```
