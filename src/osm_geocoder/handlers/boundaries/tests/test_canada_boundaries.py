#!/usr/bin/env python3
"""Example: Find administrative boundaries in Canada.

Demonstrates a 4-step workflow extracting multiple boundary types:
  1. Resolve "Canada" to the Canadian OSM data extract
  2. Extract province/territory boundaries (admin_level=4)
  3. Extract lake boundaries (major natural water features)
  4. Render an interactive Leaflet map with both layers

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_canada_boundaries.py
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
                            "name": "Boundaries",
                            "declarations": [
                                _ef(
                                    "StateBoundaries",
                                    [{"name": "cache", "type": "OSMCache"}],
                                    [{"name": "result", "type": "BoundaryFeatures"}],
                                ),
                                _ef(
                                    "LakeBoundaries",
                                    [{"name": "cache", "type": "OSMCache"}],
                                    [{"name": "result", "type": "BoundaryFeatures"}],
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
# Workflow AST - a 4-step pipeline: resolve, provinces, lakes, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow BoundaryMapByRegion(
        region: String,
        prefer_continent: String = "",
        title: String = "Administrative Boundaries",
        color: String = "#2c3e50"
    ) => (map_path: String, province_count: Long, lake_count: Long,
          region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        provinces = StateBoundaries(cache = resolved.cache)
        lakes = LakeBoundaries(cache = resolved.cache)
        map = RenderMap(
            geojson_path = provinces.result.output_path,
            title = $.title,
            color = $.color
        )
        yield BoundaryMapByRegion(
            map_path = map.result.output_path,
            province_count = provinces.result.feature_count,
            lake_count = lakes.result.feature_count,
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
            if wf["name"] == "BoundaryMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Canadian province/territory and lake boundary data.
# ---------------------------------------------------------------------------

PROVINCES = [
    {
        "name": "Ontario",
        "capital": "Toronto",
        "area_km2": 1076395,
        "pop_2021": 14223942,
        "abbrev": "ON",
    },
    {
        "name": "Quebec",
        "capital": "Quebec City",
        "area_km2": 1542056,
        "pop_2021": 8501833,
        "abbrev": "QC",
    },
    {
        "name": "British Columbia",
        "capital": "Victoria",
        "area_km2": 944735,
        "pop_2021": 5000879,
        "abbrev": "BC",
    },
    {
        "name": "Alberta",
        "capital": "Edmonton",
        "area_km2": 661848,
        "pop_2021": 4262635,
        "abbrev": "AB",
    },
    {
        "name": "Manitoba",
        "capital": "Winnipeg",
        "area_km2": 647797,
        "pop_2021": 1342153,
        "abbrev": "MB",
    },
    {
        "name": "Saskatchewan",
        "capital": "Regina",
        "area_km2": 651036,
        "pop_2021": 1132505,
        "abbrev": "SK",
    },
    {
        "name": "Nova Scotia",
        "capital": "Halifax",
        "area_km2": 55284,
        "pop_2021": 969383,
        "abbrev": "NS",
    },
    {
        "name": "New Brunswick",
        "capital": "Fredericton",
        "area_km2": 72908,
        "pop_2021": 775610,
        "abbrev": "NB",
    },
    {
        "name": "Newfoundland and Labrador",
        "capital": "St. John's",
        "area_km2": 405212,
        "pop_2021": 510550,
        "abbrev": "NL",
    },
    {
        "name": "Prince Edward Island",
        "capital": "Charlottetown",
        "area_km2": 5660,
        "pop_2021": 154331,
        "abbrev": "PE",
    },
    {
        "name": "Northwest Territories",
        "capital": "Yellowknife",
        "area_km2": 1346106,
        "pop_2021": 41070,
        "abbrev": "NT",
    },
    {
        "name": "Yukon",
        "capital": "Whitehorse",
        "area_km2": 482443,
        "pop_2021": 40232,
        "abbrev": "YT",
    },
    {
        "name": "Nunavut",
        "capital": "Iqaluit",
        "area_km2": 2093190,
        "pop_2021": 36858,
        "abbrev": "NU",
    },
]

LAKES = [
    {"name": "Lake Superior", "area_km2": 82100, "shared_with": "USA", "provinces": "ON"},
    {"name": "Lake Huron", "area_km2": 59600, "shared_with": "USA", "provinces": "ON"},
    {"name": "Great Bear Lake", "area_km2": 31153, "shared_with": "-", "provinces": "NT"},
    {"name": "Great Slave Lake", "area_km2": 28568, "shared_with": "-", "provinces": "NT"},
    {"name": "Lake Erie", "area_km2": 25700, "shared_with": "USA", "provinces": "ON"},
    {"name": "Lake Winnipeg", "area_km2": 24514, "shared_with": "-", "provinces": "MB"},
    {"name": "Lake Ontario", "area_km2": 19009, "shared_with": "USA", "provinces": "ON"},
    {"name": "Lake Athabasca", "area_km2": 7935, "shared_with": "-", "provinces": "AB/SK"},
    {"name": "Reindeer Lake", "area_km2": 6650, "shared_with": "-", "provinces": "MB/SK"},
    {"name": "Nettilling Lake", "area_km2": 5542, "shared_with": "-", "provinces": "NU"},
    {"name": "Lake Winnipegosis", "area_km2": 5374, "shared_with": "-", "provinces": "MB"},
    {"name": "Lake Nipigon", "area_km2": 4848, "shared_with": "-", "provinces": "ON"},
]

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/north-america/canada-latest.osm.pbf",
            "path": "/tmp/osm-cache/north-america/canada-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 2345678901,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Canada",
            "region_namespace": "osm.cache.NorthAmerica",
            "continent": "NorthAmerica",
            "geofabrik_path": "north-america/canada",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "StateBoundaries": lambda p: {
        "result": {
            "output_path": "/tmp/canada_provinces.geojson",
            "feature_count": len(PROVINCES),
            "boundary_type": "administrative",
            "admin_levels": "4",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "LakeBoundaries": lambda p: {
        "result": {
            "output_path": "/tmp/canada_lakes.geojson",
            "feature_count": len(LAKES),
            "boundary_type": "natural",
            "admin_levels": "-",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/canada_boundaries_map.html",
            "format": "html",
            "feature_count": len(PROVINCES) + len(LAKES),
            "bounds": "41.68,-141.00,83.11,-52.62",
            "title": p.get("title", "Map"),
            "extraction_date": "2026-02-06T12:00:03+00:00",
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
    """Run the Canada boundaries workflow end-to-end with mock handlers."""
    print("Compiling BoundaryMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: BoundaryMapByRegion(region="Canada")')
    print("  Pipeline: ResolveRegion -> StateBoundaries -> LakeBoundaries -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Canada",
            "title": "Canadian Provinces, Territories, and Major Lakes",
            "color": "#2c3e50",
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

        # Read params the evaluator stored on the step
        step = store.get_step(step_id)
        params = {k: v.value for k, v in step.attributes.params.items()}
        print(f"  Step {step_num}: {facet_short}")

        # Invoke mock handler and feed results back
        handler_result = handler(params)
        evaluator.continue_step(step_id, handler_result)

        # Resume workflow - will run until the next event step or completion
        result = evaluator.resume(result.workflow_id, workflow_ast, PROGRAM_AST)

    assert result.status == ExecutionStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"

    # 3. Show results
    outputs = result.outputs
    print(f"\n{'=' * 60}")
    print("RESULTS: Canadian Boundaries")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Provinces/territories:  {outputs.get('province_count')}")
    print(f"  Major lakes:            {outputs.get('lake_count')}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show provinces and territories
    total_area = sum(p["area_km2"] for p in PROVINCES)
    total_pop = sum(p["pop_2021"] for p in PROVINCES)
    print("\n  Provinces and Territories (10 provinces + 3 territories):")
    print(f"  {'Name':<30} {'Abbrev':<8} {'Capital':<16} {'Area (km2)':>12} {'Pop. 2021':>12}")
    print(f"  {'-' * 30} {'-' * 6}  {'-' * 14}  {'-' * 12} {'-' * 12}")
    for prov in sorted(PROVINCES, key=lambda p: p["pop_2021"], reverse=True):
        print(
            f"  {prov['name']:<30} {prov['abbrev']:<8} {prov['capital']:<16} {prov['area_km2']:>10,}   {prov['pop_2021']:>10,}"
        )
    print(f"  {'-' * 30} {'-' * 6}  {'-' * 14}  {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<30} {'':8} {'':16} {total_area:>10,}   {total_pop:>10,}")

    # Show major lakes
    total_lake_area = sum(lk["area_km2"] for lk in LAKES)
    print(f"\n  Major Lakes ({len(LAKES)} lakes, {total_lake_area:,} km2 total):")
    for lake in sorted(LAKES, key=lambda lk: lk["area_km2"], reverse=True):
        shared = f"(shared w/ {lake['shared_with']})" if lake["shared_with"] != "-" else ""
        print(
            f"    {lake['name']:.<30} {lake['area_km2']:>7,} km2  {lake['provinces']:<6} {shared}"
        )

    assert result.success
    assert outputs["region_name"] == "Canada"
    assert outputs["province_count"] == 13
    assert outputs["lake_count"] == 12
    assert outputs["map_path"] == "/tmp/canada_boundaries_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
