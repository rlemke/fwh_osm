"""Integration test: end-to-end population pipeline with real Geofabrik download + osmium.

Downloads Monaco (smallest Geofabrik region, ~2MB), extracts cities with population
using osmium, filters by population range, and computes statistics.

External dependencies:
  - requests (for Geofabrik download)
  - osmium (for PBF parsing)
  - Network access

Run:
    pytest tests/real/py/test_population_pipeline.py -v --mongodb
"""

import pytest
from helpers import (
    EXAMPLE_AFL_FILES,
    INTEGRATION_AFL_DIR,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

from facetwork.runtime import ExecutionStatus

# The package is installed via pyproject.toml, so handlers import
# package-qualified (osm_geocoder.handlers.*) — no sys.path manipulation needed.


# Skip if osmium not available
try:
    import osmium  # noqa: F401

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False

# Skip if requests not available
try:
    import requests  # noqa: F401

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def _compile_population_pipeline():
    """Compile the population pipeline workflow with dependencies."""
    return compile_afl_files(
        INTEGRATION_AFL_DIR / "population_pipeline.ffl",
        EXAMPLE_AFL_FILES["osmtypes.ffl"],
        EXAMPLE_AFL_FILES["osmregion.ffl"],
        EXAMPLE_AFL_FILES["osmcache_download.ffl"],
        EXAMPLE_AFL_FILES["osmfilters_population.ffl"],
    )


def _register_handlers(poller):
    """Register real handlers for the population pipeline."""
    from osm_geocoder.handlers.cache.region_handlers import register_region_handlers
    from osm_geocoder.handlers.population.population_handlers import register_population_handlers

    register_region_handlers(poller)
    register_population_handlers(poller)


class TestPopulationPipelineCompilation:
    """Compilation tests that don't need external deps."""

    def test_compile_population_pipeline(self):
        """The population pipeline FFL compiles without errors."""
        program = _compile_population_pipeline()
        workflow = extract_workflow(program, "PopulationPipeline")
        assert workflow["name"] == "PopulationPipeline"


@pytest.mark.skipif(not HAS_OSMIUM, reason="osmium not installed")
@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestPopulationPipelineIntegration:
    """Full data pipeline: Geofabrik download → osmium extraction → filtering → stats."""

    def test_monaco_population(self, mongo_store, evaluator, poller):
        """Download Monaco PBF, extract cities, filter, compute stats.

        Monaco is tiny (~2MB download) so this test is fast even with real I/O.
        Note: Monaco may have very few or no cities with large populations,
        so we test with min_population=0 to ensure we get results.
        """
        program = _compile_population_pipeline()
        workflow = extract_workflow(program, "PopulationPipeline")

        _register_handlers(poller)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={
                "region": "Monaco",
                "min_population": 0,
                "max_population": 10000000,
            },
            max_rounds=50,
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["region_name"] == "Monaco"
        # Monaco is small — feature_count may be 0 or small
        assert isinstance(result.outputs["feature_count"], int)
        assert isinstance(result.outputs["total_population"], int)
