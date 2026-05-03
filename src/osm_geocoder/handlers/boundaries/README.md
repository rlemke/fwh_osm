# Boundary Extraction for OSM Geocoder

This document describes the boundary extraction feature for the OSM Geocoder example.

## Overview

The boundary extraction module extracts administrative and natural boundaries from OpenStreetMap PBF files and outputs GeoJSON. It uses pyosmium for efficient PBF parsing and shapely for geometry handling.

## Dependencies

```
pyosmium>=3.6
shapely>=2.0
```

Install with:
```bash
pip install pyosmium shapely
```

## FFL Schema and Event Facets

The boundary extraction is defined in `afl/osmboundaries.ffl`:

### BoundaryFeatures Schema

```afl
schema BoundaryFeatures {
    output_path: String       // Path to the output GeoJSON file
    feature_count: Long       // Number of features extracted
    boundary_type: String     // Human-readable boundary type description
    admin_levels: String      // Comma-separated admin levels (e.g., "2,4")
    format: String            // Output format (always "GeoJSON")
    extraction_date: String   // ISO 8601 timestamp
}
```

### Event Facets

All event facets are in the `osm.Boundaries` namespace.

#### Administrative Boundaries

| Facet | Admin Level | Description |
|-------|-------------|-------------|
| `CountryBoundaries` | 2 | Country borders |
| `StateBoundaries` | 4 | State/province boundaries |
| `CountyBoundaries` | 6 | County/district boundaries |
| `CityBoundaries` | 8 | City/municipality boundaries |
| `AdminBoundary` | configurable | Extracts any admin level (default: 2) |

#### Natural Boundaries

| Facet | Natural Type | Description |
|-------|--------------|-------------|
| `LakeBoundaries` | water | Lakes, reservoirs, ponds |
| `ForestBoundaries` | forest | Woods and forests |
| `ParkBoundaries` | park | Parks and nature reserves |
| `NaturalBoundary` | configurable | Extracts any natural type (default: "water") |

## Admin Levels Reference

OSM uses numeric admin_level values:

| Level | Typical Use |
|-------|-------------|
| 2 | Country |
| 4 | State, Province, Region |
| 5 | State district (rare) |
| 6 | County, District |
| 7 | Municipality, Township |
| 8 | City, Town, Village |
| 9 | City district |
| 10 | Neighborhood |

## Natural Type Tags

The extractor matches these OSM tag combinations:

### Water (`natural_type: "water"`)
- `natural=water`
- `water=lake`
- `water=reservoir`
- `water=pond`

### Forest (`natural_type: "forest"`)
- `natural=wood`
- `landuse=forest`

### Park (`natural_type: "park"`)
- `leisure=park`
- `leisure=nature_reserve`
- `boundary=national_park`

## Usage

### In FFL Workflows

```afl
workflow ExtractUSCounties(cache: OSMCache) => (boundaries: BoundaryFeatures) andThen {
    counties = CountyBoundaries(cache = $.cache)
    yield ExtractUSCounties(boundaries = counties.result)
}
```

### With Configurable Admin Level

```afl
workflow ExtractAdminLevel(cache: OSMCache, level: Long) => (boundaries: BoundaryFeatures) andThen {
    admin = AdminBoundary(cache = $.cache, admin_level = $.level)
    yield ExtractAdminLevel(boundaries = admin.result)
}
```

### Programmatic Use

```python
from examples.osm-geocoder.handlers.boundary_extractor import extract_boundaries

# Extract country boundaries
result = extract_boundaries(
    "/path/to/region.osm.pbf",
    admin_levels=[2]
)
print(f"Extracted {result.feature_count} countries to {result.output_path}")

# Extract lakes and forests
result = extract_boundaries(
    "/path/to/region.osm.pbf",
    natural_types=["water", "forest"]
)
```

## Output Format

The extractor outputs GeoJSON FeatureCollections:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "osm_id": 12345,
        "osm_type": "relation",
        "name": "California",
        "boundary_type": "administrative",
        "admin_level": 4,
        "ISO3166-2": "US-CA"
      },
      "geometry": {
        "type": "MultiPolygon",
        "coordinates": [...]
      }
    }
  ]
}
```

## Output Directory

By default, extracted GeoJSON files are written to `/tmp/osm-boundaries/`. The filename includes:
- Input filename stem
- Admin levels (if specified)
- Natural types (if specified)

Example: `california_admin4.geojson`

## Testing

Run the end-to-end test with a mock handler:

```bash
PYTHONPATH=. python examples/osm-geocoder/test_boundaries.py
```

This tests the workflow execution without requiring actual PBF files or pyosmium.

## Integration with OSM Cache

The boundary facets accept an `OSMCache` parameter, which is the output of the regional cache facets (e.g., `osm.Europe.Monaco.Cache`). This allows chaining cache lookups with boundary extraction:

```afl
workflow MonacoBoundaries() => (boundaries: BoundaryFeatures) andThen {
    cache = Monaco.Cache()
    countries = CountryBoundaries(cache = cache.result)
    yield MonacoBoundaries(boundaries = countries.result)
}
```
