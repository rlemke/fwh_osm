# Building Extraction

Extract building footprints from OSM data with automatic classification.

## Features

- **Building classification**: Residential, commercial, industrial, retail, office, public, religious
- **Height data**: Extracts building height and level count for 3D visualization
- **Area calculation**: Computes footprint area in square meters
- **Filtering options**: By type, minimum area, or presence of height data

## FFL Facets

```afl
namespace osm.Buildings {
    // General extraction
    event facet ExtractBuildings(cache: OSMCache, building_type: String = "all") => (result: BuildingFeatures)

    // By classification
    event facet ResidentialBuildings(cache: OSMCache) => (result: BuildingFeatures)
    event facet CommercialBuildings(cache: OSMCache) => (result: BuildingFeatures)
    event facet IndustrialBuildings(cache: OSMCache) => (result: BuildingFeatures)
    event facet RetailBuildings(cache: OSMCache) => (result: BuildingFeatures)

    // Special extractions
    event facet Buildings3D(cache: OSMCache) => (result: BuildingFeatures)      // Buildings with height data
    event facet LargeBuildings(cache: OSMCache, min_area_m2: Double = 1000.0) => (result: BuildingFeatures)

    // Statistics and filtering
    event facet BuildingStatistics(input_path: String) => (stats: BuildingStats)
    event facet FilterBuildingsByType(input_path: String, building_type: String) => (result: BuildingFeatures)
}
```

## Result Schemas

```afl
schema BuildingFeatures {
    output_path: String
    feature_count: Long
    building_type: String
    total_area_km2: Double
    with_height_data: Long
    format: String
    extraction_date: String
}

schema BuildingStats {
    total_buildings: Long
    total_area_km2: Double
    residential: Long
    commercial: Long
    industrial: Long
    retail: Long
    other: Long
    avg_levels: Double
    with_height: Long
}
```

## Building Classifications

| Type | OSM Tags |
|------|----------|
| Residential | `house`, `residential`, `apartments`, `detached`, `semidetached_house`, `terrace`, `dormitory` |
| Commercial | `commercial`, `hotel` |
| Industrial | `industrial`, `warehouse`, `factory`, `manufacture` |
| Retail | `retail`, `supermarket`, `kiosk` |
| Office | `office` |
| Public | `public`, `civic`, `government`, `hospital`, `school`, `university`, `kindergarten` |
| Religious | `church`, `chapel`, `cathedral`, `mosque`, `temple`, `synagogue` |

## Example Workflow

```afl
workflow AnalyzeResidentialBuildings(region: String = "Liechtenstein")
    => (building_count: Long, total_area: Double, with_height: Long) andThen {

    // Stage 1: Get cached region data
    cache = osm.ops.CacheRegion(region = $.region)

    // Stage 2: Extract residential buildings
    buildings = osm.Buildings.ResidentialBuildings(cache = cache.cache)

    // Stage 3: Calculate statistics
    stats = osm.Buildings.BuildingStatistics(input_path = buildings.result.output_path)

    yield AnalyzeResidentialBuildings(
        building_count = stats.stats.residential,
        total_area = stats.stats.total_area_km2,
        with_height = stats.stats.with_height
    )
}
```

## GeoJSON Output

Each building feature includes:

```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 12345678,
    "osm_type": "way",
    "building_type": "residential",
    "name": "Example Building",
    "height": 12.5,
    "levels": 4,
    "area_m2": 450.0,
    "building": "apartments"
  },
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[...], [...]]]
  }
}
```

## Dependencies

Building extraction requires:
- `pyosmium` - For parsing OSM PBF files
- `shapely` - For geometry handling and area calculation

Both are optional; handlers return empty results if unavailable.
