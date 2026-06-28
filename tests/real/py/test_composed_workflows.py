"""Runtime integration tests for all 15 composed workflows in osmworkflows_composed.afl.

Compiles all 43 FFL files, then executes each workflow end-to-end through:
  FFL source → compile → MongoStore → Evaluator → AgentPoller → real handlers → completion

Uses Liechtenstein as the default region (tiny ~2MB download, fast processing).

Special cases:
  - TransitAnalysis / TransitAccessibility: compile-only (require external GTFS feed)
  - RoadZoomBuilder: compile-only (requires GraphHopper JAR)

External dependencies:
  - requests (for Geofabrik download)
  - osmium (for PBF parsing)
  - Network access
  - MongoDB (--mongodb flag)

Run:
    pytest tests/real/py/test_composed_workflows.py -v --mongodb
"""

import sys
from pathlib import Path

import pytest
from helpers import (
    EXAMPLE_FW_FILES,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

from facetwork.runtime import ExecutionStatus

# Legacy sys.path manipulation: the package is now installed via pyproject.toml,
_EXAMPLE_ROOT = Path(__file__).parent.parent.parent.parent
if str(_EXAMPLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_ROOT))


# Check required dependencies
try:
    import osmium  # noqa: F401

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False

try:
    import requests  # noqa: F401

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ============================================================================
# All 16 composed workflow names
# ============================================================================

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


# ============================================================================
# Module helpers
# ============================================================================


def _compile_composed():
    """Compile all FFL files with osmworkflows_composed.afl as primary."""
    primary = EXAMPLE_FW_FILES["osmworkflows_composed.ffl"]
    libs = [p for n, p in sorted(EXAMPLE_FW_FILES.items()) if n != "osmworkflows_composed.ffl"]
    return compile_afl_files(primary, *libs)


def _register_handlers(poller):
    """Register all ~270+ OSM handlers with the poller."""
    from osm_geocoder.handlers import register_all_handlers

    register_all_handlers(poller)


# ============================================================================
# Compilation tests (no MongoDB, no network needed)
# ============================================================================


class TestComposedWorkflowsCompilation:
    """Verify all 15 composed workflows compile from FFL source."""

    def test_compile_all_composed_workflows(self):
        """All 43 FFL files compile together and all 15 workflows are present."""
        program = _compile_composed()
        assert program["type"] == "Program"

        for name in ALL_WORKFLOW_NAMES:
            workflow = extract_workflow(program, name)
            assert workflow["name"] == name


# ============================================================================
# Runtime integration tests (require --mongodb + osmium + requests)
# ============================================================================


@pytest.mark.skipif(not HAS_OSMIUM, reason="osmium not installed")
@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestComposedWorkflowsIntegration:
    """Full runtime execution of composed workflows through MongoDB + AgentPoller.

    Each test compiles all FFL files, extracts one workflow, registers all
    handlers, and runs end-to-end with Liechtenstein as the region.
    """

    # ------------------------------------------------------------------
    # Pattern 1: Cache -> Extract -> Visualize (3 steps)
    # ------------------------------------------------------------------

    def test_visualize_bicycle_routes(self, mongo_store, evaluator, poller):
        """VisualizeBicycleRoutes: cache -> bicycle routes -> render map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "VisualizeBicycleRoutes")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["route_count"], int)

    # ------------------------------------------------------------------
    # Pattern 2: Cache -> Extract -> Statistics (3 steps)
    # ------------------------------------------------------------------

    def test_analyze_parks(self, mongo_store, evaluator, poller):
        """AnalyzeParks: cache -> extract parks -> park statistics."""
        program = _compile_composed()
        workflow = extract_workflow(program, "AnalyzeParks")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["total_parks"], int)
        assert isinstance(result.outputs["total_area"], (int, float))
        assert isinstance(result.outputs["national"], int)
        assert isinstance(result.outputs["state"], int)

    # ------------------------------------------------------------------
    # Pattern 3: Cache -> Extract -> Filter -> Visualize (4 steps)
    # ------------------------------------------------------------------

    def test_large_cities_map(self, mongo_store, evaluator, poller):
        """LargeCitiesMap: cache -> cities -> filter by population -> render map.

        Uses min_pop=0 since Liechtenstein has very few large cities.
        """
        program = _compile_composed()
        workflow = extract_workflow(program, "LargeCitiesMap")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein", "min_pop": 0},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["city_count"], int)

    # ------------------------------------------------------------------
    # Pattern 4: Cache -> Multiple Extractions -> Combine Statistics (9 steps)
    # ------------------------------------------------------------------

    def test_transport_overview(self, mongo_store, evaluator, poller):
        """TransportOverview: cache -> 4 route types -> 4 route statistics.

        9 steps total, needs higher max_rounds.
        """
        program = _compile_composed()
        workflow = extract_workflow(program, "TransportOverview")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=100,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["bicycle_km"], (int, float))
        assert isinstance(result.outputs["hiking_km"], (int, float))
        assert isinstance(result.outputs["train_km"], (int, float))
        assert isinstance(result.outputs["bus_routes"], int)

    # ------------------------------------------------------------------
    # Pattern 5: Cache -> Extract -> Filter -> Statistics -> Visualize (5 steps)
    # ------------------------------------------------------------------

    def test_national_parks_analysis(self, mongo_store, evaluator, poller):
        """NationalParksAnalysis: cache -> all parks -> filter national -> stats -> map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "NationalParksAnalysis")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["park_count"], int)
        assert isinstance(result.outputs["total_area"], (int, float))
        assert isinstance(result.outputs["avg_area"], (int, float))

    # ------------------------------------------------------------------
    # Pattern 6: Parameterized City Analysis (4 steps)
    # ------------------------------------------------------------------

    def test_city_analysis(self, mongo_store, evaluator, poller):
        """CityAnalysis: cache -> cities -> population stats -> render map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "CityAnalysis")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein", "min_population": 0},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["large_cities"], int)
        assert isinstance(result.outputs["total_pop"], int)

    # ------------------------------------------------------------------
    # Pattern 7: Multi-Layer Visualization (4 steps)
    # ------------------------------------------------------------------

    def test_transport_map(self, mongo_store, evaluator, poller):
        """TransportMap: cache -> bicycle + hiking routes -> render bicycle map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "TransportMap")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)

    # ------------------------------------------------------------------
    # Pattern 8: Boundary Analysis Pipeline (3 steps)
    # ------------------------------------------------------------------

    def test_state_boundaries_with_stats(self, mongo_store, evaluator, poller):
        """StateBoundariesWithStats: cache -> state boundaries -> render map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "StateBoundariesWithStats")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["state_count"], int)

    # ------------------------------------------------------------------
    # Pattern 9: POI Discovery Pipeline (5 steps)
    # ------------------------------------------------------------------

    def test_discover_cities_and_towns(self, mongo_store, evaluator, poller):
        """DiscoverCitiesAndTowns: cache -> cities/towns/villages -> render map."""
        program = _compile_composed()
        workflow = extract_workflow(program, "DiscoverCitiesAndTowns")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["map_path"], str)
        assert isinstance(result.outputs["cities"], int)
        assert isinstance(result.outputs["towns"], int)
        assert isinstance(result.outputs["villages"], int)

    # ------------------------------------------------------------------
    # Pattern 10: Complete Regional Analysis (8 steps)
    # ------------------------------------------------------------------

    def test_regional_analysis(self, mongo_store, evaluator, poller):
        """RegionalAnalysis: cache -> parks/routes/cities -> stats for each -> map.

        8 steps total, needs higher max_rounds.
        """
        program = _compile_composed()
        workflow = extract_workflow(program, "RegionalAnalysis")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein"},
            max_rounds=100,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["parks_count"], int)
        assert isinstance(result.outputs["parks_area"], (int, float))
        assert isinstance(result.outputs["routes_km"], (int, float))
        assert isinstance(result.outputs["cities_count"], int)
        assert isinstance(result.outputs["map_path"], str)

    # ------------------------------------------------------------------
    # Pattern 11: Cache -> Validate -> Summary (3 steps)
    # ------------------------------------------------------------------

    def test_validate_and_summarize(self, mongo_store, evaluator, poller):
        """ValidateAndSummarize: cache -> validate cache -> validation summary."""
        program = _compile_composed()
        workflow = extract_workflow(program, "ValidateAndSummarize")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein", "output_dir": "/tmp"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["total"], int)
        assert isinstance(result.outputs["valid"], int)
        assert isinstance(result.outputs["invalid"], int)
        assert isinstance(result.outputs["output_path"], str)

    # ------------------------------------------------------------------
    # Pattern 12: Cache -> Local Verify -> Summary (OSMOSE, 3 steps)
    # ------------------------------------------------------------------

    def test_osmose_quality_check(self, mongo_store, evaluator, poller):
        """OsmoseQualityCheck: cache -> OSMOSE verify all -> compute summary."""
        program = _compile_composed()
        workflow = extract_workflow(program, "OsmoseQualityCheck")
        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"region": "Liechtenstein", "output_dir": "/tmp"},
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert isinstance(result.outputs["total_issues"], int)
        assert isinstance(result.outputs["geometry_issues"], int)
        assert isinstance(result.outputs["tag_issues"], int)
        assert isinstance(result.outputs["reference_issues"], int)
        assert isinstance(result.outputs["tag_coverage_pct"], (int, float))
        assert isinstance(result.outputs["output_path"], str)

    # ------------------------------------------------------------------
    # Pattern 13: GTFS Transit Analysis (compile-only)
    # Requires a real GTFS feed URL — not available in standard test env.
    # ------------------------------------------------------------------

    def test_transit_analysis_compiles(self):
        """TransitAnalysis compiles (runtime skipped: requires GTFS feed URL)."""
        program = _compile_composed()
        workflow = extract_workflow(program, "TransitAnalysis")
        assert workflow["name"] == "TransitAnalysis"
        assert len(workflow["body"]["steps"]) == 4

    # ------------------------------------------------------------------
    # Pattern 14: GTFS Transit Accessibility (compile-only)
    # Requires a real GTFS feed URL — not available in standard test env.
    # ------------------------------------------------------------------

    def test_transit_accessibility_compiles(self):
        """TransitAccessibility compiles (runtime skipped: requires GTFS feed URL)."""
        program = _compile_composed()
        workflow = extract_workflow(program, "TransitAccessibility")
        assert workflow["name"] == "TransitAccessibility"
        assert len(workflow["body"]["steps"]) == 2

    # ------------------------------------------------------------------
    # Pattern 15: Low-Zoom Road Infrastructure Builder (compile-only)
    # Requires GraphHopper JAR for graph building.
    # ------------------------------------------------------------------

    def test_road_zoom_builder_compiles(self):
        """RoadZoomBuilder compiles (runtime skipped: requires GraphHopper JAR)."""
        program = _compile_composed()
        workflow = extract_workflow(program, "RoadZoomBuilder")
        assert workflow["name"] == "RoadZoomBuilder"
        assert len(workflow["body"]["steps"]) == 2
