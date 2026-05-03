# Point of Interest (POI) Extraction

The POI layer extracts geographic features — cities, towns, villages, and other points of interest — from downloaded OSM data. POI facets receive an `OSMCache` input (typically from a download step) and return extracted feature data.

## FFL facets

POI facets are defined in `afl/osmpoi.ffl` under the `osm.POIs` namespace:

```afl
namespace osm.POIs {
    event facet POI(cache: OSMCache) => (pois: OSMCache)

    facet Volcanos(cache: OSMCache, elevation: Long, active: Boolean) => (pois: OSMCache)
    facet Prisons(operator: String, capacity: Long, securityLevel: String, gender: String) => (pois: OSMCache)
    facet NationalParks() => (pois: OSMCache)
    facet StateParks() => (pois: OSMCache)
    facet Hospitals() => (pois: OSMCache)
    facet Airports() => (pois: OSMCache)
    facet Military(kind: String) => (pois: OSMCache)

    event facet Cities(cache: OSMCache) => (cities: OSMCache)
    event facet Towns(cache: OSMCache) => (towns: OSMCache)
    event facet Suburbs(cache: OSMCache) => (towns: OSMCache)
    event facet Villages(cache: OSMCache) => (villages: OSMCache)
    event facet Hamlets(cache: OSMCache) => (villages: OSMCache)
    event facet Countries(cache: OSMCache) => (villages: OSMCache)
    event facet GeoOSMCache(cache: OSMCache) => (geojson: OSMCache)
}
```

### Event facets vs plain facets

The namespace contains two kinds of facets:

**Event facets** (8) — trigger agent execution. These are the facets that have Python handlers registered:

| Facet | Input | Return | Description |
|-------|-------|--------|-------------|
| `POI` | `cache: OSMCache` | `pois: OSMCache` | General POI extraction |
| `Cities` | `cache: OSMCache` | `cities: OSMCache` | Extract city-level settlements |
| `Towns` | `cache: OSMCache` | `towns: OSMCache` | Extract town-level settlements |
| `Suburbs` | `cache: OSMCache` | `towns: OSMCache` | Extract suburb boundaries |
| `Villages` | `cache: OSMCache` | `villages: OSMCache` | Extract village-level settlements |
| `Hamlets` | `cache: OSMCache` | `villages: OSMCache` | Extract hamlet-level settlements |
| `Countries` | `cache: OSMCache` | `villages: OSMCache` | Extract country boundaries |
| `GeoOSMCache` | `cache: OSMCache` | `geojson: OSMCache` | Generate GeoJSON cache |

**Plain facets** (7) — typed signatures for specialized queries (Volcanos, Prisons, NationalParks, StateParks, Hospitals, Airports, Military). These define the parameter schema but are not currently wired to event handlers. They serve as placeholders for future agent implementations that would accept richer query parameters.

## Python handler

POI handlers are registered in `handlers/poi_handlers.py`. Each event facet gets a handler that logs the extraction and returns an `OSMCache` struct under the facet's return parameter name:

```python
POI_FACETS = {
    "POI": "pois",
    "Cities": "cities",
    "Towns": "towns",
    "Suburbs": "towns",
    "Villages": "villages",
    "Hamlets": "villages",
    "Countries": "villages",
    "GeoOSMCache": "geojson",
}
```

The handler factory:

```python
def _make_poi_handler(facet_name: str, return_param: str):
    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        return {
            return_param: {
                "url": cache.get("url", ""),
                "path": cache.get("path", ""),
                "date": cache.get("date", ""),
                "size": 0,
                "wasInCache": True,
            }
        }
    return handler
```

All handlers are registered under the qualified name `osm.POIs.<FacetName>`.

### Return parameter grouping

Several facets share the same return parameter name, reflecting a logical grouping:

| Return parameter | Facets |
|-----------------|--------|
| `pois` | POI |
| `cities` | Cities |
| `towns` | Towns, Suburbs |
| `villages` | Villages, Hamlets, Countries |
| `geojson` | GeoOSMCache |

This grouping means that `Towns` and `Suburbs` results can be used interchangeably in workflows that expect a `towns` output, and similarly for the `villages` group.

## Usage in workflows

POI facets are typically chained after a download operation:

```afl
use osm.ops
use osm.POIs
use osm.cache.Europe

facet ExtractFranceCities() => (cities: OSMCache) andThen {
    france = France()                              // cache lookup
    dl_france = Download(cache = france.cache)     // download PBF
    fr_cities = Cities(cache = dl_france.cache)    // extract cities
    yield ExtractFranceCities(cities = fr_cities.cities)
}
```

The data flow is:
1. Cache facet resolves the region URL
2. Download fetches the PBF file
3. POI facet extracts features from the downloaded data

## Adding a new POI type

To add a new POI extraction type:

1. Add the event facet to `afl/osmpoi.ffl`:
   ```afl
   event facet Universities(cache: OSMCache) => (universities: OSMCache)
   ```

2. Add the entry to `POI_FACETS` in `handlers/poi_handlers.py`:
   ```python
   POI_FACETS = {
       # ...
       "Universities": "universities",
   }
   ```

The handler is generated automatically from the dict entry. No other changes are needed.
