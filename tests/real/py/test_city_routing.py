"""Integration test: full 9-step CityRouteMap pipeline.

Compiles osmcityrouting.afl and all its dependencies from real FFL source files,
then runs the full 9-step pipeline with real handlers:

  1. ResolveRegion — real handler (pure Python region resolver)
  2. Download — real handler (Geofabrik HTTP download)
  3. BuildGraph — real handler (requires GraphHopper JAR)
  4. ValidateGraph — real handler (checks graph files)
  5. ExtractPlacesWithPopulation — real handler (requires osmium)
  6. FilterByPopulationRange — real handler (GeoJSON filtering)
  7. PopulationStatistics — real handler (GeoJSON stats)
  8. ComputePairwiseRoutes — real handler (GraphHopper or great-circle fallback)
  9. RenderLayers — real handler (requires folium)

External dependencies:
  - requests (for Geofabrik download)
  - osmium (for PBF parsing)
  - folium (for map rendering) — optional, test adapts if missing
  - GraphHopper JAR (for routing) — optional, uses great-circle fallback
  - Network access

Run:
    pytest tests/real/py/test_city_routing.py -v --mongodb
"""

import sys
from pathlib import Path

import pytest
from facetwork.runtime import ExecutionStatus
from helpers import (
    EXAMPLE_FW_FILES,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

# The osm.CityRouting.CityRouteMap pipeline this exercises depended on the
# osm.ops.DownloadPBF / osm.ops.PgRouting.* / BuildRoutingTopology /
# ValidateTopology handlers, which were removed in an earlier refactor; the
# (now-dangling) FFL workflow was removed with them. The live city-routing
# workflows live in handlers/routing/ffl/routing_workflows.ffl.
pytestmark = pytest.mark.skip(
    reason="osm.CityRouting.CityRouteMap removed (its handlers no longer exist); "
    "see handlers/routing/ for the current routing workflows"
)

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


# FFL files needed by osmcityrouting.afl
_LIBRARY_FILES = [
    "osmtypes.ffl",
    "osmregion.ffl",
    "osmoperations.ffl",
    "osmgraphhopper.ffl",
    "osmfilters_population.ffl",
    "osmvisualization.ffl",
]


def _compile_city_routing():
    """Compile the CityRouteMap workflow with all dependencies."""
    libs = [EXAMPLE_FW_FILES[f] for f in _LIBRARY_FILES]
    return compile_afl_files(
        EXAMPLE_FW_FILES["osmcityrouting.ffl"],
        *libs,
    )


def _register_all_handlers(poller):
    """Register all handlers needed for the 9-step pipeline."""
    from osm_geocoder.handlers.downloads.operations_handlers import register_operations_handlers

    from osm_geocoder.handlers.cache.region_handlers import register_region_handlers
    from osm_geocoder.handlers.graphhopper.graphhopper_handlers import register_graphhopper_handlers
    from osm_geocoder.handlers.population.population_handlers import register_population_handlers
    from osm_geocoder.handlers.routes.routing_handlers import register_routing_handlers
    from osm_geocoder.handlers.visualization.visualization_handlers import (
        register_visualization_handlers,
    )

    register_region_handlers(poller)
    register_operations_handlers(poller)
    register_graphhopper_handlers(poller)
    register_population_handlers(poller)
    register_routing_handlers(poller)
    register_visualization_handlers(poller)


class TestCityRoutingCompilation:
    """Compilation tests that don't need external deps."""

    def test_compile_city_routing(self):
        """The osmcityrouting.afl compiles with all dependencies."""
        program = _compile_city_routing()
        workflow = extract_workflow(program, "CityRouteMap")
        assert workflow["name"] == "CityRouteMap"

        # Verify all 9 steps are in the workflow
        steps = workflow["body"]["steps"]
        assert len(steps) == 9
        step_names = [s["name"] for s in steps]
        assert "resolved" in step_names
        assert "downloaded" in step_names
        assert "graph" in step_names
        assert "validation" in step_names
        assert "cities" in step_names
        assert "filtered" in step_names
        assert "stats" in step_names
        assert "routes" in step_names
        assert "map" in step_names


@pytest.mark.skipif(not HAS_OSMIUM, reason="osmium not installed")
@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestCityRoutingIntegration:
    """Full 9-step CityRouteMap pipeline through MongoDB + AgentPoller."""

    def test_liechtenstein_city_routes(self, mongo_store, evaluator, poller):
        """Run the full 9-step pipeline for Liechtenstein.

        Liechtenstein is tiny (very small download, fast processing).
        Uses low population thresholds since Liechtenstein has few cities.
        """
        program = _compile_city_routing()
        workflow = extract_workflow(program, "CityRouteMap")

        _register_all_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={
                "region": "Liechtenstein",
                "min_population": 0,
                "max_population": 100000,
                "profile": "car",
                "title": "Liechtenstein City Routes",
            },
            max_rounds=100,  # 9 steps need many rounds
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["region_name"] == "Liechtenstein"
        assert isinstance(result.outputs["city_count"], int)
        assert isinstance(result.outputs["route_count"], int)
        assert isinstance(result.outputs["total_distance_km"], int)
