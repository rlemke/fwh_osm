#!/usr/bin/env python3
"""Example: Build driving routes between mid-size German cities.

Demonstrates a 5-step workflow that finds cities within a population range
and computes pairwise driving routes between them:
  1. Resolve "Germany" to the German OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population between 1,000,000 and 2,000,000
  4. Build pairwise driving routes between the filtered cities
  5. Render an interactive Leaflet map of the route network

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_germany_cityroutes.py
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
# Mock data - German cities and pairwise driving routes.
# ---------------------------------------------------------------------------

GERMAN_CITIES = [
    {"name": "Berlin", "state": "Berlin", "population": 3748148, "lat": 52.520, "lon": 13.405},
    {"name": "Hamburg", "state": "Hamburg", "population": 1899160, "lat": 53.551, "lon": 9.994},
    {"name": "Munich", "state": "Bavaria", "population": 1472000, "lat": 48.137, "lon": 11.576},
    {"name": "Cologne", "state": "NRW", "population": 1084795, "lat": 50.938, "lon": 6.960},
    {"name": "Frankfurt", "state": "Hesse", "population": 764104, "lat": 50.110, "lon": 8.682},
    {"name": "Stuttgart", "state": "BaWu", "population": 635911, "lat": 48.776, "lon": 9.183},
    {"name": "Dusseldorf", "state": "NRW", "population": 621877, "lat": 51.228, "lon": 6.774},
    {"name": "Leipzig", "state": "Saxony", "population": 601866, "lat": 51.340, "lon": 12.375},
    {"name": "Dortmund", "state": "NRW", "population": 588250, "lat": 51.514, "lon": 7.468},
    {"name": "Essen", "state": "NRW", "population": 583109, "lat": 51.457, "lon": 7.012},
    {"name": "Bremen", "state": "Bremen", "population": 569352, "lat": 53.079, "lon": 8.801},
    {"name": "Dresden", "state": "Saxony", "population": 556780, "lat": 51.051, "lon": 13.738},
    {"name": "Hanover", "state": "LowerSaxony", "population": 536925, "lat": 52.376, "lon": 9.739},
    {"name": "Nuremberg", "state": "Bavaria", "population": 518365, "lat": 49.452, "lon": 11.077},
]

RANGE_CITIES = [c for c in GERMAN_CITIES if 1_000_000 <= c["population"] <= 2_000_000]

# Pairwise driving routes (from GraphHopper-style routing)
ROUTES = [
    {
        "from": "Hamburg",
        "to": "Munich",
        "distance_km": 790,
        "duration_min": 450,
        "waypoints": 1247,
        "via": "Hanover - Nuremberg",
    },
    {
        "from": "Hamburg",
        "to": "Cologne",
        "distance_km": 425,
        "duration_min": 245,
        "waypoints": 672,
        "via": "Dortmund - Dusseldorf",
    },
    {
        "from": "Munich",
        "to": "Cologne",
        "distance_km": 575,
        "duration_min": 330,
        "waypoints": 918,
        "via": "Stuttgart - Frankfurt",
    },
]

TOTAL_DISTANCE = sum(r["distance_km"] for r in ROUTES)
TOTAL_DURATION = sum(r["duration_min"] for r in ROUTES)
AVG_DISTANCE = TOTAL_DISTANCE // len(ROUTES)


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/germany-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/germany-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 3876543210,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Germany",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/germany",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/germany_cities.geojson",
            "feature_count": len(
                [c for c in GERMAN_CITIES if c["population"] >= p.get("min_population", 0)]
            ),
            "original_count": 2847,
            "place_type": "city",
            "min_population": p.get("min_population", 0),
            "max_population": GERMAN_CITIES[0]["population"],
            "filter_applied": f"population >= {p.get('min_population', 0):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulationRange": lambda p: {
        "result": {
            "output_path": "/tmp/germany_cities_1m_2m.geojson",
            "feature_count": len(RANGE_CITIES),
            "original_count": len(
                [c for c in GERMAN_CITIES if c["population"] >= p.get("min_population", 0)]
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
            "output_path": "/tmp/germany_city_routes.geojson",
            "route_count": len(ROUTES),
            "city_count": len(RANGE_CITIES),
            "total_distance_km": TOTAL_DISTANCE,
            "total_duration_min": TOTAL_DURATION,
            "avg_distance_km": AVG_DISTANCE,
            "avg_duration_min": TOTAL_DURATION // len(ROUTES),
            "profile": p.get("profile", "car"),
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/germany_city_routes_map.html",
            "format": "html",
            "feature_count": len(ROUTES) + len(RANGE_CITIES),
            "bounds": "47.27,5.87,55.06,15.04",
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
    """Run the city route map workflow end-to-end with mock handlers."""
    print("Compiling CityRouteMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print(
        'Executing: CityRouteMapByRegion(region="Germany", '
        'min_population=1000000, max_population=2000000, profile="car")'
    )
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulationRange -> BuildRoutesBetweenCities -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Germany",
            "min_population": 1_000_000,
            "max_population": 2_000_000,
            "profile": "car",
            "title": "German Cities 1-2M: Driving Routes",
            "color": "#27ae60",
        },
        program_ast=PROGRAM_AST,
    )
    assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

    # 2. Process event steps one at a time - simulate what an AgentPoller does
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
    print(f"\n{'=' * 65}")
    print("RESULTS: German Cities 1-2M Population - Driving Routes")
    print(f"{'=' * 65}")
    print(f"  Region:                 {outputs.get('region_name')}")
    print(f"  Cities in range:        {outputs.get('city_count')}")
    print(f"  Pairwise routes:        {outputs.get('route_count')}")
    print(f"  Total driving distance: {outputs.get('total_distance_km'):,} km")
    print(f"  Average route length:   {outputs.get('avg_distance_km'):,} km")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show the cities in range
    print("\n  Cities with population between 1,000,000 and 2,000,000:")
    max_pop = RANGE_CITIES[0]["population"]
    for city in sorted(RANGE_CITIES, key=lambda c: c["population"], reverse=True):
        bar_len = int(35 * city["population"] / max_pop)
        bar = "#" * bar_len
        print(f"    {city['name']:.<16} {city['population']:>10,}  {city['state']:<10} {bar}")

    # Show the routes
    print("\n  Driving routes (profile: car):")
    print(f"    {'Route':<26} {'Distance':>10} {'Duration':>10} {'Via'}")
    print(f"    {'-' * 26} {'-' * 10} {'-' * 10} {'-' * 30}")
    for route in sorted(ROUTES, key=lambda r: r["distance_km"], reverse=True):
        label = f"{route['from']} -> {route['to']}"
        hours = route["duration_min"] // 60
        mins = route["duration_min"] % 60
        print(
            f"    {label:<26} {route['distance_km']:>7,} km  "
            f"{hours}h {mins:02d}m      {route['via']}"
        )

    print(f"\n    Total distance: {TOTAL_DISTANCE:,} km across {len(ROUTES)} routes")
    print(f"    Total drive time: {TOTAL_DURATION // 60}h {TOTAL_DURATION % 60:02d}m")

    # Show which cities are excluded and why
    too_big = [c for c in GERMAN_CITIES if c["population"] > 2_000_000]
    too_small = [c for c in GERMAN_CITIES if c["population"] < 1_000_000]
    print("\n  Excluded cities:")
    for c in too_big:
        print(f"    {c['name']:.<16} {c['population']:>10,}  (above 2M)")
    for c in sorted(too_small, key=lambda x: x["population"], reverse=True)[:5]:
        print(f"    {c['name']:.<16} {c['population']:>10,}  (below 1M)")
    if len(too_small) > 5:
        print(f"    ... and {len(too_small) - 5} more cities below 1M")

    assert result.success
    assert outputs["region_name"] == "Germany"
    assert outputs["city_count"] == len(RANGE_CITIES)
    assert outputs["route_count"] == len(ROUTES)
    assert outputs["total_distance_km"] == TOTAL_DISTANCE
    assert outputs["avg_distance_km"] == AVG_DISTANCE
    assert outputs["map_path"] == "/tmp/germany_city_routes_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
