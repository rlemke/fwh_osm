#!/usr/bin/env python3
"""Example: Find parks and protected areas in Brazil.

Demonstrates a 4-step workflow using park extraction and statistics:
  1. Resolve "Brazil" to the Brazilian OSM data extract
  2. Extract all parks and protected areas from the OSM data
  3. Compute park statistics by type
  4. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_brazil_parks.py
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
                            "name": "Parks",
                            "declarations": [
                                _ef(
                                    "ExtractParks",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "park_type", "type": "String"},
                                        {"name": "protect_classes", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "ParkFeatures"}],
                                ),
                                _ef(
                                    "ParkStatistics",
                                    [{"name": "input_path", "type": "String"}],
                                    [{"name": "stats", "type": "ParkStats"}],
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
# Workflow AST - a 4-step pipeline: resolve, extract parks, stats, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow ParkMapByRegion(
        region: String,
        park_type: String = "all",
        prefer_continent: String = "",
        title: String = "Parks and Protected Areas",
        color: String = "#27ae60"
    ) => (map_path: String, total_parks: Long, total_area_km2: Double,
          national_parks: Long, state_parks: Long, nature_reserves: Long,
          region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        parks = ExtractParks(cache = resolved.cache, park_type = $.park_type)
        stats = ParkStatistics(input_path = parks.result.output_path)
        map = RenderMap(
            geojson_path = parks.result.output_path,
            title = $.title,
            color = $.color
        )
        yield ParkMapByRegion(
            map_path = map.result.output_path,
            total_parks = stats.stats.total_parks,
            total_area_km2 = stats.stats.total_area_km2,
            national_parks = stats.stats.national_parks,
            state_parks = stats.stats.state_parks,
            nature_reserves = stats.stats.nature_reserves,
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
            if wf["name"] == "ParkMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Brazilian park and protected area data with realistic statistics.
# ---------------------------------------------------------------------------

BRAZIL_STATS = {
    "total_parks": 2446,
    "total_area_km2": 2518732.4,
    "national_parks": 74,
    "state_parks": 236,
    "nature_reserves": 892,
    "other_protected": 1244,
    "park_type": "all",
}

NOTABLE_PARKS = {
    "National Parks (Amazon)": [
        {
            "name": "Parque Nacional do Jau",
            "type": "national",
            "area_km2": 23672,
            "state": "Amazonas",
            "biome": "Amazon",
        },
        {
            "name": "Parque Nacional da Amazonia",
            "type": "national",
            "area_km2": 10707,
            "state": "Para",
            "biome": "Amazon",
        },
        {
            "name": "Parque Nacional do Pico da Neblina",
            "type": "national",
            "area_km2": 22564,
            "state": "Amazonas",
            "biome": "Amazon",
        },
        {
            "name": "Parque Nacional Montanhas do Tumucumaque",
            "type": "national",
            "area_km2": 38874,
            "state": "Amapa",
            "biome": "Amazon",
        },
    ],
    "National Parks (Atlantic Forest)": [
        {
            "name": "Parque Nacional da Tijuca",
            "type": "national",
            "area_km2": 40,
            "state": "Rio de Janeiro",
            "biome": "Atlantic Forest",
        },
        {
            "name": "Parque Nacional do Iguacu",
            "type": "national",
            "area_km2": 1852,
            "state": "Parana",
            "biome": "Atlantic Forest",
        },
        {
            "name": "Parque Nacional da Serra da Bocaina",
            "type": "national",
            "area_km2": 1040,
            "state": "Sao Paulo",
            "biome": "Atlantic Forest",
        },
    ],
    "National Parks (Cerrado & Caatinga)": [
        {
            "name": "Parque Nacional da Chapada dos Veadeiros",
            "type": "national",
            "area_km2": 2404,
            "state": "Goias",
            "biome": "Cerrado",
        },
        {
            "name": "Parque Nacional das Emas",
            "type": "national",
            "area_km2": 1318,
            "state": "Goias",
            "biome": "Cerrado",
        },
        {
            "name": "Parque Nacional da Chapada Diamantina",
            "type": "national",
            "area_km2": 1524,
            "state": "Bahia",
            "biome": "Caatinga",
        },
    ],
    "Nature Reserves": [
        {
            "name": "Reserva Biologica do Rio Trombetas",
            "type": "nature_reserve",
            "area_km2": 3850,
            "state": "Para",
            "biome": "Amazon",
        },
        {
            "name": "Estacao Ecologica de Maraca",
            "type": "nature_reserve",
            "area_km2": 1013,
            "state": "Roraima",
            "biome": "Amazon",
        },
        {
            "name": "Reserva Biologica do Gurupi",
            "type": "nature_reserve",
            "area_km2": 2715,
            "state": "Maranhao",
            "biome": "Amazon",
        },
    ],
}

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/south-america/brazil-latest.osm.pbf",
            "path": "/tmp/osm-cache/south-america/brazil-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 1876543210,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "Brazil",
            "region_namespace": "osm.cache.SouthAmerica",
            "continent": "SouthAmerica",
            "geofabrik_path": "south-america/brazil",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractParks": lambda p: {
        "result": {
            "output_path": "/tmp/brazil_parks.geojson",
            "feature_count": BRAZIL_STATS["total_parks"],
            "park_type": p.get("park_type", "all"),
            "protect_classes": p.get("protect_classes", "*"),
            "total_area_km2": BRAZIL_STATS["total_area_km2"],
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "ParkStatistics": lambda p: {
        "stats": BRAZIL_STATS,
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/brazil_parks_map.html",
            "format": "html",
            "feature_count": BRAZIL_STATS["total_parks"],
            "bounds": "-33.75,-73.99,5.27,-34.79",
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
    """Run the Brazil parks workflow end-to-end with mock handlers."""
    print("Compiling ParkMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: ParkMapByRegion(region="Brazil")')
    print("  Pipeline: ResolveRegion -> ExtractParks -> ParkStatistics -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Brazil",
            "title": "Brazilian Parks and Protected Areas",
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
    print("RESULTS: Brazilian Parks and Protected Areas")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total protected areas:  {outputs.get('total_parks'):,}")
    print(f"  Total protected area:   {outputs.get('total_area_km2'):,.1f} km2")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show park type breakdown
    types = [
        ("National Parks", outputs.get("national_parks", 0)),
        ("State Parks", outputs.get("state_parks", 0)),
        ("Nature Reserves", outputs.get("nature_reserves", 0)),
    ]
    other = BRAZIL_STATS["other_protected"]
    total = outputs.get("total_parks", 1)
    print("\n  Protected areas by type:")
    for label, count in sorted(types, key=lambda x: x[1], reverse=True):
        pct = 100 * count / total
        bar = "#" * max(1, int(pct))
        print(f"    {label:.<22} {count:>6,}  ({pct:4.1f}%)  {bar}")
    print(
        f"    {'Other protected':.<22} {other:>6,}  ({100 * other / total:4.1f}%)  {'#' * max(1, int(100 * other / total))}"
    )

    # Show area context
    total_area = outputs.get("total_area_km2", 0)
    brazil_area = 8515767.0
    print(
        f"\n  Coverage: {total_area:,.1f} km2 = {100 * total_area / brazil_area:.1f}% of Brazil's total area"
    )

    # Show notable parks by region
    print("\n  Notable parks:")
    for category, parks in NOTABLE_PARKS.items():
        print(f"    {category}:")
        for park in sorted(parks, key=lambda p: p["area_km2"], reverse=True):
            print(f"      {park['name']:.<46} {park['area_km2']:>7,} km2  {park['state']}")

    assert result.success
    assert outputs["region_name"] == "Brazil"
    assert outputs["total_parks"] == 2446
    assert outputs["total_area_km2"] == 2518732.4
    assert outputs["national_parks"] == 74
    assert outputs["state_parks"] == 236
    assert outputs["nature_reserves"] == 892
    assert outputs["map_path"] == "/tmp/brazil_parks_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
