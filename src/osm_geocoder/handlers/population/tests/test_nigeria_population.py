#!/usr/bin/env python3
"""Example: Find population data in Nigeria.

Demonstrates a 5-step workflow using population extraction and filtering:
  1. Resolve "Nigeria" to the Nigerian OSM data extract
  2. Extract all populated places from the OSM data
  3. Filter to cities with population >= 500,000
  4. Compute population statistics
  5. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_nigeria_population.py
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
                                    "FilterByPopulation",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "min_population", "type": "Long"},
                                        {"name": "place_type", "type": "String"},
                                        {"name": "operator", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "PopulationFilteredFeatures"}],
                                ),
                                _ef(
                                    "PopulationStatistics",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "place_type", "type": "String"},
                                    ],
                                    [{"name": "stats", "type": "PopulationStats"}],
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
# Workflow AST - a 5-step pipeline: resolve, extract, filter, stats, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow PopulationMapByRegion(
        region: String,
        min_population: Long = 500000,
        place_type: String = "city",
        prefer_continent: String = "",
        title: String = "Major Cities by Population",
        color: String = "#e74c3c"
    ) => (map_path: String, total_places: Long, filtered_count: Long,
          total_population: Long, max_population: Long, avg_population: Long,
          region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        all_places = ExtractPlacesWithPopulation(
            cache = resolved.cache,
            place_type = $.place_type
        )
        filtered = FilterByPopulation(
            input_path = all_places.result.output_path,
            min_population = $.min_population,
            place_type = $.place_type,
            operator = "gte"
        )
        stats = PopulationStatistics(
            input_path = filtered.result.output_path,
            place_type = $.place_type
        )
        map = RenderMap(
            geojson_path = filtered.result.output_path,
            title = $.title,
            color = $.color
        )
        yield PopulationMapByRegion(
            map_path = map.result.output_path,
            total_places = all_places.result.feature_count,
            filtered_count = filtered.result.feature_count,
            total_population = stats.stats.total_population,
            max_population = stats.stats.max_population,
            avg_population = stats.stats.avg_population,
            region_name = resolved.resolution.matched_name
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
            if wf["name"] == "PopulationMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Nigerian city population data.
# ---------------------------------------------------------------------------

NIGERIA_CITIES = [
    {"name": "Lagos", "state": "Lagos", "population": 16006000, "type": "city"},
    {"name": "Kano", "state": "Kano", "population": 4103000, "type": "city"},
    {"name": "Ibadan", "state": "Oyo", "population": 3649000, "type": "city"},
    {"name": "Abuja", "state": "FCT", "population": 3464000, "type": "city"},
    {"name": "Port Harcourt", "state": "Rivers", "population": 3171000, "type": "city"},
    {"name": "Benin City", "state": "Edo", "population": 1782000, "type": "city"},
    {"name": "Maiduguri", "state": "Borno", "population": 1197000, "type": "city"},
    {"name": "Zaria", "state": "Kaduna", "population": 1091000, "type": "city"},
    {"name": "Aba", "state": "Abia", "population": 1024000, "type": "city"},
    {"name": "Jos", "state": "Plateau", "population": 917000, "type": "city"},
    {"name": "Ilorin", "state": "Kwara", "population": 908000, "type": "city"},
    {"name": "Oyo", "state": "Oyo", "population": 847000, "type": "city"},
    {"name": "Enugu", "state": "Enugu", "population": 795000, "type": "city"},
    {"name": "Abeokuta", "state": "Ogun", "population": 764000, "type": "city"},
    {"name": "Onitsha", "state": "Anambra", "population": 738000, "type": "city"},
    {"name": "Warri", "state": "Delta", "population": 699000, "type": "city"},
    {"name": "Sokoto", "state": "Sokoto", "population": 672000, "type": "city"},
    {"name": "Calabar", "state": "Cross River", "population": 605000, "type": "city"},
    {"name": "Katsina", "state": "Katsina", "population": 587000, "type": "city"},
    {"name": "Akure", "state": "Ondo", "population": 555000, "type": "city"},
]

LARGE_CITIES = [c for c in NIGERIA_CITIES if c["population"] >= 500000]
TOTAL_POP = sum(c["population"] for c in LARGE_CITIES)
MAX_POP = max(c["population"] for c in LARGE_CITIES)
AVG_POP = TOTAL_POP // len(LARGE_CITIES)

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/africa/nigeria-latest.osm.pbf",
            "path": "/tmp/osm-cache/africa/nigeria-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 234567890,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Nigeria",
            "region_namespace": "osm.cache.Africa",
            "continent": "Africa",
            "geofabrik_path": "africa/nigeria",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractPlacesWithPopulation": lambda p: {
        "result": {
            "output_path": "/tmp/nigeria_cities.geojson",
            "feature_count": 847,
            "original_count": 12463,
            "place_type": p.get("place_type", "city"),
            "min_population": 0,
            "max_population": 16006000,
            "filter_applied": f"place_type={p.get('place_type', 'city')}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByPopulation": lambda p: {
        "result": {
            "output_path": f"/tmp/nigeria_cities_pop{p.get('min_population', 0)}.geojson",
            "feature_count": len(LARGE_CITIES),
            "original_count": 847,
            "place_type": p.get("place_type", "city"),
            "min_population": p.get("min_population", 500000),
            "max_population": MAX_POP,
            "filter_applied": f"population >= {p.get('min_population', 500000):,}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "PopulationStatistics": lambda p: {
        "stats": {
            "total_places": len(LARGE_CITIES),
            "total_population": TOTAL_POP,
            "min_population": min(c["population"] for c in LARGE_CITIES),
            "max_population": MAX_POP,
            "avg_population": AVG_POP,
            "place_type": p.get("place_type", "city"),
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/nigeria_population_map.html",
            "format": "html",
            "feature_count": len(LARGE_CITIES),
            "bounds": "4.27,2.69,13.89,14.68",
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
    """Run the Nigeria population workflow end-to-end with mock handlers."""
    print("Compiling PopulationMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: PopulationMapByRegion(region="Nigeria", min_population=500000)')
    print("  Pipeline: ResolveRegion -> ExtractPlacesWithPopulation")
    print("            -> FilterByPopulation -> PopulationStatistics -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Nigeria",
            "min_population": 500000,
            "place_type": "city",
            "title": "Nigerian Cities Above 500,000 Population",
            "color": "#e74c3c",
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
    print(f"\n{'=' * 60}")
    print("RESULTS: Nigerian Cities Above 500,000 Population")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total places extracted: {outputs.get('total_places'):,}")
    print(f"  Cities >= 500,000:      {outputs.get('filtered_count')}")
    print(f"  Combined population:    {outputs.get('total_population'):,}")
    print(f"  Largest city:           {outputs.get('max_population'):,}")
    print(f"  Average population:     {outputs.get('avg_population'):,}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show cities with population bar chart
    print("\n  Cities with population >= 500,000:")
    max_pop = LARGE_CITIES[0]["population"]
    for city in sorted(LARGE_CITIES, key=lambda c: c["population"], reverse=True):
        bar_len = int(40 * city["population"] / max_pop)
        bar = "#" * bar_len
        print(f"    {city['name']:.<20} {city['population']:>11,}  {city['state']:<14} {bar}")

    # Show regional distribution
    regions = {}
    for city in LARGE_CITIES:
        state = city["state"]
        regions.setdefault(state, []).append(city)

    print(f"\n  Distribution across {len(regions)} states:")
    for state in sorted(
        regions, key=lambda s: sum(c["population"] for c in regions[s]), reverse=True
    ):
        cities = regions[state]
        state_pop = sum(c["population"] for c in cities)
        names = ", ".join(c["name"] for c in cities)
        print(
            f"    {state:.<20} {state_pop:>11,}  ({len(cities)} {'city' if len(cities) == 1 else 'cities'}: {names})"
        )

    assert result.success
    assert outputs["region_name"] == "Nigeria"
    assert outputs["total_places"] == 847
    assert outputs["filtered_count"] == len(LARGE_CITIES)
    assert outputs["total_population"] == TOTAL_POP
    assert outputs["max_population"] == MAX_POP
    assert outputs["map_path"] == "/tmp/nigeria_population_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
