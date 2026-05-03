#!/usr/bin/env python3
"""Example: Find voting locations in South Africa.

Demonstrates a 5-step workflow combining amenity extraction with boundary data:
  1. Resolve "South Africa" to the South African OSM data extract
  2. Extract amenities tagged as polling stations, community halls, schools
  3. Extract ward boundaries (admin_level=9 in South Africa)
  4. Filter amenities to those with "voting" or "polling" in their tags
  5. Render an interactive Leaflet map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_southafrica_voting.py
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
                                    "SearchAmenities",
                                    [
                                        {"name": "input_path", "type": "String"},
                                        {"name": "name_pattern", "type": "String"},
                                    ],
                                    [{"name": "result", "type": "AmenityFeatures"}],
                                ),
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "Boundaries",
                            "declarations": [
                                _ef(
                                    "AdminBoundary",
                                    [
                                        {"name": "cache", "type": "OSMCache"},
                                        {"name": "admin_level", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "BoundaryFeatures"}],
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
# Workflow AST - a 5-step pipeline: resolve, extract amenities, extract wards,
# search for polling stations, render map.
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.RegionMap {
    workflow VotingLocationMap(
        region: String,
        search_pattern: String = "poll|vot|election|IEC",
        ward_admin_level: Long = 9,
        prefer_continent: String = "",
        title: String = "Voting Locations",
        color: String = "#16a085"
    ) => (map_path: String, total_amenities: Long, voting_locations: Long,
          ward_count: Long, region_name: String) andThen {
        resolved = ResolveRegion(name = $.region, prefer_continent = $.prefer_continent)
        amenities = ExtractAmenities(cache = resolved.cache, category = "all")
        wards = AdminBoundary(cache = resolved.cache, admin_level = $.ward_admin_level)
        polling = SearchAmenities(
            input_path = amenities.result.output_path,
            name_pattern = $.search_pattern
        )
        map = RenderMap(
            geojson_path = polling.result.output_path,
            title = $.title,
            color = $.color
        )
        yield VotingLocationMap(
            map_path = map.result.output_path,
            total_amenities = amenities.result.feature_count,
            voting_locations = polling.result.feature_count,
            ward_count = wards.result.feature_count,
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
            if wf["name"] == "VotingLocationMap":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock handlers - simulate each pipeline stage without network calls.
# South African voting location and ward boundary data.
# ---------------------------------------------------------------------------

PROVINCES = [
    {"name": "Gauteng", "wards": 508, "stations": 2789, "registered": 7296924},
    {"name": "KwaZulu-Natal", "wards": 828, "stations": 4614, "registered": 5808834},
    {"name": "Western Cape", "wards": 405, "stations": 1573, "registered": 3640857},
    {"name": "Eastern Cape", "wards": 692, "stations": 4284, "registered": 3461765},
    {"name": "Limpopo", "wards": 543, "stations": 3163, "registered": 2835432},
    {"name": "Mpumalanga", "wards": 402, "stations": 1804, "registered": 2183456},
    {"name": "North West", "wards": 383, "stations": 1665, "registered": 1897234},
    {"name": "Free State", "wards": 305, "stations": 1516, "registered": 1587123},
    {"name": "Northern Cape", "wards": 194, "stations": 706, "registered": 672345},
]

TOTAL_WARDS = sum(p["wards"] for p in PROVINCES)
TOTAL_STATIONS = sum(p["stations"] for p in PROVINCES)
TOTAL_REGISTERED = sum(p["registered"] for p in PROVINCES)

SAMPLE_STATIONS = [
    {
        "name": "Soweto Community Hall",
        "type": "community_centre",
        "province": "Gauteng",
        "ward": "Ward 42",
        "capacity": 2400,
    },
    {
        "name": "Orlando Stadium Precinct",
        "type": "polling_station",
        "province": "Gauteng",
        "ward": "Ward 38",
        "capacity": 3100,
    },
    {
        "name": "Durban City Hall",
        "type": "government",
        "province": "KwaZulu-Natal",
        "ward": "Ward 28",
        "capacity": 1800,
    },
    {
        "name": "Khayelitsha Community Hall",
        "type": "community_centre",
        "province": "Western Cape",
        "ward": "Ward 94",
        "capacity": 2200,
    },
    {
        "name": "Nelson Mandela Bay Municipality",
        "type": "government",
        "province": "Eastern Cape",
        "ward": "Ward 1",
        "capacity": 1500,
    },
    {
        "name": "Polokwane Civic Centre",
        "type": "government",
        "province": "Limpopo",
        "ward": "Ward 15",
        "capacity": 1900,
    },
    {
        "name": "Mbombela Stadium Hall",
        "type": "community_centre",
        "province": "Mpumalanga",
        "ward": "Ward 22",
        "capacity": 2600,
    },
    {
        "name": "Rustenburg Civic Centre",
        "type": "government",
        "province": "North West",
        "ward": "Ward 7",
        "capacity": 1200,
    },
    {
        "name": "Bloemfontein City Hall",
        "type": "government",
        "province": "Free State",
        "ward": "Ward 11",
        "capacity": 1600,
    },
    {
        "name": "Kimberley Town Hall",
        "type": "government",
        "province": "Northern Cape",
        "ward": "Ward 3",
        "capacity": 800,
    },
]

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/africa/south-africa-latest.osm.pbf",
            "path": "/tmp/osm-cache/africa/south-africa-latest.osm.pbf",
            "date": "2026-02-06T12:00:00+00:00",
            "size": 456789012,
            "wasInCache": True,
        },
        "resolution": {
            "query": p["name"],
            "matched_name": "SouthAfrica",
            "region_namespace": "osm.cache.Africa",
            "continent": "Africa",
            "geofabrik_path": "africa/south-africa",
            "is_ambiguous": False,
            "disambiguation": "",
        },
    },
    "ExtractAmenities": lambda p: {
        "result": {
            "output_path": "/tmp/southafrica_amenities.geojson",
            "feature_count": 87432,
            "amenity_category": p.get("category", "all"),
            "amenity_types": "community_centre,school,government,church,hall,...",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:01+00:00",
        },
    },
    "AdminBoundary": lambda p: {
        "result": {
            "output_path": f"/tmp/southafrica_admin{p.get('admin_level', 9)}.geojson",
            "feature_count": TOTAL_WARDS,
            "boundary_type": "administrative",
            "admin_levels": str(p.get("admin_level", 9)),
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:02+00:00",
        },
    },
    "SearchAmenities": lambda p: {
        "result": {
            "output_path": "/tmp/southafrica_polling_stations.geojson",
            "feature_count": TOTAL_STATIONS,
            "amenity_category": "voting",
            "amenity_types": "polling_station,community_centre,school,government",
            "format": "geojson",
            "extraction_date": "2026-02-06T12:00:03+00:00",
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/southafrica_voting_map.html",
            "format": "html",
            "feature_count": TOTAL_STATIONS,
            "bounds": "-34.83,16.46,-22.13,32.89",
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
    """Run the South Africa voting location workflow end-to-end with mock handlers."""
    print("Compiling VotingLocationMap from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow - pauses at the first event step (ResolveRegion)
    print('Executing: VotingLocationMap(region="South Africa")')
    print("  Pipeline: ResolveRegion -> ExtractAmenities -> AdminBoundary")
    print("            -> SearchAmenities -> RenderMap\n")

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "South Africa",
            "search_pattern": "poll|vot|election|IEC",
            "ward_admin_level": 9,
            "title": "South Africa Voting Locations and Electoral Wards",
            "color": "#16a085",
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

        step = store.get_step(step_id)
        params = {k: v.value for k, v in step.attributes.params.items()}
        print(f"  Step {step_num}: {facet_short}")

        handler_result = handler(params)
        evaluator.continue_step(step_id, handler_result)
        result = evaluator.resume(result.workflow_id, workflow_ast, PROGRAM_AST)

    assert result.status == ExecutionStatus.COMPLETED, f"Expected COMPLETED, got {result.status}"

    # 3. Show results
    outputs = result.outputs
    print(f"\n{'=' * 60}")
    print("RESULTS: South Africa Voting Locations")
    print(f"{'=' * 60}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Total amenities:        {outputs.get('total_amenities'):,}")
    print(f"  Voting locations:       {outputs.get('voting_locations'):,}")
    print(f"  Electoral wards:        {outputs.get('ward_count'):,}")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Show provincial breakdown
    print("\n  Electoral data by province:")
    print(
        f"  {'Province':<22} {'Wards':>6} {'Stations':>10} {'Registered':>12} {'Voters/Station':>15}"
    )
    print(f"  {'-' * 22} {'-' * 6} {'-' * 10} {'-' * 12} {'-' * 15}")
    for prov in sorted(PROVINCES, key=lambda p: p["registered"], reverse=True):
        vps = prov["registered"] // prov["stations"]
        print(
            f"  {prov['name']:<22} {prov['wards']:>6,} {prov['stations']:>10,} "
            f"{prov['registered']:>12,} {vps:>15,}"
        )
    print(f"  {'-' * 22} {'-' * 6} {'-' * 10} {'-' * 12} {'-' * 15}")
    avg_vps = TOTAL_REGISTERED // TOTAL_STATIONS
    print(
        f"  {'Total':<22} {TOTAL_WARDS:>6,} {TOTAL_STATIONS:>10,} "
        f"{TOTAL_REGISTERED:>12,} {avg_vps:>15,}"
    )

    # Show sample voting stations
    print("\n  Sample voting stations:")
    for station in SAMPLE_STATIONS:
        print(
            f"    {station['name']:.<42} {station['type']:<20} "
            f"{station['province']:<16} {station['ward']}"
        )

    # Show coverage ratio
    stations_per_ward = TOTAL_STATIONS / TOTAL_WARDS
    print("\n  Coverage:")
    print(f"    Avg stations per ward:    {stations_per_ward:.1f}")
    print(f"    Avg registered per ward:  {TOTAL_REGISTERED // TOTAL_WARDS:,}")
    print(f"    Avg registered per station:{TOTAL_REGISTERED // TOTAL_STATIONS:,}")

    assert result.success
    assert outputs["region_name"] == "SouthAfrica"
    assert outputs["total_amenities"] == 87432
    assert outputs["voting_locations"] == TOTAL_STATIONS
    assert outputs["ward_count"] == TOTAL_WARDS
    assert outputs["map_path"] == "/tmp/southafrica_voting_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")


if __name__ == "__main__":
    main()
