#!/usr/bin/env python3
"""Example: Find points of interest in London.

Demonstrates a 3-step workflow using the POI extraction facet:
  1. Resolve "London" to the England OSM data extract
  2. Extract all points of interest from the OSM data
  3. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_london_pois.py
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
                            "name": "POIs",
                            "declarations": [
                                _ef(
                                    "POI",
                                    [{"name": "cache", "type": "OSMCache"}],
                                    [{"name": "pois", "type": "OSMCache"}],
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
# Workflow AST - a 3-step pipeline: resolve region, extract POIs, render map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow POIMapByRegion(
        region: String,
        prefer_continent: String = "",
        title: String = "Points of Interest",
        color: String = "#e74c3c"
    ) => (map_path: String, poi_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        pois = POI(cache = resolved.cache)
        map = RenderMap(
            geojson_path = pois.pois.path,
            title = $.title,
            color = $.color
        )
        yield POIMapByRegion(
            map_path = map.result.output_path,
            poi_count = pois.pois.size,
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
            if wf["name"] == "POIMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# London POI data across major categories.
# ---------------------------------------------------------------------------

LONDON_POIS = {
    "Museums & Galleries": [
        "British Museum",
        "Natural History Museum",
        "Tate Modern",
        "Victoria and Albert Museum",
        "Science Museum",
        "National Gallery",
        "Imperial War Museum",
        "Museum of London",
    ],
    "Historic Sites": [
        "Tower of London",
        "Westminster Abbey",
        "Buckingham Palace",
        "St Paul's Cathedral",
        "Hampton Court Palace",
        "Greenwich Observatory",
        "Houses of Parliament",
        "Kensington Palace",
    ],
    "Parks & Gardens": [
        "Hyde Park",
        "Regent's Park",
        "Kew Gardens",
        "Richmond Park",
        "Greenwich Park",
        "St James's Park",
        "Hampstead Heath",
        "Victoria Park",
    ],
    "Transport Hubs": [
        "King's Cross St Pancras",
        "Paddington Station",
        "Waterloo Station",
        "Liverpool Street Station",
        "Victoria Station",
        "Heathrow Airport",
        "London City Airport",
        "Euston Station",
    ],
    "Markets": [
        "Borough Market",
        "Camden Market",
        "Portobello Road Market",
        "Covent Garden Market",
        "Brick Lane Market",
        "Columbia Road Flower Market",
    ],
    "Entertainment": [
        "O2 Arena",
        "Royal Albert Hall",
        "Shakespeare's Globe",
        "West End Theatre District",
        "Wembley Stadium",
        "Lord's Cricket Ground",
    ],
}

TOTAL_POIS = sum(len(v) for v in LONDON_POIS.values())

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/great-britain/england-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/great-britain/england-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 987654321,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "England",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/great-britain/england",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "POI": lambda p: {
        "pois": {
            "url": p["cache"]["url"],
            "path": "/tmp/london_pois.geojson",
            "date": "2026-02-06T12:00:01+00:00",
            "size": TOTAL_POIS,
            "wasInCache": False,
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/london_pois_map.html",
            "format": "html",
            "feature_count": TOTAL_POIS,
            "bounds": "51.28,-0.51,51.69,0.33",
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
    """Run the London POI workflow end-to-end with mock handlers."""
    print("Compiling POIMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: POIMapByRegion(region="London")')
    print("  Pipeline: ResolveRegion -> POI -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "London",
            "title": "London Points of Interest",
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
    print("RESULTS: London Points of Interest")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total POIs found:       {outputs.get('poi_count')}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show POIs by category
    print("\n  POIs by category:")
    for category in sorted(LONDON_POIS, key=lambda c: len(LONDON_POIS[c]), reverse=True):
        pois = LONDON_POIS[category]
        print(f"    {category:.<30} {len(pois):>2} places")
        for poi in pois:
            print(f"      {poi}")

    assert result.success
    assert outputs["region_name"] == "England"
    assert outputs["poi_count"] == TOTAL_POIS
    assert outputs["map_path"] == "/tmp/london_pois_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
