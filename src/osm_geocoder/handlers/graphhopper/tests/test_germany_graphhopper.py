#!/usr/bin/env python3
"""Example: Build GraphHopper routing graphs for Germany.

Demonstrates a 4-step workflow using GraphHopper routing graph operations:
  1. Resolve "Germany" to the German OSM data extract
  2. Build a multi-profile routing graph (car, bike, foot)
  3. Validate the routing graph and get statistics
  4. Render an interactive Leaflet map of the road network coverage

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_germany_graphhopper.py
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
                            "name": "Operations",
                            "declarations": [
                                {
                                    "type": "Namespace",
                                    "name": "GraphHopper",
                                    "declarations": [
                                        _ef(
                                            "BuildGraph",
                                            [
                                                {"name": "cache", "type": "OSMCache"},
                                                {"name": "profile", "type": "String"},
                                                {"name": "recreate", "type": "Boolean"},
                                            ],
                                            [{"name": "graph", "type": "GraphHopperCache"}],
                                        ),
                                        _ef(
                                            "ValidateGraph",
                                            [{"name": "graph", "type": "GraphHopperCache"}],
                                            [
                                                {"name": "valid", "type": "Boolean"},
                                                {"name": "nodeCount", "type": "Long"},
                                                {"name": "edgeCount", "type": "Long"},
                                            ],
                                        ),
                                    ],
                                },
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
# Workflow AST - a 4-step pipeline: resolve, build graph, validate, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow RoutingGraphByRegion(
        region: String,
        profile: String = "car",
        recreate: Boolean = false,
        prefer_continent: String = "",
        title: String = "Routing Graph Coverage",
        color: String = "#2980b9"
    ) => (map_path: String, region_name: String, profile: String,
          valid: Boolean, node_count: Long, edge_count: Long) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        graph = BuildGraph(
            cache = resolved.cache,
            profile = $.profile,
            recreate = $.recreate
        )
        validation = ValidateGraph(graph = graph.graph)
        map = RenderMap(
            geojson_path = graph.graph.path,
            title = $.title,
            color = $.color
        )
        yield RoutingGraphByRegion(
            map_path = map.result.output_path,
            region_name = resolved.resolution.matched_name,
            profile = $.profile,
            valid = validation.valid,
            node_count = validation.nodeCount,
            edge_count = validation.edgeCount
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
            if wf["name"] == "RoutingGraphByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# German routing graph data with realistic node/edge counts per profile.
# ---------------------------------------------------------------------------

PROFILES = {
    "car": {
        "node_count": 18432567,
        "edge_count": 42876543,
        "graph_size_mb": 1847,
        "build_time_s": 342,
        "description": "Motorway, primary, secondary, tertiary, residential roads",
    },
    "bike": {
        "node_count": 21567834,
        "edge_count": 48923156,
        "graph_size_mb": 2134,
        "build_time_s": 418,
        "description": "All roads + cycleways, bike paths, shared paths",
    },
    "foot": {
        "node_count": 24891023,
        "edge_count": 53412789,
        "graph_size_mb": 2456,
        "build_time_s": 487,
        "description": "All roads + footways, paths, steps, pedestrian zones",
    },
}

CURRENT_PROFILE = "car"

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
    "BuildGraph": lambda p: {
        "graph": {
            "url": f"file:///tmp/graphhopper/germany-{p.get('profile', 'car')}",
            "path": f"/tmp/graphhopper/germany-{p.get('profile', 'car')}",
            "date": "2026-02-06T12:00:01+00:00",
            "size": PROFILES[p.get("profile", "car")]["graph_size_mb"] * 1024 * 1024,
            "wasInCache": not p.get("recreate", False),
        },
    },
    "ValidateGraph": lambda p: {
        "valid": True,
        "nodeCount": PROFILES[CURRENT_PROFILE]["node_count"],
        "edgeCount": PROFILES[CURRENT_PROFILE]["edge_count"],
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/germany_routing_map.html",
            "format": "html",
            "feature_count": 1,
            "bounds": "47.27,5.87,55.06,15.04",
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
    """Run the Germany routing graph workflow end-to-end with mock handlers."""
    global CURRENT_PROFILE

    print("Compiling RoutingGraphByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    # Run the workflow for each routing profile
    for profile in ["car", "bike", "foot"]:
        CURRENT_PROFILE = profile
        store = MemoryStore()
        evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

        print(f'Executing: RoutingGraphByRegion(region="Germany", profile="{profile}")')
        print("  Pipeline: ResolveRegion -> BuildGraph -> ValidateGraph -> RenderMap\n")

        result = evaluator.execute(
            workflow_ast,
            inputs={
                "region": "Germany",
                "profile": profile,
                "recreate": False,
                "title": f"Germany Routing Graph ({profile})",
                "color": "#2980b9",
            },
            program_ast=PROGRAM_AST,
        )
        assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

        # Process event steps
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

        assert result.status == ExecutionStatus.COMPLETED, (
            f"Expected COMPLETED, got {result.status}"
        )

        outputs = result.outputs
        print(
            f"\n  Result: profile={outputs.get('profile')}, "
            f"valid={outputs.get('valid')}, "
            f"nodes={outputs.get('node_count'):,}, "
            f"edges={outputs.get('edge_count'):,}"
        )
        print(f"  Map: {outputs.get('map_path')}")

        assert result.success
        assert outputs["region_name"] == "Germany"
        assert outputs["profile"] == profile
        assert outputs["valid"] is True
        print(f"  OK ({step_num} steps)\n")

    # Summary across all profiles
    print(f"{'=' * 60}")
    print("RESULTS: Germany GraphHopper Routing Graphs")
    print(f"{'=' * 60}")
    print(
        f"\n  {'Profile':<10} {'Nodes':>14} {'Edges':>14} {'Graph Size':>12} {'Build Time':>12} Description"
    )
    print(f"  {'-' * 10} {'-' * 14} {'-' * 14} {'-' * 12} {'-' * 12} {'-' * 50}")
    for name, stats in PROFILES.items():
        print(
            f"  {name:<10} {stats['node_count']:>12,}   {stats['edge_count']:>12,}   "
            f"{stats['graph_size_mb']:>8,} MB  {stats['build_time_s']:>8} s   {stats['description']}"
        )

    total_nodes = sum(p["node_count"] for p in PROFILES.values())
    total_edges = sum(p["edge_count"] for p in PROFILES.values())
    total_size = sum(p["graph_size_mb"] for p in PROFILES.values())
    print(f"  {'-' * 10} {'-' * 14} {'-' * 14} {'-' * 12} {'-' * 12}")
    print(f"  {'Total':<10} {total_nodes:>12,}   {total_edges:>12,}   {total_size:>8,} MB")

    print("\n  Supported profiles: car, bike, foot, motorcycle, truck, hike, mtb, racingbike")
    print("\nAll assertions passed. (3 profiles x 4 steps = 12 event steps processed)")


if __name__ == "__main__":
    main()
