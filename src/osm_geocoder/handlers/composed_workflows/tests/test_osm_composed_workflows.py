"""Integration tests for all 15 composed workflows in osmworkflows_composed.afl.

Verifies that every workflow compiles correctly and has the expected params,
returns, steps, and cache call targets.  Pure compile-time tests — no MongoDB
or runtime execution required.
"""

from pathlib import Path

import pytest

from facetwork.emitter import emit_dict
from facetwork.parser import FFLParser
from facetwork.source import CompilerInput, FileOrigin, SourceEntry
from facetwork.validator import validate

_OSM_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# Collect all FFL files from root afl/ and handlers/*/ffl/ (skip tests/)
_AFL_BY_NAME: dict[str, Path] = {}
for _p in sorted(_OSM_ROOT.rglob("*.ffl")):
    if "/tests/" not in str(_p):
        _AFL_BY_NAME[_p.name] = _p


def _compile_all() -> dict:
    """Compile all FFL files with osmworkflows_composed.afl as primary."""
    filenames = sorted(_AFL_BY_NAME.keys())

    # Put osmworkflows_composed.afl first as primary
    filenames.remove("osmworkflows_composed.ffl")
    filenames.insert(0, "osmworkflows_composed.ffl")

    entries = []
    for i, name in enumerate(filenames):
        path = _AFL_BY_NAME[name]
        entries.append(
            SourceEntry(
                text=path.read_text(),
                origin=FileOrigin(path=str(path)),
                is_library=(i > 0),
            )
        )

    compiler_input = CompilerInput(
        primary_sources=[entries[0]],
        library_sources=entries[1:],
    )

    parser = FFLParser()
    program_ast, _registry = parser.parse_sources(compiler_input)

    result = validate(program_ast)
    if result.errors:
        messages = "; ".join(str(e) for e in result.errors)
        raise ValueError(f"Validation errors: {messages}")

    return emit_dict(program_ast, include_locations=False)


def _find_wf(node: dict, name: str) -> dict | None:
    """Recursively search compiled program for a workflow by name."""
    for decl in node.get("declarations", []):
        if decl.get("type") == "WorkflowDecl" and decl["name"] == name:
            return decl
        if decl.get("type") == "Namespace":
            found = _find_wf(decl, name)
            if found is not None:
                return found
    return None


def _find_facet(node: dict, name: str) -> dict | None:
    """Recursively search compiled program for a facet by name."""
    for decl in node.get("declarations", []):
        if decl.get("type") == "FacetDecl" and decl["name"] == name:
            return decl
        if decl.get("type") == "Namespace":
            found = _find_facet(decl, name)
            if found is not None:
                return found
    return None


def _step_call_target(step: dict) -> str:
    """Extract the fully-qualified call target from a step dict."""
    return step["call"]["target"]


def _param_dict(wf: dict) -> dict[str, str]:
    """Return {name: type} for workflow params."""
    return {p["name"]: p["type"] for p in wf.get("params", [])}


def _return_dict(wf: dict) -> dict[str, str]:
    """Return {name: type} for workflow returns."""
    return {r["name"]: r["type"] for r in wf.get("returns", [])}


def _steps(wf: dict) -> list[dict]:
    """Return the list of step dicts from the workflow body."""
    return wf["body"]["steps"]


def _step_names(wf: dict) -> list[str]:
    """Return ordered list of step variable names."""
    return [s["name"] for s in _steps(wf)]


# ---------------------------------------------------------------------------
# Module-scoped fixture — compile once, reuse across all tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def program():
    """Compiled program from all 43 FFL files."""
    return _compile_all()


# ---------------------------------------------------------------------------
# All 16 workflow names for completeness checks
# ---------------------------------------------------------------------------

ALL_WORKFLOW_NAMES = [
    "VisualizeBicycleRoutes",
    "AnalyzeParks",
    "LargeCitiesMap",
    "TransportOverview",
    "NationalParksAnalysis",
    "CityAnalysis",
    "TransportMap",
    "StateBoundariesWithStats",
    "DiscoverCitiesAndTowns",
    "RegionalAnalysis",
    "ValidateAndSummarize",
    "OsmoseQualityCheck",
    "TransitAnalysis",
    "TransitAccessibility",
    "RoadZoomBuilder",
]

# Workflows that use osm.ops.CacheRegion(region = $.region)
_OPERATIONS_CACHE_WORKFLOWS = [
    "VisualizeBicycleRoutes",
    "AnalyzeParks",
    "LargeCitiesMap",
    "TransportOverview",
    "NationalParksAnalysis",
    "CityAnalysis",
    "TransportMap",
    "StateBoundariesWithStats",
    "DiscoverCitiesAndTowns",
    "RegionalAnalysis",
    "ValidateAndSummarize",
    "OsmoseQualityCheck",
    "TransitAccessibility",
    "RoadZoomBuilder",
]

# FromCache facets corresponding to each cache workflow
ALL_FROM_CACHE_FACETS = [
    "VisualizeBicycleRoutesFromCache",
    "AnalyzeParksFromCache",
    "LargeCitiesMapFromCache",
    "TransportOverviewFromCache",
    "NationalParksAnalysisFromCache",
    "CityAnalysisFromCache",
    "TransportMapFromCache",
    "StateBoundariesWithStatsFromCache",
    "DiscoverCitiesAndTownsFromCache",
    "RegionalAnalysisFromCache",
    "ValidateAndSummarizeFromCache",
    "OsmoseQualityCheckFromCache",
    "TransitAccessibilityFromCache",
    "RoadZoomBuilderFromCache",
]


class TestComposedWorkflows:
    """Integration tests for all 15 composed workflows."""

    # ------------------------------------------------------------------
    # Smoke / completeness
    # ------------------------------------------------------------------

    def test_all_workflows_compile(self, program):
        """All FFL files compile together without errors."""
        assert program["type"] == "Program"

    def test_all_15_workflows_present(self, program):
        """Every one of the 15 composed workflows exists in the compiled output."""
        for name in ALL_WORKFLOW_NAMES:
            wf = _find_wf(program, name)
            assert wf is not None, f"Workflow {name!r} not found in compiled program"

    def test_all_from_cache_facets_present(self, program):
        """Every FromCache facet exists in the compiled output."""
        for name in ALL_FROM_CACHE_FACETS:
            f = _find_facet(program, name)
            assert f is not None, f"Facet {name!r} not found in compiled program"

    # ------------------------------------------------------------------
    # Pattern 1: Cache → Extract → Visualize
    # ------------------------------------------------------------------

    def test_visualize_bicycle_routes(self, program):
        wf = _find_wf(program, "VisualizeBicycleRoutes")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {"map_path": "String", "route_count": "Long"}

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "VisualizeBicycleRoutesFromCache"

    def test_visualize_bicycle_routes_from_cache(self, program):
        f = _find_facet(program, "VisualizeBicycleRoutesFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert _return_dict(f) == {"map_path": "String", "route_count": "Long"}
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["routes", "map"]

    # ------------------------------------------------------------------
    # Pattern 2: Cache → Extract → Statistics
    # ------------------------------------------------------------------

    def test_analyze_parks(self, program):
        wf = _find_wf(program, "AnalyzeParks")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {
            "total_parks": "Long",
            "total_area": "Double",
            "national": "Long",
            "state": "Long",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "AnalyzeParksFromCache"

    def test_analyze_parks_from_cache(self, program):
        f = _find_facet(program, "AnalyzeParksFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["parks", "stats"]

    # ------------------------------------------------------------------
    # Pattern 3: Cache → Extract → Filter → Visualize
    # ------------------------------------------------------------------

    def test_large_cities_map(self, program):
        wf = _find_wf(program, "LargeCitiesMap")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String", "min_pop": "Long"}
        assert _return_dict(wf) == {"map_path": "String", "city_count": "Long"}

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "LargeCitiesMapFromCache"

    def test_large_cities_map_from_cache(self, program):
        f = _find_facet(program, "LargeCitiesMapFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache", "min_pop": "Long"}
        assert len(_steps(f)) == 3
        assert _step_names(f) == ["cities", "large", "map"]

    # ------------------------------------------------------------------
    # Pattern 4: Cache → Multiple Extractions → Combine Statistics
    # ------------------------------------------------------------------

    def test_transport_overview(self, program):
        wf = _find_wf(program, "TransportOverview")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {
            "bicycle_km": "Double",
            "hiking_km": "Double",
            "train_km": "Double",
            "bus_routes": "Long",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "TransportOverviewFromCache"

    def test_transport_overview_from_cache(self, program):
        f = _find_facet(program, "TransportOverviewFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 8
        assert _step_names(f) == [
            "bicycle",
            "hiking",
            "train",
            "bus",
            "bicycle_stats",
            "hiking_stats",
            "train_stats",
            "bus_stats",
        ]

    # ------------------------------------------------------------------
    # Pattern 5: Cache → Extract → Filter → Statistics → Visualize
    # ------------------------------------------------------------------

    def test_national_parks_analysis(self, program):
        wf = _find_wf(program, "NationalParksAnalysis")
        assert wf is not None

        params = _param_dict(wf)
        assert params == {"region": "String"}
        assert _return_dict(wf) == {
            "map_path": "String",
            "park_count": "Long",
            "total_area": "Double",
            "avg_area": "Double",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "NationalParksAnalysisFromCache"

    def test_national_parks_analysis_from_cache(self, program):
        f = _find_facet(program, "NationalParksAnalysisFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 4
        assert _step_names(f) == ["all_parks", "national", "stats", "map"]

    # ------------------------------------------------------------------
    # Pattern 6: Parameterized City Analysis
    # ------------------------------------------------------------------

    def test_city_analysis(self, program):
        wf = _find_wf(program, "CityAnalysis")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String", "min_population": "Long"}
        assert _return_dict(wf) == {
            "map_path": "String",
            "large_cities": "Long",
            "total_pop": "Long",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "CityAnalysisFromCache"

    def test_city_analysis_from_cache(self, program):
        f = _find_facet(program, "CityAnalysisFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache", "min_population": "Long"}
        assert len(_steps(f)) == 3
        assert _step_names(f) == ["cities", "stats", "map"]

    # ------------------------------------------------------------------
    # Pattern 7: Multi-Layer Visualization
    # ------------------------------------------------------------------

    def test_transport_map(self, program):
        wf = _find_wf(program, "TransportMap")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {"map_path": "String"}

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "TransportMapFromCache"

    def test_transport_map_from_cache(self, program):
        f = _find_facet(program, "TransportMapFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 3
        assert _step_names(f) == ["bicycle", "hiking", "bicycle_map"]

    # ------------------------------------------------------------------
    # Pattern 8: Boundary Analysis Pipeline
    # ------------------------------------------------------------------

    def test_state_boundaries_with_stats(self, program):
        wf = _find_wf(program, "StateBoundariesWithStats")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {"map_path": "String", "state_count": "Long"}

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "StateBoundariesWithStatsFromCache"

    def test_state_boundaries_from_cache(self, program):
        f = _find_facet(program, "StateBoundariesWithStatsFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["boundaries", "map"]

    # ------------------------------------------------------------------
    # Pattern 9: POI Discovery Pipeline
    # ------------------------------------------------------------------

    def test_discover_cities_and_towns(self, program):
        wf = _find_wf(program, "DiscoverCitiesAndTowns")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {
            "map_path": "String",
            "cities": "Long",
            "towns": "Long",
            "villages": "Long",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "DiscoverCitiesAndTownsFromCache"

    def test_discover_cities_and_towns_from_cache(self, program):
        f = _find_facet(program, "DiscoverCitiesAndTownsFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 4
        assert _step_names(f) == ["city_data", "town_data", "village_data", "map"]

    # ------------------------------------------------------------------
    # Pattern 10: Complete Regional Analysis
    # ------------------------------------------------------------------

    def test_regional_analysis(self, program):
        wf = _find_wf(program, "RegionalAnalysis")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String"}
        assert _return_dict(wf) == {
            "parks_count": "Long",
            "parks_area": "Double",
            "routes_km": "Double",
            "cities_count": "Long",
            "map_path": "String",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "RegionalAnalysisFromCache"

    def test_regional_analysis_from_cache(self, program):
        f = _find_facet(program, "RegionalAnalysisFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache"}
        assert len(_steps(f)) == 7
        assert _step_names(f) == [
            "parks",
            "routes",
            "cities",
            "park_stats",
            "route_stats",
            "city_stats",
            "map",
        ]

    # ------------------------------------------------------------------
    # Pattern 11: Cache → Validate → Summary
    # ------------------------------------------------------------------

    def test_validate_and_summarize(self, program):
        wf = _find_wf(program, "ValidateAndSummarize")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String", "output_dir": "String"}
        assert _return_dict(wf) == {
            "total": "Long",
            "valid": "Long",
            "invalid": "Long",
            "output_path": "String",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "ValidateAndSummarizeFromCache"

    def test_validate_and_summarize_from_cache(self, program):
        f = _find_facet(program, "ValidateAndSummarizeFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache", "output_dir": "String"}
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["validation", "summary"]

    # ------------------------------------------------------------------
    # Pattern 12: Cache → Local Verify → Summary (OSMOSE)
    # ------------------------------------------------------------------

    def test_osmose_quality_check(self, program):
        wf = _find_wf(program, "OsmoseQualityCheck")
        assert wf is not None

        assert _param_dict(wf) == {"region": "String", "output_dir": "String"}
        assert _return_dict(wf) == {
            "total_issues": "Long",
            "geometry_issues": "Long",
            "tag_issues": "Long",
            "reference_issues": "Long",
            "tag_coverage_pct": "Double",
            "output_path": "String",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "OsmoseQualityCheckFromCache"

    def test_osmose_quality_check_from_cache(self, program):
        f = _find_facet(program, "OsmoseQualityCheckFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache", "output_dir": "String"}
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["verify", "summary"]

    # ------------------------------------------------------------------
    # Pattern 13: GTFS Transit Analysis (no cache step — unchanged)
    # ------------------------------------------------------------------

    def test_transit_analysis(self, program):
        wf = _find_wf(program, "TransitAnalysis")
        assert wf is not None

        assert _param_dict(wf) == {"gtfs_url": "String"}
        assert _return_dict(wf) == {
            "agency_name": "String",
            "stop_count": "Long",
            "route_count": "Long",
            "trip_count": "Long",
            "has_shapes": "Boolean",
            "stops_path": "String",
            "routes_path": "String",
        }

        assert len(_steps(wf)) == 4
        assert _step_names(wf) == ["dl", "stops", "routes", "stats"]
        # No cache step — first step downloads GTFS feed
        assert _step_call_target(_steps(wf)[0]) == "osm.Transit.GTFS.DownloadFeed"

    # ------------------------------------------------------------------
    # Pattern 14: GTFS Transit Accessibility
    # ------------------------------------------------------------------

    def test_transit_accessibility(self, program):
        wf = _find_wf(program, "TransitAccessibility")
        assert wf is not None

        assert _param_dict(wf) == {"gtfs_url": "String", "region": "String"}
        assert _return_dict(wf) == {
            "pct_within_400m": "Double",
            "pct_within_800m": "Double",
            "coverage_pct": "Double",
            "gap_cells": "Long",
            "accessibility_path": "String",
            "coverage_path": "String",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "TransitAccessibilityFromCache"

    def test_transit_accessibility_from_cache(self, program):
        f = _find_facet(program, "TransitAccessibilityFromCache")
        assert f is not None
        assert _param_dict(f) == {"cache": "OSMCache", "gtfs_url": "String"}
        assert len(_steps(f)) == 5
        assert _step_names(f) == ["dl", "buildings", "stops", "access", "gaps"]

    # ------------------------------------------------------------------
    # Pattern 15: Low-Zoom Road Infrastructure Builder
    # ------------------------------------------------------------------

    def test_road_zoom_builder(self, program):
        wf = _find_wf(program, "RoadZoomBuilder")
        assert wf is not None

        assert _param_dict(wf) == {
            "region": "String",
            "output_dir": "String",
            "max_concurrent": "Long",
        }
        assert _return_dict(wf) == {
            "total_edges": "Long",
            "selected_edges": "Long",
            "zoom_distribution": "String",
            "csv_path": "String",
            "metrics_path": "String",
        }

        assert len(_steps(wf)) == 2
        assert _step_names(wf) == ["cache", "f"]
        assert _step_call_target(_steps(wf)[0]) == "osm.ops.CacheRegion"
        assert _step_call_target(_steps(wf)[1]) == "RoadZoomBuilderFromCache"

    def test_road_zoom_builder_from_cache(self, program):
        f = _find_facet(program, "RoadZoomBuilderFromCache")
        assert f is not None
        assert _param_dict(f) == {
            "cache": "OSMCache",
            "output_dir": "String",
            "max_concurrent": "Long",
        }
        assert len(_steps(f)) == 2
        assert _step_names(f) == ["graph", "zoom"]

    # ------------------------------------------------------------------
    # Cross-cutting: all generic cache workflows use Operations.Cache
    # ------------------------------------------------------------------

    def test_generic_cache_workflows_use_operations_cache(self, program):
        """Regression guard: 14 workflows must use osm.ops.CacheRegion."""
        for name in _OPERATIONS_CACHE_WORKFLOWS:
            wf = _find_wf(program, name)
            assert wf is not None, f"{name} not found"
            cache_step = _steps(wf)[0]
            assert cache_step["name"] == "cache", (
                f"{name}: first step should be 'cache', got {cache_step['name']!r}"
            )
            assert _step_call_target(cache_step) == "osm.ops.CacheRegion", (
                f"{name}: cache step should target osm.ops.CacheRegion, "
                f"got {_step_call_target(cache_step)!r}"
            )

    def test_cache_workflows_delegate_to_from_cache(self, program):
        """All 14 cache workflows delegate to a FromCache facet as second step."""
        for name in _OPERATIONS_CACHE_WORKFLOWS:
            wf = _find_wf(program, name)
            assert wf is not None, f"{name} not found"
            assert len(_steps(wf)) == 2, (
                f"{name}: expected 2 steps (cache + FromCache), got {len(_steps(wf))}"
            )
            fc_step = _steps(wf)[1]
            assert fc_step["name"] == "f", (
                f"{name}: second step should be 'f', got {fc_step['name']!r}"
            )
            expected_target = f"{name}FromCache"
            assert _step_call_target(fc_step) == expected_target, (
                f"{name}: second step should target {expected_target!r}, "
                f"got {_step_call_target(fc_step)!r}"
            )
