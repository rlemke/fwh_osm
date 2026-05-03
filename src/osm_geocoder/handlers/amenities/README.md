# Amenity Extraction

Extract amenities (points of interest with services) from OSM data with automatic categorization.

## Features

- **Category classification**: Food, shopping, services, healthcare, education, entertainment, transport
- **Detailed metadata**: Name, opening hours, phone, website, brand, cuisine
- **Search capability**: Filter amenities by name pattern (regex)
- **Statistics**: Count amenities by category with metadata coverage

## FFL Facets

```afl
namespace osm.Amenities {
    // General extraction
    event facet ExtractAmenities(cache: OSMCache, category: String = "all") => (result: AmenityFeatures)

    // Food & Drink
    event facet FoodAndDrink(cache: OSMCache) => (result: AmenityFeatures)
    event facet Restaurants(cache: OSMCache) => (result: AmenityFeatures)
    event facet Cafes(cache: OSMCache) => (result: AmenityFeatures)
    event facet Bars(cache: OSMCache) => (result: AmenityFeatures)
    event facet FastFood(cache: OSMCache) => (result: AmenityFeatures)

    // Shopping
    event facet Shopping(cache: OSMCache) => (result: AmenityFeatures)
    event facet Supermarkets(cache: OSMCache) => (result: AmenityFeatures)

    // Services
    event facet Banks(cache: OSMCache) => (result: AmenityFeatures)
    event facet ATMs(cache: OSMCache) => (result: AmenityFeatures)
    event facet PostOffices(cache: OSMCache) => (result: AmenityFeatures)
    event facet FuelStations(cache: OSMCache) => (result: AmenityFeatures)
    event facet ChargingStations(cache: OSMCache) => (result: AmenityFeatures)
    event facet Parking(cache: OSMCache) => (result: AmenityFeatures)

    // Healthcare
    event facet Healthcare(cache: OSMCache) => (result: AmenityFeatures)
    event facet Hospitals(cache: OSMCache) => (result: AmenityFeatures)
    event facet Clinics(cache: OSMCache) => (result: AmenityFeatures)
    event facet Pharmacies(cache: OSMCache) => (result: AmenityFeatures)
    event facet Dentists(cache: OSMCache) => (result: AmenityFeatures)

    // Education
    event facet Education(cache: OSMCache) => (result: AmenityFeatures)
    event facet Schools(cache: OSMCache) => (result: AmenityFeatures)
    event facet Universities(cache: OSMCache) => (result: AmenityFeatures)
    event facet Libraries(cache: OSMCache) => (result: AmenityFeatures)

    // Entertainment
    event facet Entertainment(cache: OSMCache) => (result: AmenityFeatures)
    event facet Cinemas(cache: OSMCache) => (result: AmenityFeatures)
    event facet Theatres(cache: OSMCache) => (result: AmenityFeatures)

    // Statistics and filtering
    event facet AmenityStatistics(input_path: String) => (stats: AmenityStats)
    event facet SearchAmenities(input_path: String, name_pattern: String) => (result: AmenityFeatures)
    event facet FilterByCategory(input_path: String, category: String) => (result: AmenityFeatures)
}
```

## Result Schemas

```afl
schema AmenityFeatures {
    output_path: String
    feature_count: Long
    amenity_category: String
    amenity_types: String
    format: String
    extraction_date: String
}

schema AmenityStats {
    total_amenities: Long
    food: Long
    shopping: Long
    services: Long
    healthcare: Long
    education: Long
    entertainment: Long
    transport: Long
    other: Long
    with_name: Long
    with_opening_hours: Long
}
```

## Amenity Categories

| Category | OSM Tags |
|----------|----------|
| Food | `restaurant`, `cafe`, `bar`, `pub`, `fast_food`, `food_court`, `ice_cream`, `biergarten` |
| Shopping | `supermarket`, `convenience`, `mall`, `department_store`, `clothes`, `shoes`, `electronics` + any `shop=*` |
| Services | `bank`, `atm`, `post_office`, `fuel`, `car_wash`, `car_rental`, `charging_station`, `parking` |
| Healthcare | `hospital`, `clinic`, `doctors`, `dentist`, `pharmacy`, `veterinary` |
| Education | `school`, `university`, `college`, `library`, `kindergarten` |
| Entertainment | `cinema`, `theatre`, `nightclub`, `casino`, `arts_centre` |
| Transport | `bus_station`, `ferry_terminal`, `taxi` |

## Example Workflow

```afl
workflow FindRestaurants(region: String = "Liechtenstein")
    => (map_path: String, restaurant_count: Long) andThen {

    // Stage 1: Get cached region data
    cache = osm.ops.CacheRegion(region = $.region)

    // Stage 2: Extract restaurants
    restaurants = osm.Amenities.Restaurants(cache = cache.cache)

    // Stage 3: Visualize on map
    map = osm.viz.RenderMap(
        geojson_path = restaurants.result.output_path,
        title = "Restaurants",
        color = "#e74c3c"
    )

    yield FindRestaurants(
        map_path = map.result.output_path,
        restaurant_count = restaurants.result.feature_count
    )
}
```

## Search Example

```afl
workflow FindCoffeeShops(region: String = "Liechtenstein")
    => (result_path: String, count: Long) andThen {

    cache = osm.ops.CacheRegion(region = $.region)
    cafes = osm.Amenities.Cafes(cache = cache.cache)

    // Search by name pattern (regex)
    starbucks = osm.Amenities.SearchAmenities(
        input_path = cafes.result.output_path,
        name_pattern = "(?i)starbucks|costa|nero"
    )

    yield FindCoffeeShops(
        result_path = starbucks.result.output_path,
        count = starbucks.result.feature_count
    )
}
```

## GeoJSON Output

Each amenity feature includes:

```json
{
  "type": "Feature",
  "properties": {
    "osm_id": 12345678,
    "osm_type": "node",
    "amenity": "restaurant",
    "shop": "",
    "category": "food",
    "name": "La Trattoria",
    "opening_hours": "Mo-Sa 11:00-22:00",
    "phone": "+1-555-0123",
    "website": "https://example.com",
    "cuisine": "italian",
    "brand": ""
  },
  "geometry": {
    "type": "Point",
    "coordinates": [9.5209, 47.1410]
  }
}
```

## Dependencies

Amenity extraction requires:
- `pyosmium` - For parsing OSM PBF files

Optional; handlers return empty results if unavailable.
