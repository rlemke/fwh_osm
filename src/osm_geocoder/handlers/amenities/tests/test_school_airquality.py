#!/usr/bin/env python3
"""Example: School air quality exposure in London.

Demonstrates a 6-step cross-dataset pipeline combining OSM with OpenAQ:
  1. Resolve "London" to the England OSM data extract
  2. Extract schools (education amenities) from OSM
  3. Fetch air quality stations from OpenAQ (parallel with step 2)
  4. Correlate each school with its nearest air quality sensor
  5. Compute aggregate exposure statistics
  6. Render a color-coded exposure map

Uses mock handlers (no network calls). Run from the repo root:

    PYTHONPATH=. python examples/osm-geocoder/tests/mocked/py/test_school_airquality.py
"""

import math

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
                            ],
                        },
                        {
                            "type": "Namespace",
                            "name": "AirQuality",
                            "declarations": [
                                _ef(
                                    "FetchAirQuality",
                                    [
                                        {"name": "bbox", "type": "String"},
                                        {"name": "parameter", "type": "String"},
                                        {"name": "radius_m", "type": "Long"},
                                    ],
                                    [{"name": "result", "type": "AirQualityResult"}],
                                ),
                                _ef(
                                    "CorrelateSchoolAirQuality",
                                    [
                                        {"name": "schools_path", "type": "String"},
                                        {"name": "air_quality_path", "type": "String"},
                                        {"name": "max_distance_km", "type": "Double"},
                                    ],
                                    [{"name": "result", "type": "ExposureCorrelationResult"}],
                                ),
                                _ef(
                                    "ExposureStatistics",
                                    [{"name": "input_path", "type": "String"}],
                                    [{"name": "stats", "type": "ExposureStats"}],
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
# Workflow FFL - 6-step pipeline: resolve, schools, air quality, correlate,
# stats, map. Steps 2 and 3 depend only on step 1 (parallel-eligible).
# ---------------------------------------------------------------------------

WORKFLOW_AFL = """\
namespace osm.SchoolAirQuality {
    workflow SchoolAirQualityMap(
        region: String,
        prefer_continent: String = "",
        parameter: String = "pm25",
        radius_m: Long = 25000,
        max_distance_km: Double = 10,
        title: String = "School Air Quality Exposure",
        color: String = "#e74c3c"
    ) => (
        map_path: String,
        region_name: String,
        school_count: Long,
        station_count: Long,
        high_exposure: Long,
        medium_exposure: Long,
        low_exposure: Long,
        avg_pm25: Double,
        max_pm25: Double
    ) andThen {
        resolved = ResolveRegion(
            name = $.region,
            prefer_continent = $.prefer_continent
        )
        schools = ExtractAmenities(
            cache = resolved.cache,
            category = "education"
        )
        air = FetchAirQuality(
            bbox = resolved.resolution.geofabrik_path,
            parameter = $.parameter,
            radius_m = $.radius_m
        )
        correlated = CorrelateSchoolAirQuality(
            schools_path = schools.result.output_path,
            air_quality_path = air.result.output_path,
            max_distance_km = $.max_distance_km
        )
        stats = ExposureStatistics(
            input_path = correlated.result.output_path
        )
        map = RenderMap(
            geojson_path = correlated.result.output_path,
            title = $.title,
            color = $.color
        )
        yield SchoolAirQualityMap(
            map_path = map.result.output_path,
            region_name = resolved.resolution.matched_name,
            school_count = stats.stats.total_schools,
            station_count = air.result.station_count,
            high_exposure = stats.stats.high_count,
            medium_exposure = stats.stats.medium_count,
            low_exposure = stats.stats.low_count,
            avg_pm25 = stats.stats.avg_pm25,
            max_pm25 = stats.stats.max_pm25
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
            if wf["name"] == "SchoolAirQualityMap":
                return wf
    raise RuntimeError("Workflow not found in compiled output")


# ---------------------------------------------------------------------------
# Mock data — 8 London schools and 6 air quality stations with PM2.5 values
# ---------------------------------------------------------------------------

LONDON_SCHOOLS = [
    {"name": "Westminster Academy", "lat": 51.5194, "lon": -0.1810},
    {"name": "City of London School", "lat": 51.5095, "lon": -0.0987},
    {"name": "Hackney Free School", "lat": 51.5462, "lon": -0.0550},
    {"name": "Greenwich Free School", "lat": 51.4769, "lon": -0.0005},
    {"name": "Kensington Aldridge Academy", "lat": 51.5133, "lon": -0.2056},
    {"name": "Mossbourne Academy", "lat": 51.5500, "lon": -0.0596},
    {"name": "Southbank Intl School", "lat": 51.5022, "lon": -0.1140},
    {"name": "Pimlico Academy", "lat": 51.4890, "lon": -0.1350},
]

AIR_QUALITY_STATIONS = [
    {"name": "Marylebone Road", "lat": 51.5225, "lon": -0.1546, "pm25": 38.2},
    {"name": "Bloomsbury", "lat": 51.5222, "lon": -0.1259, "pm25": 22.5},
    {"name": "Tower Hamlets Roadside", "lat": 51.5225, "lon": -0.0422, "pm25": 28.1},
    {"name": "Greenwich Eltham", "lat": 51.4527, "lon": 0.0705, "pm25": 12.3},
    {"name": "Kensington", "lat": 51.4954, "lon": -0.1984, "pm25": 31.7},
    {"name": "Lambeth Brixton", "lat": 51.4649, "lon": -0.1147, "pm25": 19.4},
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in km between two points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _correlate_mock(schools: list[dict], stations: list[dict]) -> list[dict]:
    """Correlate each school with its nearest station (mock logic)."""
    results = []
    for school in schools:
        best_station = None
        best_distance = float("inf")
        best_pm25 = None
        for station in stations:
            dist = _haversine_km(school["lat"], school["lon"], station["lat"], station["lon"])
            if dist < best_distance:
                best_distance = dist
                best_station = station["name"]
                best_pm25 = station["pm25"]
        exposure = "high" if best_pm25 >= 35 else ("medium" if best_pm25 >= 15 else "low")
        results.append(
            {
                "school": school["name"],
                "nearest_station": best_station,
                "distance_km": round(best_distance, 2),
                "pm25": best_pm25,
                "exposure": exposure,
            }
        )
    return results


# Pre-compute correlation for assertions
CORRELATIONS = _correlate_mock(LONDON_SCHOOLS, AIR_QUALITY_STATIONS)
EXPECTED_HIGH = sum(1 for c in CORRELATIONS if c["exposure"] == "high")
EXPECTED_MEDIUM = sum(1 for c in CORRELATIONS if c["exposure"] == "medium")
EXPECTED_LOW = sum(1 for c in CORRELATIONS if c["exposure"] == "low")
EXPECTED_PM25_VALUES = [c["pm25"] for c in CORRELATIONS]
EXPECTED_AVG_PM25 = round(sum(EXPECTED_PM25_VALUES) / len(EXPECTED_PM25_VALUES), 2)
EXPECTED_MAX_PM25 = max(EXPECTED_PM25_VALUES)


# ---------------------------------------------------------------------------
# Mock handlers
# ---------------------------------------------------------------------------

MOCK_HANDLERS = {
    "ResolveRegion": lambda p: {
        "cache": {
            "url": "https://download.geofabrik.de/europe/great-britain/england-latest.osm.pbf",
            "path": "/tmp/osm-cache/europe/great-britain/england-latest.osm.pbf",
            "date": "2026-02-08T10:00:00+00:00",
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
    "ExtractAmenities": lambda p: {
        "result": {
            "output_path": "/tmp/london_schools.geojson",
            "feature_count": len(LONDON_SCHOOLS),
            "amenity_category": p.get("category", "all"),
            "amenity_types": "school,university,library,kindergarten",
            "format": "geojson",
            "extraction_date": "2026-02-08T10:00:01+00:00",
        },
    },
    "FetchAirQuality": lambda p: {
        "result": {
            "output_path": "/tmp/london_airquality.geojson",
            "station_count": len(AIR_QUALITY_STATIONS),
            "parameter": p.get("parameter", "pm25"),
            "avg_value": round(
                sum(s["pm25"] for s in AIR_QUALITY_STATIONS) / len(AIR_QUALITY_STATIONS), 2
            ),
            "unit": "µg/m³",
            "format": "GeoJSON",
        },
    },
    "CorrelateSchoolAirQuality": lambda p: {
        "result": {
            "output_path": "/tmp/london_school_exposure.geojson",
            "school_count": len(LONDON_SCHOOLS),
            "matched_count": len(LONDON_SCHOOLS),
            "high_exposure": EXPECTED_HIGH,
            "medium_exposure": EXPECTED_MEDIUM,
            "low_exposure": EXPECTED_LOW,
            "avg_pm25": EXPECTED_AVG_PM25,
            "format": "GeoJSON",
        },
    },
    "ExposureStatistics": lambda p: {
        "stats": {
            "total_schools": len(LONDON_SCHOOLS),
            "matched_schools": len(LONDON_SCHOOLS),
            "high_count": EXPECTED_HIGH,
            "medium_count": EXPECTED_MEDIUM,
            "low_count": EXPECTED_LOW,
            "high_pct": round(EXPECTED_HIGH / len(LONDON_SCHOOLS) * 100, 1),
            "medium_pct": round(EXPECTED_MEDIUM / len(LONDON_SCHOOLS) * 100, 1),
            "low_pct": round(EXPECTED_LOW / len(LONDON_SCHOOLS) * 100, 1),
            "avg_pm25": EXPECTED_AVG_PM25,
            "max_pm25": EXPECTED_MAX_PM25,
            "min_pm25": min(EXPECTED_PM25_VALUES),
        },
    },
    "RenderMap": lambda p: {
        "result": {
            "output_path": "/tmp/london_school_airquality_map.html",
            "format": "html",
            "feature_count": len(LONDON_SCHOOLS),
            "bounds": "51.45,-0.21,51.55,0.07",
            "title": p.get("title", "Map"),
            "extraction_date": "2026-02-08T10:00:05+00:00",
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
    """Run the London school air quality workflow end-to-end with mock handlers."""
    print("Compiling SchoolAirQualityMap from FFL source...")
    workflow_ast = compile_workflow()
    print("  OK\n")

    store = MemoryStore()
    evaluator = Evaluator(persistence=store, telemetry=Telemetry(enabled=False))

    # 1. Execute workflow — pauses at the first event step (ResolveRegion)
    print('Executing: SchoolAirQualityMap(region="London")')
    print(
        "  Pipeline: ResolveRegion -> [ExtractAmenities, FetchAirQuality] "
        "-> CorrelateSchoolAirQuality -> ExposureStatistics -> RenderMap\n"
    )

    result = evaluator.execute(
        workflow_ast,
        inputs={
            "region": "London",
            "title": "London School Air Quality",
            "color": "#e74c3c",
        },
        program_ast=PROGRAM_AST,
    )
    assert result.status == ExecutionStatus.PAUSED, f"Expected PAUSED, got {result.status}"

    # 2. Process event steps one at a time
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
    print(f"\n{'=' * 70}")
    print("RESULTS: London School Air Quality Exposure")
    print(f"{'=' * 70}")
    print(f"  Region resolved:        {outputs.get('region_name')}")
    print(f"  Schools analysed:       {outputs.get('school_count')}")
    print(f"  Air quality stations:   {outputs.get('station_count')}")
    print(f"  Avg PM2.5:              {outputs.get('avg_pm25')} µg/m³")
    print(f"  Max PM2.5:              {outputs.get('max_pm25')} µg/m³")
    print(f"  Map output:             {outputs.get('map_path')}")

    # Exposure breakdown
    high = outputs.get("high_exposure", 0)
    med = outputs.get("medium_exposure", 0)
    low = outputs.get("low_exposure", 0)
    total = high + med + low
    print("\n  Exposure breakdown (WHO PM2.5 thresholds):")
    print(
        f"    HIGH   (>= 35 µg/m³):   {high:>2} schools  ({high / total * 100:.0f}%)  {'!' * high}"
    )
    print(f"    MEDIUM (15-35 µg/m³):    {med:>2} schools  ({med / total * 100:.0f}%)  {'~' * med}")
    print(f"    LOW    (< 15 µg/m³):     {low:>2} schools  ({low / total * 100:.0f}%)  {'.' * low}")

    # Per-school detail table
    print(f"\n  {'School':<30} {'Station':<25} {'Dist':>5} {'PM2.5':>6} {'Level':<7}")
    print(f"  {'-' * 30} {'-' * 25} {'-' * 5} {'-' * 6} {'-' * 7}")
    for c in CORRELATIONS:
        level_marker = {"high": "!!!", "medium": " ~ ", "low": " . "}[c["exposure"]]
        print(
            f"  {c['school']:<30} {c['nearest_station']:<25} "
            f"{c['distance_km']:>4.1f}k {c['pm25']:>5.1f}  {level_marker} {c['exposure']}"
        )

    # 4. Assertions
    assert result.success
    assert outputs["region_name"] == "England"
    assert outputs["school_count"] == len(LONDON_SCHOOLS)
    assert outputs["station_count"] == len(AIR_QUALITY_STATIONS)
    assert outputs["high_exposure"] == EXPECTED_HIGH
    assert outputs["medium_exposure"] == EXPECTED_MEDIUM
    assert outputs["low_exposure"] == EXPECTED_LOW
    assert outputs["avg_pm25"] == EXPECTED_AVG_PM25
    assert outputs["max_pm25"] == EXPECTED_MAX_PM25
    assert outputs["map_path"] == "/tmp/london_school_airquality_map.html"

    print(f"\nAll assertions passed. ({step_num} event steps processed)")
    print(f"  Expected: {EXPECTED_HIGH} high, {EXPECTED_MEDIUM} medium, {EXPECTED_LOW} low")


if __name__ == "__main__":
    main()
