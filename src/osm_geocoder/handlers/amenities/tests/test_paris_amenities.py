#!/usr/bin/env python3
"""Example: Find amenities in Paris.

Demonstrates a 4-step workflow using amenity extraction and statistics:
  1. Resolve "Paris" to the Ile-de-France OSM data extract
  2. Extract all amenities from the OSM data
  3. Compute amenity statistics by category
  4. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_paris_amenities.py
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
                            "name": "Amenities",
                            "declarations": [
                                _ef(
                                    "ExtractAmenities",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "category", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "AmenityFeatures"}],
                                ),
                                _ef(
                                    "AmenityStatistics",
                                    [{"name": "input_path", "type": "String"}],
                                    [{"name": "stats", "type": "AmenityStats"}],
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
# Workflow AST - a 4-step pipeline: resolve, extract amenities, stats, map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow AmenityMapByRegion(
        region: String,
        category: String = "all",
        prefer_continent: String = "",
        title: String = "Amenities",
        color: String = "#1abc9c"
    ) => (map_path: String, total_amenities: Long, region_name: String,
          food: Long, shopping: Long, services: Long, healthcare: Long,
          education: Long, entertainment: Long) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        amenities = ExtractAmenities(cache = resolved.cache, category = $.category)
        stats = AmenityStatistics(input_path = amenities.result.output_path)
        map = RenderMap(
            geojson_path = amenities.result.output_path,
            title = $.title,
            color = $.color
        )
        yield AmenityMapByRegion(
            map_path = map.result.output_path,
            total_amenities = stats.stats.total_amenities,
            region_name = resolved.resolution.matched_name,
            food = stats.stats.food,
            shopping = stats.stats.shopping,
            services = stats.stats.services,
            healthcare = stats.stats.healthcare,
            education = stats.stats.education,
            entertainment = stats.stats.entertainment
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
            if wf["name"] == "AmenityMapByRegion":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# Paris amenity data with realistic counts across categories.
# ---------------------------------------------------------------------------

PARIS_STATS = {
    "total_amenities": 48623,
    "food": 14287,
    "shopping": 8432,
    "services": 6891,
    "healthcare": 3654,
    "education": 2918,
    "entertainment": 1847,
    "transport": 7234,
    "other": 3360,
    "with_name": 38102,
    "with_opening_hours": 21456,
}

PARIS_HIGHLIGHTS = {
    "Food & Drink": [
        {"name": "Le Comptoir du Pantheon", "type": "restaurant", "arrond": "5e"},
        {"name": "Cafe de Flore", "type": "cafe", "arrond": "6e"},
        {"name": "Le Bouillon Chartier", "type": "restaurant", "arrond": "9e"},
        {"name": "Angelina", "type": "cafe", "arrond": "1er"},
        {"name": "Le Relais de l'Entrecote", "type": "restaurant", "arrond": "6e"},
        {"name": "Pink Mamma", "type": "restaurant", "arrond": "10e"},
        {"name": "Du Pain et des Idees", "type": "bakery", "arrond": "10e"},
        {"name": "Le Petit Cler", "type": "cafe", "arrond": "7e"},
    ],
    "Shopping": [
        {"name": "Galeries Lafayette", "type": "mall", "arrond": "9e"},
        {"name": "Le Bon Marche", "type": "mall", "arrond": "7e"},
        {"name": "Marche d'Aligre", "type": "market", "arrond": "12e"},
        {"name": "Merci", "type": "concept_store", "arrond": "3e"},
        {"name": "Shakespeare and Company", "type": "bookshop", "arrond": "5e"},
    ],
    "Healthcare": [
        {"name": "Hopital Pitie-Salpetriere", "type": "hospital", "arrond": "13e"},
        {"name": "Hopital Necker", "type": "hospital", "arrond": "15e"},
        {"name": "Hopital Saint-Louis", "type": "hospital", "arrond": "10e"},
        {"name": "Hopital Cochin", "type": "hospital", "arrond": "14e"},
    ],
    "Education": [
        {"name": "Sorbonne Universite", "type": "university", "arrond": "5e"},
        {"name": "Ecole Normale Superieure", "type": "university", "arrond": "5e"},
        {"name": "Sciences Po", "type": "university", "arrond": "7e"},
        {"name": "Bibliotheque nationale de France", "type": "library", "arrond": "13e"},
    ],
    "Entertainment": [
        {"name": "Opera Garnier", "type": "theatre", "arrond": "9e"},
        {"name": "Le Grand Rex", "type": "cinema", "arrond": "2e"},
        {"name": "Moulin Rouge", "type": "nightclub", "arrond": "18e"},
        {"name": "Comedie-Francaise", "type": "theatre", "arrond": "1er"},
    ],
}

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/france/ile-de-france-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 654321098,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "France",
            "region_namespace": "osm.cache.Europe",
            "continent": "Europe",
            "geofabrik_path": "europe/france/ile-de-france",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractAmenities": lambda p: {
        "result": {
            "output_path": "/tmp/paris_amenities.geojson",
            "feature_count": PARIS_STATS["total_amenities"],
            "amenity_category": p.get("category", "all"),
            "amenity_types": "restaurant,cafe,bar,fast_food,supermarket,bank,hospital,school,...",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "AmenityStatistics": lambda p: {
        "stats": PARIS_STATS,
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/paris_amenities_map.html",
            "format": "html",
            "feature_count": PARIS_STATS["total_amenities"],
            "bounds": "48.81,2.22,48.90,2.47",
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
    """Run the Paris amenity workflow end-to-end with mock handlers."""
    print("Compiling AmenityMapByRegion from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: AmenityMapByRegion(region="Paris")')
    print("  Pipeline: ResolveRegion -> ExtractAmenities -> AmenityStatistics -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "Paris",
            "title": "Paris Amenities",
            "color": "#1abc9c",
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
    print("RESULTS: Paris Amenities")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total amenities:        {outputs.get('total_amenities'):,}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show category breakdown
    categories = [
        ("Food & Drink", outputs.get("food", 0)),
        ("Shopping", outputs.get("shopping", 0)),
        ("Services", outputs.get("services", 0)),
        ("Healthcare", outputs.get("healthcare", 0)),
        ("Education", outputs.get("education", 0)),
        ("Entertainment", outputs.get("entertainment", 0)),
    ]
    print("\n  Amenities by category:")
    for label, count in sorted(categories, key=lambda x: x[1], reverse=True):
        bar = "#" * (count // 500)
        print(f"    {label:.<20} {count:>6,}  {bar}")

    # Show highlighted places
    print("\n  Notable places:")
    for category, places in PARIS_HIGHLIGHTS.items():
        print(f"    {category}:")
        for place in places:
            print(f"      {place['name']:.<38} {place['type']:<16} {place['arrond']}")

    assert result.success
    assert outputs["region_name"] == "France"
    assert outputs["total_amenities"] == 48623
    assert outputs["food"] == 14287
    assert outputs["map_path"] == "/tmp/paris_amenities_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
