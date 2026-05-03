#!/usr/bin/env python3
"""Example: Find hiking trails in Colorado above 8,000 ft.

Demonstrates the full region-resolution pipeline:
  1. Resolve "Colorado" to US state OSM data
  2. Extract hiking trails from the OSM data
  3. Enrich trails with SRTM elevation data
  4. Filter to trails above 8,000 ft
  5. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_colorado_hiking.py
"""

from facetwork import emit_dict, parse
from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry

# ---------------------------------------------------------------------------
# Program AST -declares the event facets the runtime needs to recognise.
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
                            "name": "Routes",
                            "declarations": [
                                _ef(
                                    "HikingTrails",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "network", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "RouteFeatures"}],
                                ),
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "Elevation",
                            "declarations": [
                                _ef(
                                    "EnrichWithElevation",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "dem_source", "type": "String"},
                                        {"name": "sample_interval_m", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "ElevatedRouteFeatures"}],
                                ),
                                _ef(
                                    "FilterByMaxElevation",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "min_max_elevation_ft", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "ElevatedRouteFeatures"}],
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
# Workflow AST -compiled from FFL source for the real step/yield structure.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow HikingElevationMapByRegion(
        region: String,
        min_elevation_ft: Long = 8000,
        network: String = "*",
        prefer_continent: String = "",
        title: String = "High Elevation Hiking Trails",
        color: String = "#e67e22"
    ) => (map_path: String, feature_count: Long, matched_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        trails = HikingTrails(cache = resolved.cache, network = $.network)
        enriched = EnrichWithElevation(input_path = trails.result.output_path)
        filtered = FilterByMaxElevation(
            input_path = enriched.result.output_path,
            min_max_elevation_ft = $.min_elevation_ft
        )
        map = RenderMap(
            geojson_path = filtered.result.output_path,
            title = $.title,
            color = $.color
        )
        yield HikingElevationMapByRegion(
            map_path = map.result.output_path,
            feature_count = filtered.result.feature_count,
            matched_count = filtered.result.matched_count,
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
            if wf["name"] == "HikingElevationMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers -simulate each pipeline stage without network calls.
# Colorado hiking trail data with realistic names and elevations.
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/north-america/us/colorado-latest.osm.pbf",
            "path": "/tmp/osm-cache/north-america/us/colorado-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 312456789,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Colorado",
            "region_namespace": "osm.cache.NorthAmerica.UnitedStates",
            "continent": "NorthAmerica",
            "geofabrik_path": "north-america/us/colorado",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "HikingTrails": lambda p: {
        "result": {
            "output_path": "/tmp/colorado_hiking_trails.geojson",
            "feature_count": 1243,
            "route_type": "hiking",
            "network_level": "*",
            "include_infrastructure": True,
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "EnrichWithElevation": lambda p: {
        "result": {
            "output_path": "/tmp/colorado_hiking_trails_elevated.geojson",
            "routes": [
                {"name": "Colorado Trail", "stats": {"max_elevation_ft": 13271}},
                {"name": "Fourteeners Loop", "stats": {"max_elevation_ft": 14433}},
                {"name": "Continental Divide Trail", "stats": {"max_elevation_ft": 13200}},
                {"name": "Maroon Bells Scenic Loop", "stats": {"max_elevation_ft": 12500}},
                {"name": "Longs Peak Trail", "stats": {"max_elevation_ft": 14259}},
                {"name": "Ice Lake Basin", "stats": {"max_elevation_ft": 12585}},
                {"name": "Hanging Lake Trail", "stats": {"max_elevation_ft": 7300}},
                {"name": "Bear Creek Trail", "stats": {"max_elevation_ft": 6800}},
                {"name": "Garden of the Gods Loop", "stats": {"max_elevation_ft": 6500}},
                {"name": "Roxborough State Park Loop", "stats": {"max_elevation_ft": 6200}},
            ],
            "feature_count": 1243,
            "matched_count": 1243,
            "filter_applied": "none",
            "elevation_source": "srtm",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "FilterByMaxElevation": lambda p: {
        "result": {
            "output_path": f"/tmp/colorado_hiking_above_{p['min_max_elevation_ft']}ft.geojson",
            "routes": [
                {"name": "Colorado Trail", "stats": {"max_elevation_ft": 13271}},
                {"name": "Fourteeners Loop", "stats": {"max_elevation_ft": 14433}},
                {"name": "Continental Divide Trail", "stats": {"max_elevation_ft": 13200}},
                {"name": "Maroon Bells Scenic Loop", "stats": {"max_elevation_ft": 12500}},
                {"name": "Longs Peak Trail", "stats": {"max_elevation_ft": 14259}},
                {"name": "Ice Lake Basin", "stats": {"max_elevation_ft": 12585}},
            ],
            "feature_count": 1243,
            "matched_count": 6,
            "filter_applied": f"max_elevation >= {p['min_max_elevation_ft']} ft",
            "elevation_source": "srtm",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/colorado_hiking_map.html",
            "format": "html",
            "feature_count": 6,
            "bounds": "36.99,-109.05,41.00,-102.05",
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
    """Run the Colorado hiking trail workflow end-to-end with mock handlers."""
    print("Compiling HikingElevationMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow -pauses at the first event step (ResolveRegion)
    print('Executing: HikingElevationMapByRegion(region="Colorado", min_elevation_ft=8000)')
    print("  Pipeline: ResolveRegion -> HikingTrails -> EnrichWithElevation")
    print("            -> FilterByMaxElevation -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Colorado",
            "min_elevation_ft": 8000,
            "title": "Colorado Hiking Trails Above 8,000 ft",
            "color": "#e67e22",
        },
        program_ast=PROGRAM_AST,
    )
    assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

    # 2. Process event steps one at a time -simulate what an AgentPoller does
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

        # Resume workflow -will run until the next event step or completion
        result = evaluator.resume(result.workflow_id, workflow_ast, PROGRAM_AST)

    assert result.status == ExecutionStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"

    # 3. Show results
    outputs = result.outputs
    print(f"\n{'=' * 60}")
    print("RESULTS: Colorado Hiking Trails Above 8,000 ft")
    print(f"{'=' * 60}")
    print(f"  Region resolved:      {outputs.get('region_name')}")
    print(f"  Total trails found:   {outputs.get('feature_count')}")
    print(f"  Trails above 8000ft:  {outputs.get('matched_count')}")
    print(f"  Map output:           {outputs.get('map_path')}")

    # Show the matched trails
    filter_result = MOCK_HANDLERS["FilterByMaxElevation"]({"min_max_elevation_ft": 8000})
    trails = filter_result["result"]["routes"]
    print("\n  Matched trails:")
    for trail in sorted(trails, key=lambda t: t["stats"]["max_elevation_ft"], reverse=True):
        print(f"    {trail['name']:.<40} {trail['stats']['max_elevation_ft']:,} ft")

    assert result.success
    assert outputs["region_name"] == "Colorado"
    assert outputs["feature_count"] == 1243
    assert outputs["matched_count"] == 6
    assert outputs["map_path"] == "/tmp/colorado_hiking_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
