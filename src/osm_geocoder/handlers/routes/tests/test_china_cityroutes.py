#!/usr/bin/env python3
"""Example: Build driving routes between all Chinese cities with 3-5M population.

Demonstrates a 5-step workflow that finds cities within a population range
and computes ALL pairwise driving routes between them (every city to every
other city):
  1. Resolve "China" to the Chinese OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population between 3,000,000 and 5,000,000
  4. Build all-pairs driving routes between the 12 filtered cities
  5. Render an interactive Leaflet map of the route network

With 12 cities in range this produces C(12,2) = 66 pairwise routes spanning
China from Changchun in the northeast to Nanning in the south.

The real data pipeline is defined in osmcityrouting.afl — a 9-step workflow
that chains ResolveRegion → Download → BuildGraph → ValidateGraph →
ExtractPlacesWithPopulation → FilterByPopulationRange → PopulationStatistics →
ComputePairwiseRoutes → RenderLayers using live event handlers.

This test uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_china_cityroutes.py
"""

from facetwork import emit_dict, parse
from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry

# ---------------------------------------------------------------------------
# Program AST - declares the event facets the runtime needs to recognise.
# ---------------------------------------------------------------------------


def _ef(name: str, params: list[dict], returns: list[dict]) -> dict:
    """Shorthand for an EventFacetDecl node."""
    return {"type": "EventFacetDecl", "name": name, "params": params, "returns": returns}


PROGRAM_AST = {
    "type": "Program",
    "declarations": [
        {
            "type": "Namespace",
            "name": "osm",
            "declarations": [
                {
                    "type": "Namespace",
                    "name": "geo",
                    "declarations": [
                        {
                            "type": "Namespace",
                            "name": "Region",
                            "declarations": [
                                _ef(
                                    "ResolveRegion",
                                    [
                                        {"name": "name", "type": "String"},
                                        {"name": "prefer_continent", "type": "String"},
                                    ],
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "resolution", "type": "RegionResolution"},
                                    ],
                                ),
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "Population",
                            "declarations": [
                                _ef(
                                    "ExtractPlacesWithPopulation",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "place_type", "type": "String"},
                                        {"name": "min_population", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "PopulationFilteredFeatures"}],
                                ),
                                _ef(
                                    "FilterByPopulationRange",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "min_population", "type": "Long"},
                                        {"name": "max_population", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "PopulationFilteredFeatures"}],
                                ),
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "Routing",
                            "declarations": [
                                _ef(
                                    "BuildRoutesBetweenCities",
                                    [
                                        {"name": "cities_path", "type": "String"},
                                        {"name": "profile", "type": "String"},
                                        {"name": "cache", "type": "OSMCache"},
                                    ],
                                    [{"name": "result", "type": "RoutingResult"}],
                                ),
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "Visualization",
                            "declarations": [
                                _ef(
                                    "RenderMap",
                                    [
                                        {"name": "geojson_path", "type": "String"},
                                        {"name": "title", "type": "String"},
                                        {"name": "format", "type": "String"},
                                        {"name": "width", "type": "Long"},
                                        {"name": "height", "type": "Long"},
                                        {"name": "color", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "MapResult"}],
                                ),
                            ],
                        },
                    ],
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Workflow FFL - a 5-step pipeline:
#   resolve -> extract cities -> filter by range -> build routes -> map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow CityRouteMapByRegion(
        region: String,
        min_population: Long = 3000000,
        max_population: Long = 5000000,
        profile: String = "car",
        prefer_continent: String = "",
        title: String = "City Route Map",
        color: String = "#27ae60"
    ) => (map_path: String, region_name: String, city_count: Long,
          route_count: Long, total_distance_km: Long,
          avg_distance_km: Long) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        cities = ExtractPlacesWithPopulation(
            cache = resolved.cache,
            place_type = "city",
            min_population = $.min_population
        )
        filtered = FilterByPopulationRange(
            input_path = cities.result.output_path,
            min_population = $.min_population,
            max_population = $.max_population
        )
        routes = BuildRoutesBetweenCities(
            cities_path = filtered.result.output_path,
            profile = $.profile,
            cache = resolved.cache
        )
        map = RenderMap(
            geojson_path = routes.result.output_path,
            title = $.title,
            color = $.color
        )
        yield CityRouteMapByRegion(
            map_path = map.result.output_path,
            region_name = resolved.resolution.matched_name,
            city_count = filtered.result.feature_count,
            route_count = routes.result.route_count,
            total_distance_km = routes.result.total_distance_km,
            avg_distance_km = routes.result.avg_distance_km
        )
    }
}
"""


def compile_workflow() -> dict:
    """Compile the workflow FFL to a runtime AST dict."""
    tree = parse(WORKFLOW_AFL)
    program = emit_dict(tree)
    for ns in program.get("namespaces", []):
        for wf in ns.get("workflows", []):
            if wf["name"] == "CityRouteMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock data - Chinese cities (city proper population) and all-pairs routes.
# China's expressway network (G-series) is the world's longest at 177,000 km.
# ---------------------------------------------------------------------------

CHINESE_CITIES = [
    {"name": "Shanghai", "province": "SH", "population": 24_870_000, "lat": 31.230, "lon": 121.474},
    {"name": "Beijing", "province": "BJ", "population": 21_540_000, "lat": 39.904, "lon": 116.407},
    {
        "name": "Chongqing",
        "province": "CQ",
        "population": 16_380_000,
        "lat": 29.563,
        "lon": 106.552,
    },
    {
        "name": "Guangzhou",
        "province": "GD",
        "population": 15_310_000,
        "lat": 23.130,
        "lon": 113.264,
    },
    {"name": "Shenzhen", "province": "GD", "population": 12_900_000, "lat": 22.543, "lon": 114.058},
    {"name": "Chengdu", "province": "SC", "population": 10_110_000, "lat": 30.573, "lon": 104.066},
    {"name": "Tianjin", "province": "TJ", "population": 8_700_000, "lat": 39.084, "lon": 117.201},
    {"name": "Wuhan", "province": "HB", "population": 8_600_000, "lat": 30.593, "lon": 114.306},
    {"name": "Dongguan", "province": "GD", "population": 8_340_000, "lat": 23.021, "lon": 113.752},
    {"name": "Hangzhou", "province": "ZJ", "population": 7_200_000, "lat": 30.274, "lon": 120.155},
    {"name": "Foshan", "province": "GD", "population": 7_200_000, "lat": 23.022, "lon": 113.122},
    {"name": "Nanjing", "province": "JS", "population": 6_900_000, "lat": 32.061, "lon": 118.763},
    {"name": "Shenyang", "province": "LN", "population": 6_300_000, "lat": 41.802, "lon": 123.430},
    {"name": "Xi'an", "province": "SN", "population": 6_200_000, "lat": 34.264, "lon": 108.943},
    {"name": "Zhengzhou", "province": "HA", "population": 5_700_000, "lat": 34.748, "lon": 113.625},
    {"name": "Changsha", "province": "HN", "population": 5_500_000, "lat": 28.228, "lon": 112.939},
    {"name": "Harbin", "province": "HL", "population": 5_310_000, "lat": 45.750, "lon": 126.650},
    {"name": "Kunming", "province": "YN", "population": 4_900_000, "lat": 25.038, "lon": 102.718},
    {"name": "Dalian", "province": "LN", "population": 4_500_000, "lat": 38.914, "lon": 121.615},
    {"name": "Jinan", "province": "SD", "population": 4_300_000, "lat": 36.651, "lon": 116.987},
    {"name": "Qingdao", "province": "SD", "population": 4_200_000, "lat": 36.067, "lon": 120.383},
    {"name": "Hefei", "province": "AH", "population": 4_000_000, "lat": 31.821, "lon": 117.227},
    {"name": "Fuzhou", "province": "FJ", "population": 3_900_000, "lat": 26.075, "lon": 119.306},
    {"name": "Nanning", "province": "GX", "population": 3_800_000, "lat": 22.817, "lon": 108.367},
    {"name": "Changchun", "province": "JL", "population": 3_700_000, "lat": 43.880, "lon": 125.323},
    {"name": "Wuxi", "province": "JS", "population": 3_600_000, "lat": 31.491, "lon": 120.312},
    {"name": "Nanchang", "province": "JX", "population": 3_500_000, "lat": 28.683, "lon": 115.858},
    {"name": "Guiyang", "province": "GZ", "population": 3_400_000, "lat": 26.647, "lon": 106.630},
    {"name": "Taiyuan", "province": "SX", "population": 3_200_000, "lat": 37.870, "lon": 112.551},
    {"name": "Lanzhou", "province": "GS", "population": 2_900_000, "lat": 36.061, "lon": 103.834},
    {"name": "Shantou", "province": "GD", "population": 2_900_000, "lat": 23.354, "lon": 116.682},
    {"name": "Xiamen", "province": "FJ", "population": 2_800_000, "lat": 24.480, "lon": 118.089},
    {"name": "Wenzhou", "province": "ZJ", "population": 2_700_000, "lat": 28.000, "lon": 120.672},
]

MIN_POP = 3_000_000
MAX_POP = 5_000_000
RANGE_CITIES = [c for c in CHINESE_CITIES if MIN_POP <= c["population"] <= MAX_POP]

# All C(12,2) = 66 pairwise driving routes between the 12 cities in range.
# Distances are approximate highway (G-expressway) driving distances in km.
# Chinese expressways average 90-100 km/h including service area stops.
ROUTES = [
    # --- Kunming routes (11) ---
    {
        "from": "Kunming",
        "to": "Dalian",
        "distance_km": 3400,
        "duration_min": 2100,
        "waypoints": 5382,
        "via": "G5/G65 via Chongqing - Xi'an - Beijing - Shenyang",
    },
    {
        "from": "Kunming",
        "to": "Jinan",
        "distance_km": 2600,
        "duration_min": 1620,
        "waypoints": 4117,
        "via": "G60/G56 via Guiyang - Changsha - Wuhan",
    },
    {
        "from": "Kunming",
        "to": "Qingdao",
        "distance_km": 2900,
        "duration_min": 1800,
        "waypoints": 4593,
        "via": "G56 via Guiyang - Changsha - Hefei",
    },
    {
        "from": "Kunming",
        "to": "Hefei",
        "distance_km": 2200,
        "duration_min": 1380,
        "waypoints": 3484,
        "via": "G56 via Guiyang - Changsha - Nanchang",
    },
    {
        "from": "Kunming",
        "to": "Fuzhou",
        "distance_km": 2300,
        "duration_min": 1440,
        "waypoints": 3642,
        "via": "G56/G70 via Guiyang - Changsha - Nanchang",
    },
    {
        "from": "Kunming",
        "to": "Nanning",
        "distance_km": 830,
        "duration_min": 540,
        "waypoints": 1314,
        "via": "G80 Guangkun Expressway",
    },
    {
        "from": "Kunming",
        "to": "Changchun",
        "distance_km": 3900,
        "duration_min": 2400,
        "waypoints": 6174,
        "via": "G5 via Chongqing - Xi'an - Beijing - Shenyang",
    },
    {
        "from": "Kunming",
        "to": "Wuxi",
        "distance_km": 2400,
        "duration_min": 1500,
        "waypoints": 3800,
        "via": "G60 via Guiyang - Changsha - Nanchang",
    },
    {
        "from": "Kunming",
        "to": "Nanchang",
        "distance_km": 1900,
        "duration_min": 1200,
        "waypoints": 3009,
        "via": "G60/G56 via Guiyang - Changsha",
    },
    {
        "from": "Kunming",
        "to": "Guiyang",
        "distance_km": 500,
        "duration_min": 300,
        "waypoints": 792,
        "via": "G60 Hukun Expressway",
    },
    {
        "from": "Kunming",
        "to": "Taiyuan",
        "distance_km": 2500,
        "duration_min": 1560,
        "waypoints": 3958,
        "via": "G5 via Chongqing - Xi'an",
    },
    # --- Dalian routes (10 remaining) ---
    {
        "from": "Dalian",
        "to": "Jinan",
        "distance_km": 970,
        "duration_min": 600,
        "waypoints": 1536,
        "via": "G15 via Yantai ferry or G1/G2 via Shenyang - Beijing",
    },
    {
        "from": "Dalian",
        "to": "Qingdao",
        "distance_km": 1200,
        "duration_min": 780,
        "waypoints": 1900,
        "via": "G15 Shenhai Expressway coastal route",
    },
    {
        "from": "Dalian",
        "to": "Hefei",
        "distance_km": 1550,
        "duration_min": 960,
        "waypoints": 2454,
        "via": "G1/G2/G3 via Shenyang - Beijing - Jinan",
    },
    {
        "from": "Dalian",
        "to": "Fuzhou",
        "distance_km": 2200,
        "duration_min": 1380,
        "waypoints": 3484,
        "via": "G1/G2 via Shenyang - Beijing - Hefei - Nanchang",
    },
    {
        "from": "Dalian",
        "to": "Nanning",
        "distance_km": 3200,
        "duration_min": 1980,
        "waypoints": 5067,
        "via": "G1/G4 via Shenyang - Beijing - Wuhan - Changsha",
    },
    {
        "from": "Dalian",
        "to": "Changchun",
        "distance_km": 700,
        "duration_min": 420,
        "waypoints": 1108,
        "via": "G1 Jingha Expressway via Shenyang",
    },
    {
        "from": "Dalian",
        "to": "Wuxi",
        "distance_km": 1700,
        "duration_min": 1080,
        "waypoints": 2691,
        "via": "G1/G2 via Shenyang - Beijing - Jinan",
    },
    {
        "from": "Dalian",
        "to": "Nanchang",
        "distance_km": 2000,
        "duration_min": 1260,
        "waypoints": 3167,
        "via": "G1/G2/G35 via Shenyang - Beijing - Hefei",
    },
    {
        "from": "Dalian",
        "to": "Guiyang",
        "distance_km": 3100,
        "duration_min": 1920,
        "waypoints": 4908,
        "via": "G1/G4 via Shenyang - Beijing - Wuhan - Changsha",
    },
    {
        "from": "Dalian",
        "to": "Taiyuan",
        "distance_km": 1200,
        "duration_min": 720,
        "waypoints": 1900,
        "via": "G1/G5 via Shenyang - Beijing",
    },
    # --- Jinan routes (9 remaining) ---
    {
        "from": "Jinan",
        "to": "Qingdao",
        "distance_km": 370,
        "duration_min": 240,
        "waypoints": 586,
        "via": "G20 Qingyin Expressway",
    },
    {
        "from": "Jinan",
        "to": "Hefei",
        "distance_km": 600,
        "duration_min": 360,
        "waypoints": 950,
        "via": "G3 Jingtai Expressway",
    },
    {
        "from": "Jinan",
        "to": "Fuzhou",
        "distance_km": 1300,
        "duration_min": 840,
        "waypoints": 2059,
        "via": "G3/G70 via Hefei - Nanchang",
    },
    {
        "from": "Jinan",
        "to": "Nanning",
        "distance_km": 2200,
        "duration_min": 1380,
        "waypoints": 3484,
        "via": "G35/G4 via Hefei - Changsha - Guiyang",
    },
    {
        "from": "Jinan",
        "to": "Changchun",
        "distance_km": 1500,
        "duration_min": 900,
        "waypoints": 2375,
        "via": "G2/G1 via Beijing - Shenyang",
    },
    {
        "from": "Jinan",
        "to": "Wuxi",
        "distance_km": 700,
        "duration_min": 420,
        "waypoints": 1108,
        "via": "G2 Jinghu Expressway",
    },
    {
        "from": "Jinan",
        "to": "Nanchang",
        "distance_km": 1000,
        "duration_min": 600,
        "waypoints": 1583,
        "via": "G35 Jiguang Expressway via Hefei",
    },
    {
        "from": "Jinan",
        "to": "Guiyang",
        "distance_km": 1900,
        "duration_min": 1200,
        "waypoints": 3009,
        "via": "G35/G60 via Hefei - Changsha",
    },
    {
        "from": "Jinan",
        "to": "Taiyuan",
        "distance_km": 530,
        "duration_min": 330,
        "waypoints": 839,
        "via": "G20/G5 via Shijiazhuang",
    },
    # --- Qingdao routes (8 remaining) ---
    {
        "from": "Qingdao",
        "to": "Hefei",
        "distance_km": 850,
        "duration_min": 540,
        "waypoints": 1346,
        "via": "G15/G3 via Lianyungang",
    },
    {
        "from": "Qingdao",
        "to": "Fuzhou",
        "distance_km": 1400,
        "duration_min": 900,
        "waypoints": 2217,
        "via": "G15 Shenhai Expressway coastal route",
    },
    {
        "from": "Qingdao",
        "to": "Nanning",
        "distance_km": 2500,
        "duration_min": 1560,
        "waypoints": 3958,
        "via": "G15/G4 via Hefei - Changsha - Guiyang",
    },
    {
        "from": "Qingdao",
        "to": "Changchun",
        "distance_km": 1700,
        "duration_min": 1020,
        "waypoints": 2691,
        "via": "G20/G2/G1 via Jinan - Beijing - Shenyang",
    },
    {
        "from": "Qingdao",
        "to": "Wuxi",
        "distance_km": 900,
        "duration_min": 540,
        "waypoints": 1425,
        "via": "G15 via Lianyungang - Yancheng",
    },
    {
        "from": "Qingdao",
        "to": "Nanchang",
        "distance_km": 1300,
        "duration_min": 780,
        "waypoints": 2059,
        "via": "G15/G35 via Hefei",
    },
    {
        "from": "Qingdao",
        "to": "Guiyang",
        "distance_km": 2200,
        "duration_min": 1380,
        "waypoints": 3484,
        "via": "G20/G35/G60 via Hefei - Changsha",
    },
    {
        "from": "Qingdao",
        "to": "Taiyuan",
        "distance_km": 900,
        "duration_min": 540,
        "waypoints": 1425,
        "via": "G20/G5 via Jinan - Shijiazhuang",
    },
    # --- Hefei routes (7 remaining) ---
    {
        "from": "Hefei",
        "to": "Fuzhou",
        "distance_km": 800,
        "duration_min": 480,
        "waypoints": 1267,
        "via": "G3/G70 via Nanchang",
    },
    {
        "from": "Hefei",
        "to": "Nanning",
        "distance_km": 1600,
        "duration_min": 1020,
        "waypoints": 2533,
        "via": "G35/G4 via Changsha - Guiyang",
    },
    {
        "from": "Hefei",
        "to": "Changchun",
        "distance_km": 2050,
        "duration_min": 1260,
        "waypoints": 3246,
        "via": "G3/G2/G1 via Jinan - Beijing - Shenyang",
    },
    {
        "from": "Hefei",
        "to": "Wuxi",
        "distance_km": 290,
        "duration_min": 180,
        "waypoints": 459,
        "via": "G40 Hushan Expressway",
    },
    {
        "from": "Hefei",
        "to": "Nanchang",
        "distance_km": 460,
        "duration_min": 300,
        "waypoints": 728,
        "via": "G35 Jiguang Expressway",
    },
    {
        "from": "Hefei",
        "to": "Guiyang",
        "distance_km": 1500,
        "duration_min": 900,
        "waypoints": 2375,
        "via": "G50/G56 via Changsha",
    },
    {
        "from": "Hefei",
        "to": "Taiyuan",
        "distance_km": 1050,
        "duration_min": 660,
        "waypoints": 1663,
        "via": "G3/G5 via Jinan - Shijiazhuang",
    },
    # --- Fuzhou routes (6 remaining) ---
    {
        "from": "Fuzhou",
        "to": "Nanning",
        "distance_km": 1400,
        "duration_min": 900,
        "waypoints": 2217,
        "via": "G72/G80 via Guangzhou",
    },
    {
        "from": "Fuzhou",
        "to": "Changchun",
        "distance_km": 2800,
        "duration_min": 1740,
        "waypoints": 4433,
        "via": "G70/G3/G2/G1 via Nanchang - Hefei - Jinan - Beijing",
    },
    {
        "from": "Fuzhou",
        "to": "Wuxi",
        "distance_km": 900,
        "duration_min": 540,
        "waypoints": 1425,
        "via": "G15 Shenhai Expressway via Wenzhou",
    },
    {
        "from": "Fuzhou",
        "to": "Nanchang",
        "distance_km": 550,
        "duration_min": 360,
        "waypoints": 871,
        "via": "G70 Fuyin Expressway",
    },
    {
        "from": "Fuzhou",
        "to": "Guiyang",
        "distance_km": 1700,
        "duration_min": 1080,
        "waypoints": 2691,
        "via": "G70/G56 via Nanchang - Changsha",
    },
    {
        "from": "Fuzhou",
        "to": "Taiyuan",
        "distance_km": 1800,
        "duration_min": 1140,
        "waypoints": 2850,
        "via": "G70/G3/G5 via Nanchang - Hefei - Jinan",
    },
    # --- Nanning routes (5 remaining) ---
    {
        "from": "Nanning",
        "to": "Changchun",
        "distance_km": 3700,
        "duration_min": 2280,
        "waypoints": 5858,
        "via": "G75/G5 via Guiyang - Chongqing - Xi'an - Beijing - Shenyang",
    },
    {
        "from": "Nanning",
        "to": "Wuxi",
        "distance_km": 2000,
        "duration_min": 1260,
        "waypoints": 3167,
        "via": "G75/G56 via Guiyang - Changsha - Nanchang",
    },
    {
        "from": "Nanning",
        "to": "Nanchang",
        "distance_km": 1400,
        "duration_min": 900,
        "waypoints": 2217,
        "via": "G72/G4 via Guangzhou - Changsha",
    },
    {
        "from": "Nanning",
        "to": "Guiyang",
        "distance_km": 490,
        "duration_min": 300,
        "waypoints": 776,
        "via": "G75 Lanhai Expressway",
    },
    {
        "from": "Nanning",
        "to": "Taiyuan",
        "distance_km": 2600,
        "duration_min": 1620,
        "waypoints": 4117,
        "via": "G75/G5 via Guiyang - Chongqing - Xi'an",
    },
    # --- Changchun routes (4 remaining) ---
    {
        "from": "Changchun",
        "to": "Wuxi",
        "distance_km": 2100,
        "duration_min": 1320,
        "waypoints": 3325,
        "via": "G1/G2 via Shenyang - Beijing - Jinan",
    },
    {
        "from": "Changchun",
        "to": "Nanchang",
        "distance_km": 2500,
        "duration_min": 1560,
        "waypoints": 3958,
        "via": "G1/G2/G35 via Shenyang - Beijing - Hefei",
    },
    {
        "from": "Changchun",
        "to": "Guiyang",
        "distance_km": 3500,
        "duration_min": 2160,
        "waypoints": 5542,
        "via": "G1/G4 via Shenyang - Beijing - Wuhan - Changsha",
    },
    {
        "from": "Changchun",
        "to": "Taiyuan",
        "distance_km": 1700,
        "duration_min": 1020,
        "waypoints": 2691,
        "via": "G1/G5 via Shenyang - Beijing",
    },
    # --- Wuxi routes (3 remaining) ---
    {
        "from": "Wuxi",
        "to": "Nanchang",
        "distance_km": 620,
        "duration_min": 360,
        "waypoints": 982,
        "via": "G56 Hangrui Expressway",
    },
    {
        "from": "Wuxi",
        "to": "Guiyang",
        "distance_km": 1700,
        "duration_min": 1080,
        "waypoints": 2691,
        "via": "G56/G60 via Nanchang - Changsha",
    },
    {
        "from": "Wuxi",
        "to": "Taiyuan",
        "distance_km": 1150,
        "duration_min": 720,
        "waypoints": 1821,
        "via": "G2/G5 via Jinan - Shijiazhuang",
    },
    # --- Nanchang routes (2 remaining) ---
    {
        "from": "Nanchang",
        "to": "Guiyang",
        "distance_km": 1200,
        "duration_min": 720,
        "waypoints": 1900,
        "via": "G56/G60 via Changsha",
    },
    {
        "from": "Nanchang",
        "to": "Taiyuan",
        "distance_km": 1400,
        "duration_min": 840,
        "waypoints": 2217,
        "via": "G35/G5 via Hefei - Jinan",
    },
    # --- Guiyang routes (1 remaining) ---
    {
        "from": "Guiyang",
        "to": "Taiyuan",
        "distance_km": 2000,
        "duration_min": 1260,
        "waypoints": 3167,
        "via": "G60/G5 via Chongqing - Xi'an",
    },
]

TOTAL_DISTANCE = sum(r["distance_km"] for r in ROUTES)
TOTAL_DURATION = sum(r["duration_min"] for r in ROUTES)
AVG_DISTANCE = TOTAL_DISTANCE // len(ROUTES)
NUM_ROUTES = len(ROUTES)
assert NUM_ROUTES == len(RANGE_CITIES) * (len(RANGE_CITIES) - 1) // 2, (
    f"Expected C({len(RANGE_CITIES)},2)={len(RANGE_CITIES) * (len(RANGE_CITIES) - 1) // 2}, got {NUM_ROUTES}"
)


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/asia/china-latest.osm.pbf",
            "path": "/tmp/osm-cache/asia/china-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 5_678_901_234,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "China",
            "region_namespace": "osm.cache.Asia",
            "continent": "Asia",
            "geofabrik_path": "asia/china",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/china_cities.geojson",
            "feature_count": len(
                [c for c in CHINESE_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "original_count": 45_218,
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": CHINESE_CITIES[0]["population"],
            "filter_applied": f"population >= {p.get('min_population', 0):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulationRange": lambda p: {
        "result": {
            "output_path": "/tmp/china_cities_3m_5m.geojson",
            "feature_count": len(RANGE_CITIES),
            "original_count": len(
                [c for c in CHINESE_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": p.get("max_population", 0),
            "filter_applied": (
                f"{p.get('min_population', 0):,} <= population <= {p.get('max_population', 0):,}"
            ),
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "BuildRoutesBetweenCities": lambda p: {
        "result": {
            "output_path": "/tmp/china_city_routes.geojson",
            "route_count": NUM_ROUTES,
            "city_count": len(RANGE_CITIES),
            "total_distance_km": TOTAL_DISTANCE,
            "total_duration_min": TOTAL_DURATION,
            "avg_distance_km": AVG_DISTANCE,
            "avg_duration_min": TOTAL_DURATION // NUM_ROUTES,
            "profile": p.get("profile", "car"),
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/china_city_routes_map.html",
            "format": "html",
            "feature_count": NUM_ROUTES + len(RANGE_CITIES),
            "bounds": "18.16,73.50,53.56,135.09",
            "title": p.get("title", "Map"),
            "extraction_date": "2026-02-06T12:00:04+00:00",
        },
    },
}


def find_event_blocked_step(store: MemoryStore, workflow_id: str) -> tuple[str, str] | None:
    """Find the step that is blocked waiting for an event handler.

    Returns (step_id, short_facet_name) or None.
    """
    for step in store._steps.values():
        if step.workflow_id == workflow_id and step.state == "state.EventTransmit":
            short = (
                step.facet_name.rsplit(".", 1)[-1] if "." in step.facet_name else step.facet_name
            )
            return step.id, short
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the China city route map workflow end-to-end with mock handlers."""
    print("Compiling CityRouteMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow
    print(
        'Executing: CityRouteMapByRegion(region="China", '
        f'min_population={MIN_POP:,}, max_population={MAX_POP:,}, profile="car")'
    )
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulationRange -> BuildRoutesBetweenCities -> RenderMap")
    print("\n  Real pipeline: osmcityrouting.afl (9-step workflow with Download + BuildGraph)\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "China",
            "min_population": MIN_POP,
            "max_population": MAX_POP,
            "profile": "car",
            "title": f"Chinese Cities {MIN_POP // 1_000_000}-{MAX_POP // 1_000_000}M: All Driving Routes",
            "color": "#e74c3c",
        },
        program_ast=PROGRAM_AST,
    )
    assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

    # 2. Process event steps one at a time
    step_num = 0
    while True:
        blocked = find_event_blocked_step(store, result.workflow_id)
        if blocked is None:
            break

        step_id, facet_short = blocked
        step_num += 1
        handler = MOCK_HANDLERS.get(facet_short)
        assert handler, f"No mock handler for '{facet_short}'"

        step = store.get_step(step_id)
        params = {k: v.value for k, v in step.attributes.params.items()}
        print(f"  Step {step_num}: {facet_short}")

        handler_result = handler(params)
        evaluator.continue_step(step_id, handler_result)
        result = evaluator.resume(result.workflow_id, workflow_ast, PROGRAM_AST)

    assert result.status == ExecutionStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"

    # 3. Show results
    outputs = result.outputs
    n = outputs.get("city_count")
    print(f"\n{'=' * 85}")
    print(
        f"RESULTS: Chinese Cities {MIN_POP // 1_000_000}-{MAX_POP // 1_000_000}M - All-Pairs Driving Routes"
    )
    print(f"{'=' * 85}")
    print(f"  Region:                 {outputs.get('region_name')}")
    print(f"  Cities in range:        {n}")
    print(f"  All-pairs routes:       {outputs.get('route_count')}  (C({n},2) = {n}*{n - 1}/2)")
    print(f"  Total driving distance: {outputs.get('total_distance_km'):,} km")
    print(f"  Average route length:   {outputs.get('avg_distance_km'):,} km")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show the cities in range
    print(f"\n  Cities with population between {MIN_POP:,} and {MAX_POP:,}:")
    max_pop = RANGE_CITIES[0]["population"]
    for city in sorted(RANGE_CITIES, key=lambda c: c["population"], reverse=True):
        bar_len = int(22 * city["population"] / max_pop)
        bar = "#" * bar_len
        print(f"    {city['name']:.<14} {city['population']:>10,}  {city['province']:<4} {bar}")

    # Build symmetric route lookup
    city_names = [
        c["name"] for c in sorted(RANGE_CITIES, key=lambda c: c["population"], reverse=True)
    ]
    route_map = {}
    for r in ROUTES:
        route_map[(r["from"], r["to"])] = r
        route_map[(r["to"], r["from"])] = {
            "distance_km": r["distance_km"],
            "duration_min": r["duration_min"],
            "via": r["via"],
            "waypoints": r["waypoints"],
            "from": r["to"],
            "to": r["from"],
        }

    # Distance matrix with abbreviated headers
    abbrev = {
        "Kunming": "KMG",
        "Dalian": "DLC",
        "Jinan": "TNA",
        "Qingdao": "TAO",
        "Hefei": "HFE",
        "Fuzhou": "FOC",
        "Nanning": "NNG",
        "Changchun": "CGQ",
        "Wuxi": "WUX",
        "Nanchang": "KHN",
        "Guiyang": "KWE",
        "Taiyuan": "TYN",
    }
    col_w = 9
    print("\n  Distance matrix (km):")
    header = "    " + "".ljust(14) + "".join(abbrev[c].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (14 + col_w * len(city_names)))
    for row_city in city_names:
        cells = []
        for col_city in city_names:
            if row_city == col_city:
                cells.append("--".rjust(col_w))
            else:
                r = route_map.get((row_city, col_city))
                cells.append(f"{r['distance_km']:,}".rjust(col_w) if r else "?".rjust(col_w))
        print(f"    {row_city:.<14}" + "".join(cells))

    # Duration matrix
    print("\n  Duration matrix (hours):")
    header = "    " + "".ljust(14) + "".join(abbrev[c].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (14 + col_w * len(city_names)))
    for row_city in city_names:
        cells = []
        for col_city in city_names:
            if row_city == col_city:
                cells.append("--".rjust(col_w))
            else:
                r = route_map.get((row_city, col_city))
                if r:
                    h = r["duration_min"] // 60
                    m = r["duration_min"] % 60
                    cells.append(f"{h}h{m:02d}".rjust(col_w))
                else:
                    cells.append("?".rjust(col_w))
        print(f"    {row_city:.<14}" + "".join(cells))

    # Top 10 longest and shortest routes
    sorted_routes = sorted(ROUTES, key=lambda r: r["distance_km"], reverse=True)
    print("\n  Top 10 longest routes:")
    print(f"    {'Route':<30} {'Distance':>10} {'Duration':>10}  {'Via'}")
    print(f"    {'-' * 30} {'-' * 10} {'-' * 10}  {'-' * 45}")
    for route in sorted_routes[:10]:
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<30} {route['distance_km']:>7,} km  "
            f"{hours:>2}h {mins:02d}m   {route['via']}"
        )

    print("\n  Top 5 shortest routes:")
    print(f"    {'Route':<30} {'Distance':>10} {'Duration':>10}  {'Via'}")
    print(f"    {'-' * 30} {'-' * 10} {'-' * 10}  {'-' * 45}")
    for route in sorted_routes[-5:]:
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<30} {route['distance_km']:>7,} km  "
            f"{hours:>2}h {mins:02d}m   {route['via']}"
        )

    # Summary
    print(f"\n    {'-' * 30} {'-' * 10} {'-' * 10}")
    total_h = TOTAL_DURATION // 60
    total_m = TOTAL_DURATION % 60
    print(f"    {'Total (66 routes)':<30} {TOTAL_DISTANCE:>7,} km  {total_h:>3}h {total_m:02d}m")
    print(
        f"    {'Average':<30} {AVG_DISTANCE:>7,} km  "
        f"{TOTAL_DURATION // NUM_ROUTES // 60:>3}h {TOTAL_DURATION // NUM_ROUTES % 60:02d}m"
    )

    # Per-city connectivity summary
    print("\n  Per-city connectivity:")
    for city_name in city_names:
        city_routes = [r for r in ROUTES if r["from"] == city_name or r["to"] == city_name]
        total_km = sum(r["distance_km"] for r in city_routes)
        nearest = min(city_routes, key=lambda r: r["distance_km"])
        farthest = max(city_routes, key=lambda r: r["distance_km"])
        nearest_city = nearest["to"] if nearest["from"] == city_name else nearest["from"]
        farthest_city = farthest["to"] if farthest["from"] == city_name else farthest["from"]
        print(
            f"    {city_name:.<14} {len(city_routes):>2} routes, {total_km:>6,} km total  "
            f"(nearest: {nearest_city} {nearest['distance_km']:,} km, "
            f"farthest: {farthest_city} {farthest['distance_km']:,} km)"
        )

    # Geographic clusters
    province_groups = {}
    for c in RANGE_CITIES:
        province_groups.setdefault(c["province"], []).append(c)
    multi_city_provinces = {p: cs for p, cs in province_groups.items() if len(cs) > 1}

    province_names = {
        "YN": "Yunnan",
        "LN": "Liaoning",
        "SD": "Shandong",
        "AH": "Anhui",
        "FJ": "Fujian",
        "GX": "Guangxi",
        "JL": "Jilin",
        "JS": "Jiangsu",
        "JX": "Jiangxi",
        "GZ": "Guizhou",
        "SX": "Shanxi",
    }

    if multi_city_provinces:
        print("\n  Geographic clusters (provinces with 2+ cities in range):")
        for prov, cities in sorted(
            multi_city_provinces.items(), key=lambda x: len(x[1]), reverse=True
        ):
            names = ", ".join(c["name"] for c in cities)
            full_name = province_names.get(prov, prov)
            print(f"    {full_name} ({len(cities)} cities): {names}")
            city_names_in_prov = [c["name"] for c in cities]
            intra = [
                r
                for r in ROUTES
                if r["from"] in city_names_in_prov and r["to"] in city_names_in_prov
            ]
            if intra:
                shortest = min(intra, key=lambda r: r["distance_km"])
                print(
                    f"      Shortest intra-province: {shortest['from']} -> {shortest['to']} "
                    f"({shortest['distance_km']:,} km, "
                    f"{shortest['duration_min'] // 60}h {shortest['duration_min'] % 60:02d}m)"
                )

    # Cross-country extremes
    longest = max(ROUTES, key=lambda r: r["distance_km"])
    shortest = min(ROUTES, key=lambda r: r["distance_km"])
    print("\n  Extremes:")
    print(
        f"    Longest route:  {longest['from']} -> {longest['to']} "
        f"({longest['distance_km']:,} km, "
        f"{longest['duration_min'] // 60}h {longest['duration_min'] % 60:02d}m)"
    )
    print(
        f"    Shortest route: {shortest['from']} -> {shortest['to']} "
        f"({shortest['distance_km']:,} km, "
        f"{shortest['duration_min'] // 60}h {shortest['duration_min'] % 60:02d}m)"
    )
    print(
        f"    Ratio:          {longest['distance_km'] / shortest['distance_km']:.1f}x distance, "
        f"{longest['duration_min'] / shortest['duration_min']:.1f}x time"
    )

    # Show excluded cities
    too_big = [c for c in CHINESE_CITIES if c["population"] > MAX_POP]
    too_small = [c for c in CHINESE_CITIES if c["population"] < MIN_POP]
    print("\n  Excluded cities:")
    for c in too_big:
        print(
            f"    {c['name']:.<14} {c['population']:>10,}  {c['province']:<4} (above {MAX_POP // 1_000_000}M)"
        )
    for c in sorted(too_small, key=lambda x: x["population"], reverse=True)[:5]:
        print(
            f"    {c['name']:.<14} {c['population']:>10,}  {c['province']:<4} (below {MIN_POP // 1_000_000}M)"
        )
    if len(too_small) > 5:
        print(f"    ... and {len(too_small) - 5} more cities below {MIN_POP // 1_000_000}M")

    assert result.success
    assert outputs["region_name"] == "China"
    assert outputs["city_count"] == len(RANGE_CITIES)
    assert outputs["route_count"] == NUM_ROUTES
    assert outputs["total_distance_km"] == TOTAL_DISTANCE
    assert outputs["avg_distance_km"] == AVG_DISTANCE
    assert outputs["map_path"] == "/tmp/china_city_routes_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
