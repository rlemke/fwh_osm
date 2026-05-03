#!/usr/bin/env python3
"""Example: Build driving routes between all Indian cities with 1.6-3M population.

Demonstrates a 5-step workflow that finds cities within a population range
and computes ALL pairwise driving routes between them (every city to every
other city):
  1. Resolve "India" to the Indian OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population between 1,600,000 and 3,000,000
  4. Build all-pairs driving routes between the 10 filtered cities
  5. Render an interactive Leaflet map of the route network

With 10 cities in range this produces C(10,2) = 45 pairwise routes spanning
the Indian subcontinent from Punjab to Andhra Pradesh.

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_india_cityroutes.py
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
        min_population: Long = 1600000,
        max_population: Long = 3000000,
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
# Mock data - Indian cities and all-pairs driving routes.
# City proper populations (2023 estimates).
# ---------------------------------------------------------------------------

INDIAN_CITIES = [
    {"name": "Mumbai", "state": "MH", "population": 12_442_000, "lat": 19.076, "lon": 72.878},
    {"name": "Delhi", "state": "DL", "population": 11_007_000, "lat": 28.614, "lon": 77.209},
    {"name": "Bangalore", "state": "KA", "population": 8_443_000, "lat": 12.972, "lon": 77.595},
    {"name": "Hyderabad", "state": "TS", "population": 6_810_000, "lat": 17.385, "lon": 78.487},
    {"name": "Ahmedabad", "state": "GJ", "population": 5_634_000, "lat": 23.023, "lon": 72.571},
    {"name": "Chennai", "state": "TN", "population": 4_646_000, "lat": 13.083, "lon": 80.271},
    {"name": "Kolkata", "state": "WB", "population": 4_497_000, "lat": 22.573, "lon": 88.364},
    {"name": "Surat", "state": "GJ", "population": 4_467_000, "lat": 21.170, "lon": 72.831},
    {"name": "Pune", "state": "MH", "population": 3_124_000, "lat": 18.520, "lon": 73.857},
    {"name": "Jaipur", "state": "RJ", "population": 3_046_000, "lat": 26.913, "lon": 75.787},
    {"name": "Lucknow", "state": "UP", "population": 2_815_000, "lat": 26.847, "lon": 80.947},
    {"name": "Kanpur", "state": "UP", "population": 2_768_000, "lat": 26.449, "lon": 80.349},
    {"name": "Nagpur", "state": "MH", "population": 2_405_000, "lat": 21.146, "lon": 79.089},
    {"name": "Indore", "state": "MP", "population": 1_994_000, "lat": 22.720, "lon": 75.858},
    {"name": "Thane", "state": "MH", "population": 1_841_000, "lat": 19.197, "lon": 72.964},
    {"name": "Bhopal", "state": "MP", "population": 1_798_000, "lat": 23.260, "lon": 77.413},
    {"name": "Visakhapatnam", "state": "AP", "population": 1_728_000, "lat": 17.687, "lon": 83.218},
    {"name": "Patna", "state": "BR", "population": 1_684_000, "lat": 25.611, "lon": 85.144},
    {"name": "Vadodara", "state": "GJ", "population": 1_670_000, "lat": 22.307, "lon": 73.192},
    {"name": "Ludhiana", "state": "PB", "population": 1_613_000, "lat": 30.901, "lon": 75.857},
    {"name": "Agra", "state": "UP", "population": 1_585_000, "lat": 27.177, "lon": 78.016},
    {"name": "Nashik", "state": "MH", "population": 1_487_000, "lat": 20.000, "lon": 73.790},
    {"name": "Varanasi", "state": "UP", "population": 1_432_000, "lat": 25.318, "lon": 82.992},
    {"name": "Ranchi", "state": "JH", "population": 1_120_000, "lat": 23.344, "lon": 85.310},
    {"name": "Coimbatore", "state": "TN", "population": 1_061_000, "lat": 11.017, "lon": 76.956},
]

MIN_POP = 1_600_000
MAX_POP = 3_000_000
RANGE_CITIES = [c for c in INDIAN_CITIES if MIN_POP <= c["population"] <= MAX_POP]

# All C(10,2) = 45 pairwise driving routes between the 10 cities in range.
# Distances are approximate highway driving distances in km.
# Indian National Highways average 60-80 km/h depending on road quality.
ROUTES = [
    # --- Lucknow routes (9) ---
    {
        "from": "Lucknow",
        "to": "Kanpur",
        "distance_km": 82,
        "duration_min": 80,
        "waypoints": 130,
        "via": "NH-2 Lucknow-Agra Expressway",
    },
    {
        "from": "Lucknow",
        "to": "Nagpur",
        "distance_km": 690,
        "duration_min": 630,
        "waypoints": 1093,
        "via": "NH-44 via Allahabad - Jabalpur",
    },
    {
        "from": "Lucknow",
        "to": "Indore",
        "distance_km": 680,
        "duration_min": 615,
        "waypoints": 1077,
        "via": "NH-44 via Jhansi - Lalitpur",
    },
    {
        "from": "Lucknow",
        "to": "Thane",
        "distance_km": 1390,
        "duration_min": 1200,
        "waypoints": 2202,
        "via": "NH-44 via Nagpur - Nashik",
    },
    {
        "from": "Lucknow",
        "to": "Bhopal",
        "distance_km": 600,
        "duration_min": 540,
        "waypoints": 950,
        "via": "NH-44 via Jhansi - Sagar",
    },
    {
        "from": "Lucknow",
        "to": "Visakhapatnam",
        "distance_km": 1330,
        "duration_min": 1155,
        "waypoints": 2107,
        "via": "NH-44/53 via Allahabad - Raipur",
    },
    {
        "from": "Lucknow",
        "to": "Patna",
        "distance_km": 535,
        "duration_min": 480,
        "waypoints": 847,
        "via": "NH-31 via Ayodhya - Gorakhpur",
    },
    {
        "from": "Lucknow",
        "to": "Vadodara",
        "distance_km": 1010,
        "duration_min": 870,
        "waypoints": 1600,
        "via": "NH-44/48 via Jhansi - Bhopal - Indore",
    },
    {
        "from": "Lucknow",
        "to": "Ludhiana",
        "distance_km": 680,
        "duration_min": 585,
        "waypoints": 1077,
        "via": "NH-44 via Delhi - Karnal - Ambala",
    },
    # --- Kanpur routes (8 remaining) ---
    {
        "from": "Kanpur",
        "to": "Nagpur",
        "distance_km": 640,
        "duration_min": 585,
        "waypoints": 1014,
        "via": "NH-44 via Sagar - Jabalpur",
    },
    {
        "from": "Kanpur",
        "to": "Indore",
        "distance_km": 620,
        "duration_min": 555,
        "waypoints": 982,
        "via": "NH via Jhansi - Lalitpur - Dewas",
    },
    {
        "from": "Kanpur",
        "to": "Thane",
        "distance_km": 1340,
        "duration_min": 1155,
        "waypoints": 2123,
        "via": "NH-44 via Nagpur - Nashik",
    },
    {
        "from": "Kanpur",
        "to": "Bhopal",
        "distance_km": 530,
        "duration_min": 480,
        "waypoints": 840,
        "via": "NH-44 via Jhansi - Sagar",
    },
    {
        "from": "Kanpur",
        "to": "Visakhapatnam",
        "distance_km": 1270,
        "duration_min": 1110,
        "waypoints": 2012,
        "via": "NH via Allahabad - Raipur - Rourkela",
    },
    {
        "from": "Kanpur",
        "to": "Patna",
        "distance_km": 575,
        "duration_min": 525,
        "waypoints": 911,
        "via": "NH-2 via Allahabad - Varanasi",
    },
    {
        "from": "Kanpur",
        "to": "Vadodara",
        "distance_km": 955,
        "duration_min": 825,
        "waypoints": 1513,
        "via": "NH via Jhansi - Bhopal - Indore",
    },
    {
        "from": "Kanpur",
        "to": "Ludhiana",
        "distance_km": 740,
        "duration_min": 645,
        "waypoints": 1172,
        "via": "NH-2 via Agra - Delhi - Ambala",
    },
    # --- Nagpur routes (7 remaining) ---
    {
        "from": "Nagpur",
        "to": "Indore",
        "distance_km": 470,
        "duration_min": 435,
        "waypoints": 745,
        "via": "NH-47 via Betul - Khandwa",
    },
    {
        "from": "Nagpur",
        "to": "Thane",
        "distance_km": 800,
        "duration_min": 690,
        "waypoints": 1267,
        "via": "NH-44 via Amravati - Nashik",
    },
    {
        "from": "Nagpur",
        "to": "Bhopal",
        "distance_km": 350,
        "duration_min": 315,
        "waypoints": 554,
        "via": "NH-44 via Seoni - Chhindwara",
    },
    {
        "from": "Nagpur",
        "to": "Visakhapatnam",
        "distance_km": 700,
        "duration_min": 630,
        "waypoints": 1109,
        "via": "NH-53 via Raipur - Rourkela - Bhubaneswar",
    },
    {
        "from": "Nagpur",
        "to": "Patna",
        "distance_km": 1010,
        "duration_min": 885,
        "waypoints": 1600,
        "via": "NH via Raipur - Ranchi - Gaya",
    },
    {
        "from": "Nagpur",
        "to": "Vadodara",
        "distance_km": 750,
        "duration_min": 660,
        "waypoints": 1188,
        "via": "NH-48 via Dhule - Surat",
    },
    {
        "from": "Nagpur",
        "to": "Ludhiana",
        "distance_km": 1350,
        "duration_min": 1170,
        "waypoints": 2139,
        "via": "NH-44 via Bhopal - Jhansi - Agra - Delhi",
    },
    # --- Indore routes (6 remaining) ---
    {
        "from": "Indore",
        "to": "Thane",
        "distance_km": 590,
        "duration_min": 510,
        "waypoints": 935,
        "via": "NH-48 via Dhule - Nashik",
    },
    {
        "from": "Indore",
        "to": "Bhopal",
        "distance_km": 195,
        "duration_min": 180,
        "waypoints": 309,
        "via": "NH-46 via Dewas",
    },
    {
        "from": "Indore",
        "to": "Visakhapatnam",
        "distance_km": 1150,
        "duration_min": 1005,
        "waypoints": 1822,
        "via": "NH via Bhopal - Nagpur - Raipur",
    },
    {
        "from": "Indore",
        "to": "Patna",
        "distance_km": 1180,
        "duration_min": 1020,
        "waypoints": 1869,
        "via": "NH via Bhopal - Jhansi - Allahabad",
    },
    {
        "from": "Indore",
        "to": "Vadodara",
        "distance_km": 310,
        "duration_min": 270,
        "waypoints": 491,
        "via": "NH-48 via Godhra",
    },
    {
        "from": "Indore",
        "to": "Ludhiana",
        "distance_km": 1050,
        "duration_min": 915,
        "waypoints": 1663,
        "via": "NH-48 via Udaipur - Jaipur - Delhi",
    },
    # --- Thane routes (5 remaining) ---
    {
        "from": "Thane",
        "to": "Bhopal",
        "distance_km": 780,
        "duration_min": 675,
        "waypoints": 1235,
        "via": "NH-48/44 via Nashik - Dhule",
    },
    {
        "from": "Thane",
        "to": "Visakhapatnam",
        "distance_km": 1100,
        "duration_min": 945,
        "waypoints": 1742,
        "via": "NH-44 via Pune - Hyderabad - Vijayawada",
    },
    {
        "from": "Thane",
        "to": "Patna",
        "distance_km": 1740,
        "duration_min": 1500,
        "waypoints": 2756,
        "via": "NH via Nashik - Nagpur - Jabalpur - Allahabad",
    },
    {
        "from": "Thane",
        "to": "Vadodara",
        "distance_km": 400,
        "duration_min": 345,
        "waypoints": 634,
        "via": "NH-48 via Surat",
    },
    {
        "from": "Thane",
        "to": "Ludhiana",
        "distance_km": 1560,
        "duration_min": 1350,
        "waypoints": 2471,
        "via": "NH-48 via Surat - Udaipur - Jaipur - Delhi",
    },
    # --- Bhopal routes (4 remaining) ---
    {
        "from": "Bhopal",
        "to": "Visakhapatnam",
        "distance_km": 1010,
        "duration_min": 885,
        "waypoints": 1600,
        "via": "NH via Nagpur - Raipur - Rourkela",
    },
    {
        "from": "Bhopal",
        "to": "Patna",
        "distance_km": 955,
        "duration_min": 825,
        "waypoints": 1513,
        "via": "NH-44 via Jhansi - Allahabad - Varanasi",
    },
    {
        "from": "Bhopal",
        "to": "Vadodara",
        "distance_km": 500,
        "duration_min": 435,
        "waypoints": 792,
        "via": "NH-46/48 via Indore - Godhra",
    },
    {
        "from": "Bhopal",
        "to": "Ludhiana",
        "distance_km": 1100,
        "duration_min": 945,
        "waypoints": 1742,
        "via": "NH-44 via Jhansi - Agra - Delhi - Ambala",
    },
    # --- Visakhapatnam routes (3 remaining) ---
    {
        "from": "Visakhapatnam",
        "to": "Patna",
        "distance_km": 1310,
        "duration_min": 1140,
        "waypoints": 2075,
        "via": "NH-53/33 via Raipur - Ranchi - Gaya",
    },
    {
        "from": "Visakhapatnam",
        "to": "Vadodara",
        "distance_km": 1380,
        "duration_min": 1200,
        "waypoints": 2186,
        "via": "NH via Hyderabad - Nagpur - Dhule",
    },
    {
        "from": "Visakhapatnam",
        "to": "Ludhiana",
        "distance_km": 1990,
        "duration_min": 1725,
        "waypoints": 3152,
        "via": "NH via Raipur - Nagpur - Bhopal - Delhi",
    },
    # --- Patna routes (2 remaining) ---
    {
        "from": "Patna",
        "to": "Vadodara",
        "distance_km": 1430,
        "duration_min": 1230,
        "waypoints": 2265,
        "via": "NH via Varanasi - Allahabad - Bhopal - Indore",
    },
    {
        "from": "Patna",
        "to": "Ludhiana",
        "distance_km": 1190,
        "duration_min": 1020,
        "waypoints": 1885,
        "via": "NH via Varanasi - Lucknow - Delhi",
    },
    # --- Vadodara routes (1 remaining) ---
    {
        "from": "Vadodara",
        "to": "Ludhiana",
        "distance_km": 1060,
        "duration_min": 915,
        "waypoints": 1679,
        "via": "NH-48 via Ahmedabad - Udaipur - Jaipur - Delhi",
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
            "url": "https://download.geofabrik.de/asia/india-latest.osm.pbf",
            "path": "/tmp/osm-cache/asia/india-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 1_234_567_890,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "India",
            "region_namespace": "osm.cache.Asia",
            "continent": "Asia",
            "geofabrik_path": "asia/india",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/india_cities.geojson",
            "feature_count": len(
                [c for c in INDIAN_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "original_count": 28_743,
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": INDIAN_CITIES[0]["population"],
            "filter_applied": f"population >= {p.get('min_population', 0):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulationRange": lambda p: {
        "result": {
            "output_path": "/tmp/india_cities_1600k_3000k.geojson",
            "feature_count": len(RANGE_CITIES),
            "original_count": len(
                [c for c in INDIAN_CITIES if c["population"] >= p.get("min_population", 0)]
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
            "output_path": "/tmp/india_city_routes.geojson",
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
            "output_path": "/tmp/india_city_routes_map.html",
            "format": "html",
            "feature_count": NUM_ROUTES + len(RANGE_CITIES),
            "bounds": "8.07,68.11,35.50,97.40",
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
    """Run the India city route map workflow end-to-end with mock handlers."""
    print("Compiling CityRouteMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow
    print(
        'Executing: CityRouteMapByRegion(region="India", '
        f'min_population={MIN_POP:,}, max_population={MAX_POP:,}, profile="car")'
    )
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulationRange -> BuildRoutesBetweenCities -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "India",
            "min_population": MIN_POP,
            "max_population": MAX_POP,
            "profile": "car",
            "title": f"Indian Cities {MIN_POP / 1e6:.1f}-{MAX_POP / 1e6:.0f}M: All Driving Routes",
            "color": "#e67e22",
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
    print(f"\n{'=' * 80}")
    print(
        f"RESULTS: Indian Cities {MIN_POP / 1e6:.1f}-{MAX_POP / 1e6:.0f}M - All-Pairs Driving Routes"
    )
    print(f"{'=' * 80}")
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
        bar_len = int(25 * city["population"] / max_pop)
        bar = "#" * bar_len
        print(f"    {city['name']:.<20} {city['population']:>10,}  {city['state']:<4} {bar}")

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

    # Distance matrix
    col_w = 11
    print("\n  Distance matrix (km):")
    # Abbreviate long names for matrix headers
    abbrev = {
        "Lucknow": "LKO",
        "Kanpur": "KNP",
        "Nagpur": "NGP",
        "Indore": "IDR",
        "Thane": "THN",
        "Bhopal": "BPL",
        "Visakhapatnam": "VZG",
        "Patna": "PAT",
        "Vadodara": "BRC",
        "Ludhiana": "LDH",
    }
    header = "    " + "".ljust(20) + "".join(abbrev[c].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (20 + col_w * len(city_names)))
    for row_city in city_names:
        cells = []
        for col_city in city_names:
            if row_city == col_city:
                cells.append("--".rjust(col_w))
            else:
                r = route_map.get((row_city, col_city))
                cells.append(f"{r['distance_km']:,}".rjust(col_w) if r else "?".rjust(col_w))
        print(f"    {row_city:.<20}" + "".join(cells))

    # Duration matrix
    print("\n  Duration matrix (hours):")
    header = "    " + "".ljust(20) + "".join(abbrev[c].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (20 + col_w * len(city_names)))
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
        print(f"    {row_city:.<20}" + "".join(cells))

    # Top 10 longest and shortest routes
    sorted_routes = sorted(ROUTES, key=lambda r: r["distance_km"], reverse=True)
    print("\n  Top 10 longest routes:")
    print(f"    {'Route':<34} {'Distance':>10} {'Duration':>10}  {'Via'}")
    print(f"    {'-' * 34} {'-' * 10} {'-' * 10}  {'-' * 40}")
    for route in sorted_routes[:10]:
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<34} {route['distance_km']:>7,} km  "
            f"{hours:>2}h {mins:02d}m   {route['via']}"
        )

    print("\n  Top 5 shortest routes:")
    print(f"    {'Route':<34} {'Distance':>10} {'Duration':>10}  {'Via'}")
    print(f"    {'-' * 34} {'-' * 10} {'-' * 10}  {'-' * 40}")
    for route in sorted_routes[-5:]:
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<34} {route['distance_km']:>7,} km  "
            f"{hours:>2}h {mins:02d}m   {route['via']}"
        )

    # Summary line
    print(f"\n    {'-' * 34} {'-' * 10} {'-' * 10}")
    total_h = TOTAL_DURATION // 60
    total_m = TOTAL_DURATION % 60
    print(f"    {'Total (45 routes)':<34} {TOTAL_DISTANCE:>7,} km  {total_h:>2}h {total_m:02d}m")
    print(
        f"    {'Average':<34} {AVG_DISTANCE:>7,} km  "
        f"{TOTAL_DURATION // NUM_ROUTES // 60:>2}h {TOTAL_DURATION // NUM_ROUTES % 60:02d}m"
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
            f"    {city_name:.<20} {len(city_routes)} routes, {total_km:>6,} km total  "
            f"(nearest: {nearest_city} {nearest['distance_km']:,} km, "
            f"farthest: {farthest_city} {farthest['distance_km']:,} km)"
        )

    # Geographic clusters
    state_groups = {}
    for c in RANGE_CITIES:
        state_groups.setdefault(c["state"], []).append(c)
    multi_city_states = {s: cs for s, cs in state_groups.items() if len(cs) > 1}

    state_names = {
        "UP": "Uttar Pradesh",
        "MH": "Maharashtra",
        "MP": "Madhya Pradesh",
        "GJ": "Gujarat",
        "AP": "Andhra Pradesh",
        "BR": "Bihar",
        "PB": "Punjab",
    }

    print("\n  Geographic clusters (states with 2+ cities in range):")
    for state, cities in sorted(multi_city_states.items(), key=lambda x: len(x[1]), reverse=True):
        names = ", ".join(c["name"] for c in cities)
        full_name = state_names.get(state, state)
        print(f"    {full_name} ({len(cities)} cities): {names}")
        # Find intra-state routes
        city_names_in_state = [c["name"] for c in cities]
        intra = [
            r for r in ROUTES if r["from"] in city_names_in_state and r["to"] in city_names_in_state
        ]
        if intra:
            shortest = min(intra, key=lambda r: r["distance_km"])
            print(
                f"      Shortest intra-state: {shortest['from']} -> {shortest['to']} "
                f"({shortest['distance_km']} km, "
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
    too_big = [c for c in INDIAN_CITIES if c["population"] > MAX_POP]
    too_small = [c for c in INDIAN_CITIES if c["population"] < MIN_POP]
    print("\n  Excluded cities:")
    for c in too_big:
        print(
            f"    {c['name']:.<20} {c['population']:>10,}  {c['state']:<4} (above {MAX_POP / 1e6:.0f}M)"
        )
    for c in sorted(too_small, key=lambda x: x["population"], reverse=True)[:5]:
        print(
            f"    {c['name']:.<20} {c['population']:>10,}  {c['state']:<4} (below {MIN_POP / 1e6:.1f}M)"
        )
    if len(too_small) > 5:
        print(f"    ... and {len(too_small) - 5} more cities below {MIN_POP / 1e6:.1f}M")

    assert result.success
    assert outputs["region_name"] == "India"
    assert outputs["city_count"] == len(RANGE_CITIES)
    assert outputs["route_count"] == NUM_ROUTES
    assert outputs["total_distance_km"] == TOTAL_DISTANCE
    assert outputs["avg_distance_km"] == AVG_DISTANCE
    assert outputs["map_path"] == "/tmp/india_city_routes_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
