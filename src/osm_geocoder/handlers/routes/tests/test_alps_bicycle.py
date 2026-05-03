#!/usr/bin/env python3
"""Example: Find bicycle paths through the Alps.

Demonstrates the full region-resolution pipeline:
  1. Resolve "the Alps" → first matching Alpine country (Austria)
  2. Extract bicycle routes from the OSM data
  3. Enrich routes with SRTM elevation data
  4. Filter to routes above 4,000 ft
  5. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_alps_bicycle.py
"""

from facetwork import emit_dict, parse
from facetwork.runtime import Evaluator, ExecutionStatus, MemoryStore, Telemetry

# ---------------------------------------------------------------------------
# Program AST — declares the event facets the runtime needs to recognise.
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
                                    "BicycleRoutes",
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
# Workflow AST — compiled from FFL source for the real step/yield structure.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow BicycleElevationMapByRegion(
        region: String,
        min_elevation_ft: Long = 4000,
        network: String = "*",
        prefer_continent: String = "",
        title: String = "High Elevation Cycling Routes",
        color: String = "#e74c3c"
    ) => (map_path: String, feature_count: Long, matched_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        routes = BicycleRoutes(cache = resolved.cache, network = $.network)
        enriched = EnrichWithElevation(input_path = routes.result.output_path)
        filtered = FilterByMaxElevation(
            input_path = enriched.result.output_path,
            min_max_elevation_ft = $.min_elevation_ft
        )
        map = RenderMap(
            geojson_path = filtered.result.output_path,
            title = $.title,
            color = $.color
        )
        yield BicycleElevationMapByRegion(
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
            if wf["name"] == "BicycleElevationMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers — simulate each pipeline stage without network calls.
# In production these would call region_resolver.resolve(), downloader,
# osmium, Open-Elevation API, and Leaflet rendering.
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/austria-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/austria-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 723456789,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Austria",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/austria",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "BicycleRoutes": lambda p: {
        "result": {
            "output_path": "/tmp/austria_bicycle_routes.geojson",
            "feature_count": 847,
            "route_type": "bicycle",
            "network_level": "*",
            "include_infrastructure": True,
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "EnrichWithElevation": lambda p: {
        "result": {
            "output_path": "/tmp/austria_bicycle_routes_elevated.geojson",
            "routes": [
                {"name": "Inntal Radweg", "stats": {"max_elevation_ft": 5200}},
                {"name": "Brenner Radroute", "stats": {"max_elevation_ft": 4600}},
                {"name": "Salzkammergut Radweg", "stats": {"max_elevation_ft": 3800}},
                {"name": "Tauernradweg", "stats": {"max_elevation_ft": 5400}},
                {"name": "Donauradweg", "stats": {"max_elevation_ft": 1200}},
            ],
            "feature_count": 847,
            "matched_count": 847,
            "filter_applied": "none",
            "elevation_source": "srtm",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "FilterByMaxElevation": lambda p: {
        "result": {
            "output_path": f"/tmp/austria_bicycle_above_{p['min_max_elevation_ft']}ft.geojson",
            "routes": [
                {"name": "Inntal Radweg", "stats": {"max_elevation_ft": 5200}},
                {"name": "Brenner Radroute", "stats": {"max_elevation_ft": 4600}},
                {"name": "Tauernradweg", "stats": {"max_elevation_ft": 5400}},
            ],
            "feature_count": 847,
            "matched_count": 3,
            "filter_applied": f"max_elevation >= {p['min_max_elevation_ft']} ft",
            "elevation_source": "srtm",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/alps_cycling_map.html",
            "format": "html",
            "feature_count": 3,
            "bounds": "46.3,9.5,47.8,16.4",
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
    """Run the Alps bicycle route workflow end-to-end with mock handlers."""
    print("Compiling BicycleElevationMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow — pauses at the first event step (ResolveRegion)
    print('Executing: BicycleElevationMapByRegion(region="the Alps")')
    print("  Pipeline: ResolveRegion -> BicycleRoutes -> EnrichWithElevation")
    print("            -> FilterByMaxElevation -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "the Alps",
            "min_elevation_ft": 4000,
            "title": "Alpine Cycling Routes Above 4000 ft",
        },
        program_ast=PROGRAM_AST,
    )
    assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

    # 2. Process event steps one at a time — simulate what an AgentPoller does
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

        # Resume workflow — will run until the next event step or completion
        result = evaluator.resume(result.workflow_id, workflow_ast, PROGRAM_AST)

    assert result.status == ExecutionStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"

    # 3. Show results
    outputs = result.outputs
    print(f"\n{'=' * 60}")
    print("RESULTS: Alpine Cycling Routes Above 4,000 ft")
    print(f"{'=' * 60}")
    print(f"  Region resolved:     {outputs.get('region_name')}")
    print(f"  Total routes found:  {outputs.get('feature_count')}")
    print(f"  Routes above 4000ft: {outputs.get('matched_count')}")
    print(f"  Map output:          {outputs.get('map_path')}")

    assert result.success
    assert outputs["region_name"] == "Austria"
    assert outputs["feature_count"] == 847
    assert outputs["matched_count"] == 3
    assert outputs["map_path"] == "/tmp/alps_cycling_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
