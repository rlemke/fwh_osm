# US Census TIGER Voting District Data

Download and process US electoral boundary data from the Census Bureau's TIGER/Line files.

## Data Sources

The Census Bureau provides electoral boundary shapefiles at:
https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html

### Supported District Types

| Type | Description | Scope | Update Frequency |
|------|-------------|-------|------------------|
| Congressional Districts | US House of Representatives | Nationwide | After redistricting |
| State Senate (SLDU) | State upper chamber | Per state | After redistricting |
| State House (SLDL) | State lower chamber | Per state | After redistricting |
| Voting Precincts (VTD) | Local voting districts | Per state | Decennial census |

## FFL Facets

All facets are defined in `osmvoting.ffl`.

### Download Facets (census.tiger.Districts)

```afl
// Congressional Districts (nationwide)
event facet CongressionalDistricts(
    year: Long = 2023,
    congress_number: Long = 118  // 118th Congress = 2023-2025
) => (cache: TIGERCache)

// State Senate Districts
event facet StateSenateDistricts(
    state_fips: String,          // "06" for CA, "48" for TX, etc.
    year: Long = 2023
) => (cache: TIGERCache)

// State House Districts
event facet StateHouseDistricts(
    state_fips: String,
    year: Long = 2023
) => (cache: TIGERCache)

// Voting Precincts (from decennial census)
event facet VotingPrecincts(
    state_fips: String,
    year: Long = 2020            // 2020 or 2010
) => (cache: TIGERCache)
```

### Processing Facets (census.tiger.Processing)

```afl
// Convert TIGER shapefile ZIP to GeoJSON
event facet ShapefileToGeoJSON(
    cache: TIGERCache
) => (result: VotingDistrictResult)

// Filter districts by attribute
event facet FilterDistricts(
    input_path: String,
    attribute: String,           // e.g., "NAME", "GEOID"
    value: String
) => (result: VotingDistrictResult)
```

### Convenience Workflows (census.tiger.Workflows)

```afl
// Resolve state name/abbreviation to FIPS code
facet StateFIPS(state: String) => (fips: String)

// Download and convert Congressional Districts
workflow GetCongressionalDistricts(year: Long = 2023, congress_number: Long = 118)
    => (result: VotingDistrictResult)

// Get all voting boundaries for a state
workflow GetStateVotingBoundaries(state_fips: String, year: Long = 2023)
    => (senate: VotingDistrictResult, house: VotingDistrictResult, precincts: VotingDistrictResult)
```

## State FIPS Codes

Common state FIPS codes:

| State | FIPS | State | FIPS | State | FIPS |
|-------|------|-------|------|-------|------|
| Alabama | 01 | Louisiana | 22 | Ohio | 39 |
| Alaska | 02 | Maine | 23 | Oklahoma | 40 |
| Arizona | 04 | Maryland | 24 | Oregon | 41 |
| Arkansas | 05 | Massachusetts | 25 | Pennsylvania | 42 |
| California | 06 | Michigan | 26 | Rhode Island | 44 |
| Colorado | 08 | Minnesota | 27 | South Carolina | 45 |
| Connecticut | 09 | Mississippi | 28 | South Dakota | 46 |
| Delaware | 10 | Missouri | 29 | Tennessee | 47 |
| DC | 11 | Montana | 30 | Texas | 48 |
| Florida | 12 | Nebraska | 31 | Utah | 49 |
| Georgia | 13 | Nevada | 32 | Vermont | 50 |
| Hawaii | 15 | New Hampshire | 33 | Virginia | 51 |
| Idaho | 16 | New Jersey | 34 | Washington | 53 |
| Illinois | 17 | New Mexico | 35 | West Virginia | 54 |
| Indiana | 18 | New York | 36 | Wisconsin | 55 |
| Iowa | 19 | North Carolina | 37 | Wyoming | 56 |
| Kansas | 20 | North Dakota | 38 | Puerto Rico | 72 |
| Kentucky | 21 | | | | |

The `StateFIPS` facet also accepts state names ("California") and abbreviations ("CA").

## Usage Examples

### Download Congressional Districts

```afl
workflow GetCurrentCongress() => (result: VotingDistrictResult) andThen {
    download = CongressionalDistricts(year = 2023, congress_number = 118)
    convert = ShapefileToGeoJSON(cache = download.cache)
    yield GetCurrentCongress(result = convert.result)
}
```

### Download California State Legislature Districts

```afl
workflow CaliforniaLegislature() => (senate: VotingDistrictResult, house: VotingDistrictResult) andThen {
    senate_dl = StateSenateDistricts(state_fips = "06", year = 2023)
    house_dl = StateHouseDistricts(state_fips = "06", year = 2023)

    senate_json = ShapefileToGeoJSON(cache = senate_dl.cache)
    house_json = ShapefileToGeoJSON(cache = house_dl.cache)

    yield CaliforniaLegislature(senate = senate_json.result, house = house_json.result)
}
```

### Download Texas Voting Precincts

```afl
workflow TexasPrecincts() => (result: VotingDistrictResult) andThen {
    download = VotingPrecincts(state_fips = "48", year = 2020)
    convert = ShapefileToGeoJSON(cache = download.cache)
    yield TexasPrecincts(result = convert.result)
}
```

### Filter Districts by Name

```afl
workflow FindDistrict(geojson_path: String, name: String) => (result: VotingDistrictResult) andThen {
    filtered = FilterDistricts(
        input_path = $.geojson_path,
        attribute = "NAME",
        value = $.name
    )
    yield FindDistrict(result = filtered.result)
}
```

## Shapefile Conversion

The `ShapefileToGeoJSON` facet converts TIGER shapefiles to GeoJSON format. It uses:

1. **ogr2ogr** (GDAL) - Preferred, if installed
2. **geopandas** - Python fallback

Both methods reproject to WGS84 (EPSG:4326) for compatibility with web mapping.

### Installing GDAL

```bash
# macOS
brew install gdal

# Ubuntu/Debian
sudo apt-get install gdal-bin

# With conda
conda install -c conda-forge gdal
```

### Installing geopandas (alternative)

```bash
pip install geopandas
```

## Integration with OSM Data

To join TIGER voting districts with OSM boundaries:

1. Download OSM administrative boundaries (using existing osm-geocoder facets)
2. Download TIGER voting districts
3. Perform spatial join (intersection) to find which OSM boundaries contain each district

Example workflow:

```afl
workflow VotingWithOSM(state_fips: String) => (result: VotingDistrictResult)
    with StateCache(state_fips = $.state_fips)
andThen {
    // Download OSM data for the state
    osm_boundaries = CountyBoundaries(cache = StateCache.cache)

    // Download voting precincts
    precincts_dl = VotingPrecincts(state_fips = $.state_fips, year = 2020)
    precincts = ShapefileToGeoJSON(cache = precincts_dl.cache)

    // Join (requires custom handler with spatial join logic)
    joined = JoinWithOSMBoundaries(
        districts = precincts.result,
        osm_boundaries_path = osm_boundaries.result.output_path
    )

    yield VotingWithOSM(result = joined.result)
}
```

## Data Schema

### TIGERCache

```afl
schema TIGERCache {
    url: String            // Download URL
    path: String           // Local file path
    date: String           // Download timestamp
    size: Long             // File size in bytes
    wasInCache: Boolean    // True if served from cache
    year: Long             // Data year
    district_type: String  // cd, sldu, sldl, vtd
    state_fips: String     // State FIPS or "US"
}
```

### VotingDistrictResult

```afl
schema VotingDistrictResult {
    output_path: String    // Path to GeoJSON file
    feature_count: Long    // Number of districts
    district_type: String  // Human-readable type name
    state: String          // State abbreviation
    year: Long             // Data year
    format: String         // Always "GeoJSON"
    extraction_date: String // Processing timestamp
}
```

## Running Tests

```bash
# From repo root
pytest examples/osm-geocoder/test_tiger.py -v
```

## Dependencies

- `requests>=2.28` - HTTP downloads
- `gdal` or `geopandas` - Shapefile to GeoJSON conversion
