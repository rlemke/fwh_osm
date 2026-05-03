#!/usr/bin/env python3
"""Example: Extract and filter geographic features in India.

Demonstrates a 5-step workflow using boundary extraction and radius filtering:
  1. Resolve "India" to the Indian OSM data extract
  2. Extract administrative and natural boundaries
  3. Filter by radius to find large features (>= 50 km equivalent radius)
  4. Further filter by boundary type to isolate state-level divisions
  5. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_india_filters.py
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
                            "name": "Filters",
                            "declarations": [
                                _ef(
                                    "ExtractAndFilterByRadius",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "admin_levels", "type": "Long"},
                                        {"name": "natural_types", "type": "String"},
                                        {"name": "radius", "type": "Double"},
                                        {"name": "unit", "type": "String"},
                                        {"name": "operator", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "FilteredFeatures"}],
                                ),
                                _ef(
                                    "FilterByTypeAndRadius",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "boundary_type", "type": "String"},
                                        {"name": "radius", "type": "Double"},
                                        {"name": "unit", "type": "String"},
                                        {"name": "operator", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "FilteredFeatures"}],
                                ),
                                _ef(
                                    "FilterByRadius",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "radius", "type": "Double"},
                                        {"name": "unit", "type": "String"},
                                        {"name": "operator", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "FilteredFeatures"}],
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
# Workflow AST - a 5-step pipeline: resolve, extract+filter, refine, filter
# by type, render map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow FilteredBoundaryMap(
        region: String,
        min_radius_km: Double = 50,
        boundary_type: String = "administrative",
        prefer_continent: String = "",
        title: String = "Large Geographic Features",
        color: String = "#c0392b"
    ) => (map_path: String, extracted_count: Long, filtered_count: Long,
          type_filtered_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        extracted = ExtractAndFilterByRadius(
            cache = resolved.cache,
            radius = $.min_radius_km,
            unit = "kilometers",
            operator = "gte"
        )
        by_type = FilterByTypeAndRadius(
            input_path = extracted.result.output_path,
            boundary_type = $.boundary_type,
            radius = $.min_radius_km,
            unit = "kilometers",
            operator = "gte"
        )
        large = FilterByRadius(
            input_path = by_type.result.output_path,
            radius = 100,
            unit = "kilometers",
            operator = "gte"
        )
        map = RenderMap(
            geojson_path = large.result.output_path,
            title = $.title,
            color = $.color
        )
        yield FilteredBoundaryMap(
            map_path = map.result.output_path,
            extracted_count = extracted.result.feature_count,
            filtered_count = by_type.result.feature_count,
            type_filtered_count = large.result.feature_count,
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
            if wf["name"] == "FilteredBoundaryMap":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Indian state and geographic feature data.
# ---------------------------------------------------------------------------

INDIA_STATES = [
    {"name": "Rajasthan", "area_km2": 342239, "radius_km": 330.1, "capital": "Jaipur"},
    {"name": "Madhya Pradesh", "area_km2": 308252, "radius_km": 313.2, "capital": "Bhopal"},
    {"name": "Maharashtra", "area_km2": 307713, "radius_km": 312.9, "capital": "Mumbai"},
    {"name": "Uttar Pradesh", "area_km2": 240928, "radius_km": 277.0, "capital": "Lucknow"},
    {"name": "Gujarat", "area_km2": 196024, "radius_km": 249.7, "capital": "Gandhinagar"},
    {"name": "Karnataka", "area_km2": 191791, "radius_km": 247.1, "capital": "Bengaluru"},
    {"name": "Andhra Pradesh", "area_km2": 162975, "radius_km": 227.7, "capital": "Amaravati"},
    {"name": "Odisha", "area_km2": 155707, "radius_km": 222.5, "capital": "Bhubaneswar"},
    {"name": "Tamil Nadu", "area_km2": 130058, "radius_km": 203.4, "capital": "Chennai"},
    {"name": "Telangana", "area_km2": 112077, "radius_km": 188.8, "capital": "Hyderabad"},
    {"name": "Bihar", "area_km2": 94163, "radius_km": 173.1, "capital": "Patna"},
    {"name": "West Bengal", "area_km2": 88752, "radius_km": 168.0, "capital": "Kolkata"},
    {"name": "Arunachal Pradesh", "area_km2": 83743, "radius_km": 163.2, "capital": "Itanagar"},
    {"name": "Jharkhand", "area_km2": 79716, "radius_km": 159.3, "capital": "Ranchi"},
    {"name": "Assam", "area_km2": 78438, "radius_km": 158.0, "capital": "Dispur"},
    {"name": "Himachal Pradesh", "area_km2": 55673, "radius_km": 133.1, "capital": "Shimla"},
    {"name": "Uttarakhand", "area_km2": 53483, "radius_km": 130.4, "capital": "Dehradun"},
    {"name": "Punjab", "area_km2": 50362, "radius_km": 126.6, "capital": "Chandigarh"},
    {"name": "Kerala", "area_km2": 38863, "radius_km": 111.2, "capital": "Thiruvananthapuram"},
    {"name": "Haryana", "area_km2": 44212, "radius_km": 118.6, "capital": "Chandigarh"},
]

# States with radius >= 100 km (all of the above qualify)
LARGE_STATES = [s for s in INDIA_STATES if s["radius_km"] >= 100]

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/asia/india-latest.osm.pbf",
            "path": "/tmp/osm-cache/asia/india-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 1456789012,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "India",
            "region_namespace": "osm.cache.Asia",
            "continent": "Asia",
            "geofabrik_path": "asia/india",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractAndFilterByRadius": lambda p: {
        "result": {
            "output_path": "/tmp/india_boundaries_r50.geojson",
            "feature_count": 847,
            "original_count": 12436,
            "boundary_type": "all",
            "filter_applied": f"radius >= {p.get('radius', 50)} {p.get('unit', 'km')}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "FilterByTypeAndRadius": lambda p: {
        "result": {
            "output_path": "/tmp/india_admin_r50.geojson",
            "feature_count": len(INDIA_STATES),
            "original_count": 847,
            "boundary_type": p.get("boundary_type", "administrative"),
            "filter_applied": f"{p.get('boundary_type', 'administrative')}, radius >= {p.get('radius', 50)} km",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "FilterByRadius": lambda p: {
        "result": {
            "output_path": "/tmp/india_large_states.geojson",
            "feature_count": len(LARGE_STATES),
            "original_count": len(INDIA_STATES),
            "boundary_type": "administrative",
            "filter_applied": f"radius >= {p.get('radius', 100)} {p.get('unit', 'km')}",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/india_filtered_map.html",
            "format": "html",
            "feature_count": len(LARGE_STATES),
            "bounds": "6.75,68.16,35.51,97.40",
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
    """Run the India filter workflow end-to-end with mock handlers."""
    print("Compiling FilteredBoundaryMap from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: FilteredBoundaryMap(region="India", min_radius_km=50)')
    print("  Pipeline: ResolveRegion -> ExtractAndFilterByRadius")
    print("            -> FilterByTypeAndRadius -> FilterByRadius -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "India",
            "min_radius_km": 50.0,
            "boundary_type": "administrative",
            "title": "Indian States by Size (radius >= 100 km)",
            "color": "#c0392b",
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
    print("RESULTS: Indian States Filtered by Size")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show filter pipeline
    print("\n  Filter pipeline:")
    print("    All boundaries:                   12,436")
    print(f"    After radius >= 50 km:            {outputs.get('extracted_count'):>6,}")
    print(f"    After administrative type filter:  {outputs.get('filtered_count'):>6}")
    print(f"    After radius >= 100 km:           {outputs.get('type_filtered_count'):>6}")

    # Show states sorted by area
    total_area = sum(s["area_km2"] for s in LARGE_STATES)
    print(
        f"\n  Large Indian states (radius >= 100 km, {len(LARGE_STATES)} states, {total_area:,} km2):"
    )
    print(f"  {'State':<24} {'Capital':<22} {'Area (km2)':>12} {'Radius (km)':>12}")
    print(f"  {'-' * 24} {'-' * 22} {'-' * 12} {'-' * 12}")
    for state in sorted(LARGE_STATES, key=lambda s: s["area_km2"], reverse=True):
        print(
            f"  {state['name']:<24} {state['capital']:<22} {state['area_km2']:>10,}   {state['radius_km']:>9.1f}"
        )

    assert result.success
    assert outputs["region_name"] == "India"
    assert outputs["extracted_count"] == 847
    assert outputs["filtered_count"] == len(INDIA_STATES)
    assert outputs["type_filtered_count"] == len(LARGE_STATES)
    assert outputs["map_path"] == "/tmp/india_filtered_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
