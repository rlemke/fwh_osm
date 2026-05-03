#!/usr/bin/env python3
"""Example: Find buildings in Berlin.

Demonstrates a 4-step workflow using building extraction and statistics:
  1. Resolve "Berlin" to the Berlin OSM data extract
  2. Extract all building footprints from the OSM data
  3. Compute building statistics by type
  4. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_berlin_buildings.py
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
                            "name": "Buildings",
                            "declarations": [
                                _ef(
                                    "ExtractBuildings",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "building_type", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "BuildingFeatures"}],
                                ),
                                _ef(
                                    "BuildingStatistics",
                                    [{"name": "input_path", "type": "String"}],
                                    [{"name": "stats", "type": "BuildingStats"}],
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
# Workflow AST - a 4-step pipeline: resolve, extract buildings, stats, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow BuildingMapByRegion(
        region: String,
        building_type: String = "all",
        prefer_continent: String = "",
        title: String = "Building Footprints",
        color: String = "#8e44ad"
    ) => (map_path: String, total_buildings: Long, total_area_km2: Double,
          residential: Long, commercial: Long, industrial: Long,
          retail: Long, avg_levels: Double, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        buildings = ExtractBuildings(cache = resolved.cache, building_type = $.building_type)
        stats = BuildingStatistics(input_path = buildings.result.output_path)
        map = RenderMap(
            geojson_path = buildings.result.output_path,
            title = $.title,
            color = $.color
        )
        yield BuildingMapByRegion(
            map_path = map.result.output_path,
            total_buildings = stats.stats.total_buildings,
            total_area_km2 = stats.stats.total_area_km2,
            residential = stats.stats.residential,
            commercial = stats.stats.commercial,
            industrial = stats.stats.industrial,
            retail = stats.stats.retail,
            avg_levels = stats.stats.avg_levels,
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
            if wf["name"] == "BuildingMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Berlin building data with realistic statistics.
# ---------------------------------------------------------------------------

BERLIN_STATS = {
    "total_buildings": 542817,
    "total_area_km2": 78.4,
    "residential": 387432,
    "commercial": 62418,
    "industrial": 28947,
    "retail": 31256,
    "other": 32764,
    "avg_levels": 4.2,
    "with_height": 128943,
}

NOTABLE_BUILDINGS = {
    "Landmarks": [
        {"name": "Reichstag Building", "type": "government", "levels": 4, "district": "Mitte"},
        {"name": "Berliner Fernsehturm", "type": "tower", "levels": 1, "district": "Mitte"},
        {"name": "Brandenburger Tor", "type": "monument", "levels": 1, "district": "Mitte"},
        {"name": "Berliner Dom", "type": "church", "levels": 3, "district": "Mitte"},
    ],
    "Commercial": [
        {"name": "Sony Center", "type": "commercial", "levels": 8, "district": "Tiergarten"},
        {"name": "KaDeWe", "type": "retail", "levels": 8, "district": "Schoeneberg"},
        {"name": "Mall of Berlin", "type": "retail", "levels": 4, "district": "Mitte"},
        {
            "name": "Potsdamer Platz Arkaden",
            "type": "commercial",
            "levels": 3,
            "district": "Tiergarten",
        },
    ],
    "Cultural": [
        {"name": "Philharmonie", "type": "concert_hall", "levels": 3, "district": "Tiergarten"},
        {"name": "Hamburger Bahnhof", "type": "museum", "levels": 2, "district": "Moabit"},
        {"name": "Berghain", "type": "nightclub", "levels": 3, "district": "Friedrichshain"},
        {
            "name": "Staatsoper Unter den Linden",
            "type": "theatre",
            "levels": 5,
            "district": "Mitte",
        },
    ],
    "Residential Towers": [
        {"name": "Park Inn Hotel", "type": "hotel", "levels": 37, "district": "Mitte"},
        {"name": "Steglitzer Kreisel", "type": "residential", "levels": 30, "district": "Steglitz"},
        {"name": "Treptowers", "type": "office", "levels": 17, "district": "Treptow"},
        {"name": "Upper West", "type": "mixed", "levels": 34, "district": "Charlottenburg"},
    ],
}

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/germany/berlin-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 98765432,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Berlin",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/germany/berlin",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractBuildings": lambda p: {
        "result": {
            "output_path": "/tmp/berlin_buildings.geojson",
            "feature_count": BERLIN_STATS["total_buildings"],
            "building_type": p.get("building_type", "all"),
            "total_area_km2": BERLIN_STATS["total_area_km2"],
            "with_height_data": BERLIN_STATS["with_height"],
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "BuildingStatistics": lambda p: {
        "stats": BERLIN_STATS,
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/berlin_buildings_map.html",
            "format": "html",
            "feature_count": BERLIN_STATS["total_buildings"],
            "bounds": "52.34,13.09,52.68,13.76",
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
    """Run the Berlin building workflow end-to-end with mock handlers."""
    print("Compiling BuildingMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: BuildingMapByRegion(region="Berlin")')
    print("  Pipeline: ResolveRegion -> ExtractBuildings -> BuildingStatistics -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Berlin",
            "title": "Berlin Building Footprints",
            "color": "#8e44ad",
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
    print("RESULTS: Berlin Building Footprints")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total buildings:        {outputs.get('total_buildings'):,}")
    print(f"  Total footprint area:   {outputs.get('total_area_km2'):.1f} km2")
    print(f"  Average building levels:{outputs.get('avg_levels'):.1f}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show building type breakdown
    types = [
        ("Residential", outputs.get("residential", 0)),
        ("Commercial", outputs.get("commercial", 0)),
        ("Retail", outputs.get("retail", 0)),
        ("Industrial", outputs.get("industrial", 0)),
    ]
    total = outputs.get("total_buildings", 1)
    print("\n  Buildings by type:")
    for label, count in sorted(types, key=lambda x: x[1], reverse=True):
        pct = 100 * count / total
        bar = "#" * int(pct / 2)
        print(f"    {label:.<20} {count:>8,}  ({pct:4.1f}%)  {bar}")

    # Show 3D data coverage
    with_height = BERLIN_STATS["with_height"]
    print(
        f"\n  3D data coverage:       {with_height:,} buildings ({100 * with_height / total:.0f}%) have height data"
    )

    # Show notable buildings
    print("\n  Notable buildings:")
    for category, buildings in NOTABLE_BUILDINGS.items():
        print(f"    {category}:")
        for bldg in buildings:
            levels = f"{bldg['levels']} levels" if bldg["levels"] > 1 else "1 level"
            print(f"      {bldg['name']:.<38} {levels:<10} {bldg['district']}")

    assert result.success
    assert outputs["region_name"] == "Berlin"
    assert outputs["total_buildings"] == 542817
    assert outputs["total_area_km2"] == 78.4
    assert outputs["residential"] == 387432
    assert outputs["avg_levels"] == 4.2
    assert outputs["map_path"] == "/tmp/berlin_buildings_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
