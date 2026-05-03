#!/usr/bin/env python3
"""Example: Find road network in Australia.

Demonstrates a 4-step workflow using road extraction and statistics:
  1. Resolve "Australia" to the Australian OSM data extract
  2. Extract all roads from the OSM data
  3. Compute road statistics by classification
  4. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_australia_roads.py
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
                            "name": "Roads",
                            "declarations": [
                                _ef(
                                    "ExtractRoads",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "road_class", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "RoadFeatures"}],
                                ),
                                _ef(
                                    "RoadStatistics",
                                    [{"name": "input_path", "type": "String"}],
                                    [{"name": "stats", "type": "RoadStats"}],
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
# Workflow AST - a 4-step pipeline: resolve, extract roads, stats, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow RoadMapByRegion(
        region: String,
        road_class: String = "all",
        prefer_continent: String = "",
        title: String = "Road Network",
        color: String = "#f39c12"
    ) => (map_path: String, total_roads: Long, total_length_km: Double,
          motorway_km: Double, primary_km: Double, secondary_km: Double,
          residential_km: Double, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        roads = ExtractRoads(cache = resolved.cache, road_class = $.road_class)
        stats = RoadStatistics(input_path = roads.result.output_path)
        map = RenderMap(
            geojson_path = roads.result.output_path,
            title = $.title,
            color = $.color
        )
        yield RoadMapByRegion(
            map_path = map.result.output_path,
            total_roads = stats.stats.total_roads,
            total_length_km = stats.stats.total_length_km,
            motorway_km = stats.stats.motorway_km,
            primary_km = stats.stats.primary_km,
            secondary_km = stats.stats.secondary_km,
            residential_km = stats.stats.residential_km,
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
            if wf["name"] == "RoadMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Australian road network data with realistic statistics.
# ---------------------------------------------------------------------------

AUSTRALIA_STATS = {
    "total_roads": 873421,
    "total_length_km": 877412.6,
    "motorway_km": 2487.3,
    "primary_km": 24618.5,
    "secondary_km": 41293.7,
    "tertiary_km": 68742.1,
    "residential_km": 312856.4,
    "other_km": 427414.6,
    "with_speed_limit": 234567,
    "with_surface": 412893,
    "with_lanes": 98432,
    "one_way_count": 67891,
}

NOTABLE_ROADS = [
    {
        "name": "Pacific Highway (M1)",
        "class": "motorway",
        "length_km": 868,
        "from": "Sydney",
        "to": "Brisbane",
    },
    {
        "name": "Hume Highway (M31)",
        "class": "motorway",
        "length_km": 840,
        "from": "Sydney",
        "to": "Melbourne",
    },
    {
        "name": "Stuart Highway",
        "class": "primary",
        "length_km": 2834,
        "from": "Adelaide",
        "to": "Darwin",
    },
    {
        "name": "Great Ocean Road (B100)",
        "class": "secondary",
        "length_km": 243,
        "from": "Torquay",
        "to": "Allansford",
    },
    {
        "name": "Great Northern Highway",
        "class": "primary",
        "length_km": 3198,
        "from": "Perth",
        "to": "Wyndham",
    },
    {
        "name": "Eyre Highway",
        "class": "primary",
        "length_km": 1660,
        "from": "Norseman",
        "to": "Port Augusta",
    },
    {
        "name": "Bruce Highway (M1)",
        "class": "motorway",
        "length_km": 1677,
        "from": "Brisbane",
        "to": "Cairns",
    },
    {
        "name": "Calder Highway",
        "class": "primary",
        "length_km": 266,
        "from": "Melbourne",
        "to": "Mildura",
    },
    {
        "name": "Great Western Highway",
        "class": "primary",
        "length_km": 201,
        "from": "Sydney",
        "to": "Bathurst",
    },
    {
        "name": "Gibb River Road",
        "class": "tertiary",
        "length_km": 660,
        "from": "Derby",
        "to": "Wyndham",
    },
]

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf",
            "path": "/tmp/osm-cache/australia-oceania/australia-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 1234567890,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Australia",
            "region_namespace": "osm.cache.Australia",
            "continent": "Australia",
            "geofabrik_path": "australia-oceania/australia",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractRoads": lambda p: {
        "result": {
            "output_path": "/tmp/australia_roads.geojson",
            "feature_count": AUSTRALIA_STATS["total_roads"],
            "road_class": p.get("road_class", "all"),
            "total_length_km": AUSTRALIA_STATS["total_length_km"],
            "with_speed_limit": AUSTRALIA_STATS["with_speed_limit"],
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "RoadStatistics": lambda p: {
        "stats": AUSTRALIA_STATS,
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/australia_roads_map.html",
            "format": "html",
            "feature_count": AUSTRALIA_STATS["total_roads"],
            "bounds": "-43.6,113.2,-10.7,153.6",
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
    """Run the Australia road network workflow end-to-end with mock handlers."""
    print("Compiling RoadMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: RoadMapByRegion(region="Australia")')
    print("  Pipeline: ResolveRegion -> ExtractRoads -> RoadStatistics -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Australia",
            "title": "Australian Road Network",
            "color": "#f39c12",
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
    print("RESULTS: Australian Road Network")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total road segments:    {outputs.get('total_roads'):,}")
    print(f"  Total network length:   {outputs.get('total_length_km'):,.1f} km")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show road classification breakdown
    classifications = [
        ("Motorway", outputs.get("motorway_km", 0)),
        ("Primary", outputs.get("primary_km", 0)),
        ("Secondary", outputs.get("secondary_km", 0)),
        ("Residential", outputs.get("residential_km", 0)),
    ]
    print("\n  Road network by classification:")
    for label, km in sorted(classifications, key=lambda x: x[1], reverse=True):
        bar = "#" * int(km / 5000)
        print(f"    {label:.<20} {km:>10,.1f} km  {bar}")

    # Show data quality
    stats = AUSTRALIA_STATS
    total = stats["total_roads"]
    print("\n  Data quality:")
    print(
        f"    With speed limit:     {stats['with_speed_limit']:>8,}  ({100 * stats['with_speed_limit'] / total:.0f}%)"
    )
    print(
        f"    With surface type:    {stats['with_surface']:>8,}  ({100 * stats['with_surface'] / total:.0f}%)"
    )
    print(
        f"    With lane count:      {stats['with_lanes']:>8,}  ({100 * stats['with_lanes'] / total:.0f}%)"
    )
    print(
        f"    One-way roads:        {stats['one_way_count']:>8,}  ({100 * stats['one_way_count'] / total:.0f}%)"
    )

    # Show notable roads
    print("\n  Notable roads:")
    for road in sorted(NOTABLE_ROADS, key=lambda r: r["length_km"], reverse=True):
        route = f"{road['from']} to {road['to']}"
        print(f"    {road['name']:.<36} {road['length_km']:>5,} km  {route}")

    assert result.success
    assert outputs["region_name"] == "Australia"
    assert outputs["total_roads"] == 873421
    assert outputs["total_length_km"] == 877412.6
    assert outputs["motorway_km"] == 2487.3
    assert outputs["map_path"] == "/tmp/australia_roads_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
