#!/usr/bin/env python3
"""Example: Build driving routes between all mid-size French cities.

Demonstrates a 5-step workflow that finds cities within a population range
and computes ALL pairwise driving routes between them (every city to every
other city):
  1. Resolve "France" to the French OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population between 1,100,000 and 2,500,000
  4. Build all-pairs driving routes between the filtered cities
  5. Render an interactive Leaflet map of the route network

With 5 cities in range this produces C(5,2) = 10 pairwise routes.

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_france_cityroutes.py
"""

from facetwork import emit_dict, parse
from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry

# ---------------------------------------------------------------------------
# Program AST - declares the event facets the runtime needs to recognise.
# Uses nested Namespace nodes so the evaluator can resolve qualified names.
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
        min_population: Long = 1100000,
        max_population: Long = 2500000,
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
# Mock data - French cities and all-pairs driving routes.
# ---------------------------------------------------------------------------

FRENCH_CITIES = [
    {
        "name": "Paris",
        "region": "Ile-de-France",
        "population": 11_078_000,
        "lat": 48.857,
        "lon": 2.352,
    },
    {
        "name": "Lyon",
        "region": "Auvergne-Rhone-Alpes",
        "population": 2_300_000,
        "lat": 45.764,
        "lon": 4.836,
    },
    {
        "name": "Marseille",
        "region": "Provence-Alpes-Cote d'Azur",
        "population": 1_900_000,
        "lat": 43.297,
        "lon": 5.381,
    },
    {
        "name": "Toulouse",
        "region": "Occitanie",
        "population": 1_400_000,
        "lat": 43.605,
        "lon": 1.444,
    },
    {
        "name": "Bordeaux",
        "region": "Nouvelle-Aquitaine",
        "population": 1_300_000,
        "lat": 44.838,
        "lon": -0.579,
    },
    {
        "name": "Lille",
        "region": "Hauts-de-France",
        "population": 1_200_000,
        "lat": 50.629,
        "lon": 3.057,
    },
    {
        "name": "Nice",
        "region": "Provence-Alpes-Cote d'Azur",
        "population": 1_006_000,
        "lat": 43.710,
        "lon": 7.262,
    },
    {
        "name": "Nantes",
        "region": "Pays de la Loire",
        "population": 1_010_000,
        "lat": 47.218,
        "lon": -1.554,
    },
    {
        "name": "Strasbourg",
        "region": "Grand Est",
        "population": 800_000,
        "lat": 48.574,
        "lon": 7.753,
    },
    {
        "name": "Montpellier",
        "region": "Occitanie",
        "population": 780_000,
        "lat": 43.611,
        "lon": 3.877,
    },
    {"name": "Rennes", "region": "Bretagne", "population": 750_000, "lat": 48.114, "lon": -1.681},
    {
        "name": "Grenoble",
        "region": "Auvergne-Rhone-Alpes",
        "population": 690_000,
        "lat": 45.188,
        "lon": 5.724,
    },
    {"name": "Rouen", "region": "Normandie", "population": 660_000, "lat": 49.443, "lon": 1.100},
    {
        "name": "Toulon",
        "region": "Provence-Alpes-Cote d'Azur",
        "population": 580_000,
        "lat": 43.124,
        "lon": 5.928,
    },
]

MIN_POP = 1_100_000
MAX_POP = 2_500_000
RANGE_CITIES = [c for c in FRENCH_CITIES if MIN_POP <= c["population"] <= MAX_POP]

# All-pairs driving routes between the 5 cities in range.
# Every city connects to every other city.
ROUTES = [
    {
        "from": "Lyon",
        "to": "Marseille",
        "distance_km": 315,
        "duration_min": 190,
        "waypoints": 498,
        "via": "A7 Autoroute du Soleil",
    },
    {
        "from": "Lyon",
        "to": "Toulouse",
        "distance_km": 540,
        "duration_min": 300,
        "waypoints": 856,
        "via": "A47 - A75 via Clermont-Ferrand",
    },
    {
        "from": "Lyon",
        "to": "Bordeaux",
        "distance_km": 555,
        "duration_min": 330,
        "waypoints": 879,
        "via": "A89 via Clermont-Ferrand",
    },
    {
        "from": "Lyon",
        "to": "Lille",
        "distance_km": 665,
        "duration_min": 360,
        "waypoints": 1053,
        "via": "A6 - A1 via Paris",
    },
    {
        "from": "Marseille",
        "to": "Toulouse",
        "distance_km": 405,
        "duration_min": 240,
        "waypoints": 641,
        "via": "A9 via Montpellier - Narbonne",
    },
    {
        "from": "Marseille",
        "to": "Bordeaux",
        "distance_km": 645,
        "duration_min": 375,
        "waypoints": 1021,
        "via": "A9 - A61 - A62 via Toulouse",
    },
    {
        "from": "Marseille",
        "to": "Lille",
        "distance_km": 1005,
        "duration_min": 555,
        "waypoints": 1591,
        "via": "A7 - A6 - A1 via Lyon - Paris",
    },
    {
        "from": "Toulouse",
        "to": "Bordeaux",
        "distance_km": 245,
        "duration_min": 150,
        "waypoints": 388,
        "via": "A62 Autoroute des Deux Mers",
    },
    {
        "from": "Toulouse",
        "to": "Lille",
        "distance_km": 880,
        "duration_min": 480,
        "waypoints": 1394,
        "via": "A20 - A71 - A1 via Limoges - Paris",
    },
    {
        "from": "Bordeaux",
        "to": "Lille",
        "distance_km": 800,
        "duration_min": 450,
        "waypoints": 1267,
        "via": "A10 - A1 via Tours - Paris",
    },
]

TOTAL_DISTANCE = sum(r["distance_km"] for r in ROUTES)
TOTAL_DURATION = sum(r["duration_min"] for r in ROUTES)
AVG_DISTANCE = TOTAL_DISTANCE // len(ROUTES)
NUM_ROUTES = len(ROUTES)
assert NUM_ROUTES == len(RANGE_CITIES) * (len(RANGE_CITIES) - 1) // 2


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/france-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/france-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 4_123_456_789,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "France",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/france",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/france_cities.geojson",
            "feature_count": len(
                [c for c in FRENCH_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "original_count": 3412,
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": FRENCH_CITIES[0]["population"],
            "filter_applied": f"population >= {p.get('min_population', 0):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulationRange": lambda p: {
        "result": {
            "output_path": f"/tmp/france_cities_{p.get('min_population', 0) // 1000}k_{p.get('max_population', 0) // 1000}k.geojson",
            "feature_count": len(RANGE_CITIES),
            "original_count": len(
                [c for c in FRENCH_CITIES if c["population"] >= p.get("min_population", 0)]
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
            "output_path": "/tmp/france_city_routes.geojson",
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
            "output_path": "/tmp/france_city_routes_map.html",
            "format": "html",
            "feature_count": NUM_ROUTES + len(RANGE_CITIES),
            "bounds": "42.33,-1.79,51.09,7.69",
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
    """Run the France city route map workflow end-to-end with mock handlers."""
    print("Compiling CityRouteMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow
    print(
        'Executing: CityRouteMapByRegion(region="France", '
        f'min_population={MIN_POP:,}, max_population={MAX_POP:,}, profile="car")'
    )
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulationRange -> BuildRoutesBetweenCities -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "France",
            "min_population": MIN_POP,
            "max_population": MAX_POP,
            "profile": "car",
            "title": f"French Cities {MIN_POP // 1_000_000:.1f}-{MAX_POP / 1_000_000:.1f}M: All Driving Routes",
            "color": "#27ae60",
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
    print(f"\n{'=' * 70}")
    print(
        f"RESULTS: French Cities {MIN_POP / 1e6:.1f}-{MAX_POP / 1e6:.1f}M - All-Pairs Driving Routes"
    )
    print(f"{'=' * 70}")
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
        bar_len = int(30 * city["population"] / max_pop)
        bar = "#" * bar_len
        print(f"    {city['name']:.<14} {city['population']:>10,}  {city['region']:<30} {bar}")

    # Show the full route matrix
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

    col_w = 12
    print("\n  Distance matrix (km):")
    header = "    " + "".ljust(14) + "".join(c[:col_w].rjust(col_w) for c in city_names)
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

    print("\n  Duration matrix (hours):")
    header = "    " + "".ljust(14) + "".join(c[:col_w].rjust(col_w) for c in city_names)
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

    # Show all routes sorted by distance
    print(f"\n  All {NUM_ROUTES} routes (sorted by distance):")
    print(f"    {'Route':<28} {'Distance':>10} {'Duration':>10} {'Via'}")
    print(f"    {'-' * 28} {'-' * 10} {'-' * 10} {'-' * 35}")
    for route in sorted(ROUTES, key=lambda r: r["distance_km"], reverse=True):
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<28} {route['distance_km']:>7,} km  "
            f"{hours}h {mins:02d}m      {route['via']}"
        )
    print(f"    {'-' * 28} {'-' * 10} {'-' * 10}")
    total_h = TOTAL_DURATION // 60
    total_m = TOTAL_DURATION % 60
    print(f"    {'Total':<28} {TOTAL_DISTANCE:>7,} km  {total_h}h {total_m:02d}m")
    print(
        f"    {'Average':<28} {AVG_DISTANCE:>7,} km  "
        f"{TOTAL_DURATION // NUM_ROUTES // 60}h {TOTAL_DURATION // NUM_ROUTES % 60:02d}m"
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
            f"    {city_name}: {len(city_routes)} routes, {total_km:,} km total  "
            f"(nearest: {nearest_city} {nearest['distance_km']} km, "
            f"farthest: {farthest_city} {farthest['distance_km']} km)"
        )

    # Show which cities are excluded and why
    too_big = [c for c in FRENCH_CITIES if c["population"] > MAX_POP]
    too_small = [c for c in FRENCH_CITIES if c["population"] < MIN_POP]
    print("\n  Excluded cities:")
    for c in too_big:
        print(f"    {c['name']:.<14} {c['population']:>10,}  (above {MAX_POP / 1e6:.1f}M)")
    for c in sorted(too_small, key=lambda x: x["population"], reverse=True):
        print(f"    {c['name']:.<14} {c['population']:>10,}  (below {MIN_POP / 1e6:.1f}M)")

    assert result.success
    assert outputs["region_name"] == "France"
    assert outputs["city_count"] == len(RANGE_CITIES)
    assert outputs["route_count"] == NUM_ROUTES
    assert outputs["total_distance_km"] == TOTAL_DISTANCE
    assert outputs["avg_distance_km"] == AVG_DISTANCE
    assert outputs["map_path"] == "/tmp/france_city_routes_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
