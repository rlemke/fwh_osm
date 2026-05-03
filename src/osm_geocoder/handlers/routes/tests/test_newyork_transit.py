#!/usr/bin/env python3
"""Example: Find all public transport routes in New York.

Demonstrates a simpler 3-step workflow (no elevation filtering):
  1. Resolve "New York" to the NY state OSM data extract
  2. Extract all public transport (subway, bus, commuter rail, ferry)
  3. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_newyork_transit.py
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
                            "name": "Routes",
                            "declarations": [
                                _ef(
                                    "PublicTransport",
                                    [{"name": "cache", "type": "OSMCache"}],
                                    [{"name": "result", "type": "RouteFeatures"}],
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
# Workflow AST - a simpler 3-step pipeline without elevation filtering.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow PublicTransportMapByRegion(
        region: String,
        prefer_continent: String = "",
        title: String = "Public Transport Routes",
        color: String = "#2ecc71"
    ) => (map_path: String, feature_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        transit = PublicTransport(cache = resolved.cache)
        map = RenderMap(
            geojson_path = transit.result.output_path,
            title = $.title,
            color = $.color
        )
        yield PublicTransportMapByRegion(
            map_path = map.result.output_path,
            feature_count = transit.result.feature_count,
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
            if wf["name"] == "PublicTransportMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# New York public transport data covering subway, bus, rail, and ferry.
# ---------------------------------------------------------------------------

TRANSIT_ROUTES = [
    {"name": "1 Train (Broadway-7th Ave)", "mode": "subway", "stops": 38},
    {"name": "7 Train (Flushing)", "mode": "subway", "stops": 22},
    {"name": "A Train (8th Ave Express)", "mode": "subway", "stops": 44},
    {"name": "L Train (14th St-Canarsie)", "mode": "subway", "stops": 24},
    {"name": "N/Q/R (Broadway)", "mode": "subway", "stops": 49},
    {"name": "Metro-North Hudson Line", "mode": "commuter_rail", "stops": 21},
    {"name": "Metro-North Harlem Line", "mode": "commuter_rail", "stops": 27},
    {"name": "Metro-North New Haven Line", "mode": "commuter_rail", "stops": 25},
    {"name": "LIRR Main Line", "mode": "commuter_rail", "stops": 19},
    {"name": "LIRR Montauk Branch", "mode": "commuter_rail", "stops": 23},
    {"name": "NJ Transit Northeast Corridor", "mode": "commuter_rail", "stops": 15},
    {"name": "M15 (1st/2nd Ave)", "mode": "bus", "stops": 62},
    {"name": "M42 (42nd St Crosstown)", "mode": "bus", "stops": 28},
    {"name": "Bx12 SBS (Fordham Rd)", "mode": "bus", "stops": 18},
    {"name": "B44 SBS (Nostrand Ave)", "mode": "bus", "stops": 22},
    {"name": "Q70 SBS (LaGuardia Link)", "mode": "bus", "stops": 4},
    {"name": "Staten Island Ferry", "mode": "ferry", "stops": 2},
    {"name": "NYC Ferry East River", "mode": "ferry", "stops": 7},
    {"name": "NYC Ferry Rockaway", "mode": "ferry", "stops": 4},
    {"name": "Staten Island Railway", "mode": "light_rail", "stops": 22},
    {"name": "AirTrain JFK", "mode": "people_mover", "stops": 10},
    {"name": "Roosevelt Island Tramway", "mode": "aerial_tramway", "stops": 2},
]

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/north-america/us/new-york-latest.osm.pbf",
            "path": "/tmp/osm-cache/north-america/us/new-york-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 587654321,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "NewYork",
            "region_namespace": "osm.cache.NorthAmerica.UnitedStates",
            "continent": "NorthAmerica",
            "geofabrik_path": "north-america/us/new-york",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "PublicTransport": lambda p: {
        "result": {
            "output_path": "/tmp/newyork_public_transport.geojson",
            "feature_count": len(TRANSIT_ROUTES),
            "route_type": "public_transport",
            "network_level": "*",
            "include_infrastructure": True,
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/newyork_transit_map.html",
            "format": "html",
            "feature_count": len(TRANSIT_ROUTES),
            "bounds": "40.49,-74.26,41.17,-73.70",
            "title": p.get("title", "Map"),
            "extraction_date": "2026-02-06T12:00:02+00:00",
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
    """Run the New York public transport workflow end-to-end with mock handlers."""
    print("Compiling PublicTransportMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: PublicTransportMapByRegion(region="New York")')
    print("  Pipeline: ResolveRegion -> PublicTransport -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "New York",
            "title": "New York Public Transport Network",
            "color": "#2ecc71",
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
    print("RESULTS: New York Public Transport Network")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total routes found:     {outputs.get('feature_count')}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show routes by mode
    modes = {}
    for route in TRANSIT_ROUTES:
        mode = route["mode"]
        modes.setdefault(mode, []).append(route)

    print("\n  Routes by mode:")
    for mode in sorted(modes, key=lambda m: len(modes[m]), reverse=True):
        routes = modes[mode]
        label = mode.replace("_", " ").title()
        total_stops = sum(r["stops"] for r in routes)
        print(f"    {label:.<30} {len(routes):>2} routes, {total_stops:>3} stops")
        for route in routes:
            print(f"      {route['name']:.<38} {route['stops']:>3} stops")

    assert result.success
    assert outputs["region_name"] == "NewYork"
    assert outputs["feature_count"] == len(TRANSIT_ROUTES)
    assert outputs["map_path"] == "/tmp/newyork_transit_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
