#!/usr/bin/env python3
"""Example: Build driving routes between all US cities with 1-2M population.

Demonstrates a 5-step workflow that finds cities within a population range
and computes ALL pairwise driving routes between them (every city to every
other city):
  1. Resolve "United States" to the US OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population between 1,000,000 and 2,000,000
  4. Build all-pairs driving routes between the 7 filtered cities
  5. Render an interactive Leaflet map of the route network

With 7 cities in range this produces C(7,2) = 21 pairwise routes spanning
the entire continental US.

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_usa_cityroutes.py
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
        min_population: Long = 1000000,
        max_population: Long = 2000000,
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
# Mock data - US cities (city proper population) and all-pairs driving routes.
# ---------------------------------------------------------------------------

US_CITIES = [
    {"name": "New York", "state": "NY", "population": 8_336_000, "lat": 40.713, "lon": -74.006},
    {"name": "Los Angeles", "state": "CA", "population": 3_898_000, "lat": 34.052, "lon": -118.244},
    {"name": "Chicago", "state": "IL", "population": 2_696_000, "lat": 41.878, "lon": -87.630},
    {"name": "Houston", "state": "TX", "population": 2_304_000, "lat": 29.760, "lon": -95.370},
    {"name": "Phoenix", "state": "AZ", "population": 1_650_000, "lat": 33.449, "lon": -112.074},
    {"name": "Philadelphia", "state": "PA", "population": 1_550_000, "lat": 39.953, "lon": -75.164},
    {"name": "San Antonio", "state": "TX", "population": 1_495_000, "lat": 29.425, "lon": -98.495},
    {"name": "San Diego", "state": "CA", "population": 1_387_000, "lat": 32.716, "lon": -117.161},
    {"name": "Dallas", "state": "TX", "population": 1_300_000, "lat": 32.777, "lon": -96.797},
    {"name": "Austin", "state": "TX", "population": 1_018_000, "lat": 30.267, "lon": -97.743},
    {"name": "San Jose", "state": "CA", "population": 1_014_000, "lat": 37.339, "lon": -121.895},
    {"name": "Jacksonville", "state": "FL", "population": 985_000, "lat": 30.332, "lon": -81.656},
    {"name": "Fort Worth", "state": "TX", "population": 958_000, "lat": 32.756, "lon": -97.331},
    {"name": "Columbus", "state": "OH", "population": 906_000, "lat": 39.961, "lon": -82.999},
    {"name": "Charlotte", "state": "NC", "population": 911_000, "lat": 35.227, "lon": -80.843},
    {"name": "Indianapolis", "state": "IN", "population": 887_000, "lat": 39.768, "lon": -86.158},
]

MIN_POP = 1_000_000
MAX_POP = 2_000_000
RANGE_CITIES = [c for c in US_CITIES if MIN_POP <= c["population"] <= MAX_POP]

# All C(7,2) = 21 pairwise driving routes between the 7 cities in range.
# Distances are approximate highway driving distances in km.
ROUTES = [
    # Phoenix routes (6)
    {
        "from": "Phoenix",
        "to": "Philadelphia",
        "distance_km": 3860,
        "duration_min": 2040,
        "waypoints": 6112,
        "via": "I-10 - I-20 - I-85 - I-81 via El Paso",
    },
    {
        "from": "Phoenix",
        "to": "San Antonio",
        "distance_km": 1575,
        "duration_min": 840,
        "waypoints": 2494,
        "via": "I-10 via Tucson - Las Cruces - El Paso",
    },
    {
        "from": "Phoenix",
        "to": "San Diego",
        "distance_km": 570,
        "duration_min": 315,
        "waypoints": 902,
        "via": "I-8 via Gila Bend - Yuma",
    },
    {
        "from": "Phoenix",
        "to": "Dallas",
        "distance_km": 1710,
        "duration_min": 915,
        "waypoints": 2708,
        "via": "I-10 - I-20 via El Paso - Midland",
    },
    {
        "from": "Phoenix",
        "to": "Austin",
        "distance_km": 1400,
        "duration_min": 750,
        "waypoints": 2217,
        "via": "I-10 via Tucson - El Paso - San Antonio",
    },
    {
        "from": "Phoenix",
        "to": "San Jose",
        "distance_km": 1060,
        "duration_min": 570,
        "waypoints": 1678,
        "via": "I-10 - I-5 via Los Angeles - Bakersfield",
    },
    # Philadelphia routes (5 remaining)
    {
        "from": "Philadelphia",
        "to": "San Antonio",
        "distance_km": 2830,
        "duration_min": 1500,
        "waypoints": 4481,
        "via": "I-81 - I-40 - I-30 - I-35 via Knoxville",
    },
    {
        "from": "Philadelphia",
        "to": "San Diego",
        "distance_km": 4390,
        "duration_min": 2340,
        "waypoints": 6951,
        "via": "I-76 - I-70 - I-15 via Denver - Las Vegas",
    },
    {
        "from": "Philadelphia",
        "to": "Dallas",
        "distance_km": 2490,
        "duration_min": 1320,
        "waypoints": 3943,
        "via": "I-81 - I-40 - I-30 via Knoxville - Memphis",
    },
    {
        "from": "Philadelphia",
        "to": "Austin",
        "distance_km": 2700,
        "duration_min": 1440,
        "waypoints": 4276,
        "via": "I-81 - I-40 - I-30 - I-35 via Memphis - Dallas",
    },
    {
        "from": "Philadelphia",
        "to": "San Jose",
        "distance_km": 4670,
        "duration_min": 2460,
        "waypoints": 7394,
        "via": "I-76 - I-80 via Chicago - Salt Lake City - Reno",
    },
    # San Antonio routes (4 remaining)
    {
        "from": "San Antonio",
        "to": "San Diego",
        "distance_km": 1940,
        "duration_min": 1035,
        "waypoints": 3072,
        "via": "I-10 - I-8 via El Paso - Tucson",
    },
    {
        "from": "San Antonio",
        "to": "Dallas",
        "distance_km": 440,
        "duration_min": 240,
        "waypoints": 697,
        "via": "I-35 via Waco - Temple",
    },
    {
        "from": "San Antonio",
        "to": "Austin",
        "distance_km": 130,
        "duration_min": 75,
        "waypoints": 206,
        "via": "I-35 via San Marcos - New Braunfels",
    },
    {
        "from": "San Antonio",
        "to": "San Jose",
        "distance_km": 2660,
        "duration_min": 1410,
        "waypoints": 4212,
        "via": "I-10 - I-5 via El Paso - Phoenix - LA",
    },
    # San Diego routes (3 remaining)
    {
        "from": "San Diego",
        "to": "Dallas",
        "distance_km": 2250,
        "duration_min": 1200,
        "waypoints": 3563,
        "via": "I-8 - I-10 - I-20 via Tucson - El Paso",
    },
    {
        "from": "San Diego",
        "to": "Austin",
        "distance_km": 2070,
        "duration_min": 1095,
        "waypoints": 3278,
        "via": "I-8 - I-10 via Tucson - El Paso - San Antonio",
    },
    {
        "from": "San Diego",
        "to": "San Jose",
        "distance_km": 740,
        "duration_min": 390,
        "waypoints": 1172,
        "via": "I-5 via Los Angeles - Bakersfield",
    },
    # Dallas routes (2 remaining)
    {
        "from": "Dallas",
        "to": "Austin",
        "distance_km": 315,
        "duration_min": 180,
        "waypoints": 499,
        "via": "I-35E - I-35 via Waco",
    },
    {
        "from": "Dallas",
        "to": "San Jose",
        "distance_km": 2700,
        "duration_min": 1440,
        "waypoints": 4276,
        "via": "I-20 - I-10 - I-5 via El Paso - Los Angeles",
    },
    # Austin routes (1 remaining)
    {
        "from": "Austin",
        "to": "San Jose",
        "distance_km": 2530,
        "duration_min": 1350,
        "waypoints": 4007,
        "via": "I-10 - I-5 via San Antonio - El Paso - LA",
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
            "url": "https://download.geofabrik.de/north-america/us-latest.osm.pbf",
            "path": "/tmp/osm-cache/north-america/us-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 9_876_543_210,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "United States",
            "region_namespace": "osm.cache.NorthAmerica",
            "continent": "North America",
            "geofabrik_path": "north-america/us",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/us_cities.geojson",
            "feature_count": len(
                [c for c in US_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "original_count": 19_502,
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": US_CITIES[0]["population"],
            "filter_applied": f"population >= {p.get('min_population', 0):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulationRange": lambda p: {
        "result": {
            "output_path": "/tmp/us_cities_1m_2m.geojson",
            "feature_count": len(RANGE_CITIES),
            "original_count": len(
                [c for c in US_CITIES if c["population"] >= p.get("min_population", 0)]
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
            "output_path": "/tmp/us_city_routes.geojson",
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
            "output_path": "/tmp/us_city_routes_map.html",
            "format": "html",
            "feature_count": NUM_ROUTES + len(RANGE_CITIES),
            "bounds": "25.84,-124.39,48.72,-66.94",
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
    """Run the US city route map workflow end-to-end with mock handlers."""
    print("Compiling CityRouteMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow
    print(
        'Executing: CityRouteMapByRegion(region="United States", '
        f'min_population={MIN_POP:,}, max_population={MAX_POP:,}, profile="car")'
    )
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulationRange -> BuildRoutesBetweenCities -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "United States",
            "min_population": MIN_POP,
            "max_population": MAX_POP,
            "profile": "car",
            "title": "US Cities 1-2M: All Driving Routes",
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
    print(f"\n{'=' * 75}")
    print("RESULTS: US Cities 1-2M Population - All-Pairs Driving Routes")
    print(f"{'=' * 75}")
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
        print(f"    {city['name']:.<18} {city['population']:>10,}  {city['state']:<4} {bar}")

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
    col_w = 14
    print("\n  Distance matrix (km):")
    header = "    " + "".ljust(18) + "".join(c[:col_w].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (18 + col_w * len(city_names)))
    for row_city in city_names:
        cells = []
        for col_city in city_names:
            if row_city == col_city:
                cells.append("--".rjust(col_w))
            else:
                r = route_map.get((row_city, col_city))
                cells.append(f"{r['distance_km']:,}".rjust(col_w) if r else "?".rjust(col_w))
        print(f"    {row_city:.<18}" + "".join(cells))

    # Duration matrix
    print("\n  Duration matrix (hours):")
    header = "    " + "".ljust(18) + "".join(c[:col_w].rjust(col_w) for c in city_names)
    print(header)
    print("    " + "-" * (18 + col_w * len(city_names)))
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
                    cells.append(f"{h}h {m:02d}m".rjust(col_w))
                else:
                    cells.append("?".rjust(col_w))
        print(f"    {row_city:.<18}" + "".join(cells))

    # All routes sorted by distance
    print(f"\n  All {NUM_ROUTES} routes (sorted by distance):")
    print(f"    {'Route':<32} {'Distance':>10} {'Duration':>10}  {'Via'}")
    print(f"    {'-' * 32} {'-' * 10} {'-' * 10}  {'-' * 40}")
    for route in sorted(ROUTES, key=lambda r: r["distance_km"], reverse=True):
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<32} {route['distance_km']:>7,} km  "
            f"{hours:>2}h {mins:02d}m   {route['via']}"
        )
    print(f"    {'-' * 32} {'-' * 10} {'-' * 10}")
    total_h = TOTAL_DURATION // 60
    total_m = TOTAL_DURATION % 60
    print(f"    {'Total':<32} {TOTAL_DISTANCE:>7,} km  {total_h:>2}h {total_m:02d}m")
    print(
        f"    {'Average':<32} {AVG_DISTANCE:>7,} km  "
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
            f"    {city_name:.<18} {len(city_routes)} routes, {total_km:>6,} km total  "
            f"(nearest: {nearest_city} {nearest['distance_km']:,} km, "
            f"farthest: {farthest_city} {farthest['distance_km']:,} km)"
        )

    # Geographic clusters
    texas_cities = [c for c in RANGE_CITIES if c["state"] == "TX"]
    california_cities = [c for c in RANGE_CITIES if c["state"] == "CA"]
    print("\n  Geographic clusters:")
    if texas_cities:
        names = ", ".join(c["name"] for c in texas_cities)
        print(f"    Texas ({len(texas_cities)} cities): {names}")
        tx_routes = [
            r
            for r in ROUTES
            if (
                r["from"] in [c["name"] for c in texas_cities]
                and r["to"] in [c["name"] for c in texas_cities]
            )
        ]
        if tx_routes:
            shortest = min(tx_routes, key=lambda r: r["distance_km"])
            print(
                f"      Shortest intra-TX route: {shortest['from']} -> {shortest['to']} "
                f"({shortest['distance_km']} km, {shortest['duration_min'] // 60}h {shortest['duration_min'] % 60:02d}m)"
            )
    if california_cities:
        names = ", ".join(c["name"] for c in california_cities)
        print(f"    California ({len(california_cities)} cities): {names}")
        ca_routes = [
            r
            for r in ROUTES
            if (
                r["from"] in [c["name"] for c in california_cities]
                and r["to"] in [c["name"] for c in california_cities]
            )
        ]
        if ca_routes:
            shortest = min(ca_routes, key=lambda r: r["distance_km"])
            print(
                f"      Shortest intra-CA route: {shortest['from']} -> {shortest['to']} "
                f"({shortest['distance_km']} km, {shortest['duration_min'] // 60}h {shortest['duration_min'] % 60:02d}m)"
            )

    # Cross-country extremes
    longest = max(ROUTES, key=lambda r: r["distance_km"])
    shortest = min(ROUTES, key=lambda r: r["distance_km"])
    print("\n  Extremes:")
    print(
        f"    Longest route:  {longest['from']} -> {longest['to']} "
        f"({longest['distance_km']:,} km, {longest['duration_min'] // 60}h {longest['duration_min'] % 60:02d}m)"
    )
    print(
        f"    Shortest route: {shortest['from']} -> {shortest['to']} "
        f"({shortest['distance_km']:,} km, {shortest['duration_min'] // 60}h {shortest['duration_min'] % 60:02d}m)"
    )
    print(
        f"    Ratio:          {longest['distance_km'] / shortest['distance_km']:.1f}x distance, "
        f"{longest['duration_min'] / shortest['duration_min']:.1f}x time"
    )

    # Show excluded cities
    too_big = [c for c in US_CITIES if c["population"] > MAX_POP]
    too_small = [c for c in US_CITIES if c["population"] < MIN_POP]
    print("\n  Excluded cities:")
    for c in too_big:
        print(
            f"    {c['name']:.<18} {c['population']:>10,}  {c['state']:<4} (above {MAX_POP / 1e6:.0f}M)"
        )
    for c in sorted(too_small, key=lambda x: x["population"], reverse=True):
        print(
            f"    {c['name']:.<18} {c['population']:>10,}  {c['state']:<4} (below {MIN_POP / 1e6:.0f}M)"
        )

    assert result.success
    assert outputs["region_name"] == "United States"
    assert outputs["city_count"] == len(RANGE_CITIES)
    assert outputs["route_count"] == NUM_ROUTES
    assert outputs["total_distance_km"] == TOTAL_DISTANCE
    assert outputs["avg_distance_km"] == AVG_DISTANCE
    assert outputs["map_path"] == "/tmp/us_city_routes_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
