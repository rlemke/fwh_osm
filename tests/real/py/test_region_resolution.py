"""Integration test: multi-file FFL compilation with real region resolver.

Compiles from real FFL source files (osmregion.afl + osmtypes.afl) and
uses the real region_resolver.py (pure Python, no network). The download
step is replaced with a mock cache to avoid HTTP calls.

No external dependencies beyond MongoDB and the region_resolver module.

Run:
    pytest tests/real/py/test_region_resolution.py -v --mongodb
"""

import sys
from pathlib import Path

from helpers import (
    EXAMPLE_AFL_FILES,
    INTEGRATION_AFL_DIR,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

from facetwork.runtime import ExecutionStatus

# Legacy sys.path manipulation: the package is now installed via pyproject.toml,
_EXAMPLE_ROOT = Path(__file__).parent.parent.parent.parent
if str(_EXAMPLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_ROOT))

from osm_geocoder.handlers.shared.region_resolver import resolve  # noqa: E402


def _resolve_region_handler(params: dict) -> dict:
    """Handle ResolveRegion using the real resolver but mock cache data.

    Calls the real region_resolver.resolve() for accurate name matching,
    but returns synthetic cache data instead of downloading from Geofabrik.
    """
    name = params["name"]
    prefer_continent = params.get("prefer_continent", "") or None

    result = resolve(name, prefer_continent=prefer_continent)

    if not result.matches:
        return {
            "cache": {
                "url": "",
                "path": "",
                "date": "",
                "size": 0,
                "wasInCache": False,
            },
            "resolution": {
                "query": name,
                "matched_name": "",
                "region_namespace": "",
                "continent": "",
                "geofabrik_path": "",
                "is_ambiguous": False,
                "disambiguation": f"No region found for '{name}'",
            },
        }

    best = result.matches[0]
    return {
        "cache": {
            "url": f"https://download.geofabrik.de/{best.geofabrik_path}-latest.osm.pbf",
            "path": f"/tmp/osm-cache/{best.geofabrik_path}-latest.osm.pbf",
            "date": "2026-01-01T00:00:00",
            "size": 1024000,
            "wasInCache": True,
        },
        "resolution": {
            "query": name,
            "matched_name": best.facet_name,
            "region_namespace": best.namespace,
            "continent": best.continent,
            "geofabrik_path": best.geofabrik_path,
            "is_ambiguous": result.is_ambiguous,
            "disambiguation": result.disambiguation,
        },
    }


def _compile_region_test():
    """Compile the region test workflow with its dependencies."""
    return compile_afl_files(
        INTEGRATION_AFL_DIR / "resolve_region_test.ffl",
        EXAMPLE_AFL_FILES["osmtypes.ffl"],
        EXAMPLE_AFL_FILES["osmregion.ffl"],
    )


class TestRegionResolutionIntegration:
    """Multi-file compilation + real region resolver through MongoDB pipeline."""

    def test_compile_multi_file(self):
        """Multi-file compilation succeeds with osmtypes + osmregion + test workflow."""
        program = _compile_region_test()
        assert program["type"] == "Program"

        workflow = extract_workflow(program, "ResolveRegionTest")
        assert workflow["name"] == "ResolveRegionTest"

    def test_resolve_germany(self, mongo_store, evaluator, poller):
        """Resolve 'Germany' — a direct country match."""
        program = _compile_region_test()
        workflow = extract_workflow(program, "ResolveRegionTest")

        poller.register("osm.Region.ResolveRegion", _resolve_region_handler)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"name": "Germany"},
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["matched_name"] == "Germany"
        assert result.outputs["continent"] == "Europe"
        assert "europe/germany" in result.outputs["geofabrik_path"]

    def test_resolve_alps(self, mongo_store, evaluator, poller):
        """Resolve 'Alps' — a geographic feature that returns the best match."""
        program = _compile_region_test()
        workflow = extract_workflow(program, "ResolveRegionTest")

        poller.register("osm.Region.ResolveRegion", _resolve_region_handler)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"name": "Alps"},
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        # Alps resolves to a geographic feature — should get a match
        assert result.outputs["matched_name"] != ""

    def test_resolve_ambiguous_georgia(self, mongo_store, evaluator, poller):
        """Resolve 'Georgia' with prefer_continent to disambiguate."""
        program = _compile_region_test()
        workflow = extract_workflow(program, "ResolveRegionTest")

        poller.register("osm.Region.ResolveRegion", _resolve_region_handler)

        # Georgia (US state) with UnitedStates preference
        # (the resolver uses "UnitedStates" as the continent for US states)
        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"name": "Georgia", "prefer_continent": "UnitedStates"},
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["matched_name"] == "Georgia"
        # With UnitedStates preference, should resolve to the US state
        assert "north-america" in result.outputs["geofabrik_path"]

    def test_resolve_unknown_region(self, mongo_store, evaluator, poller):
        """Resolve a non-existent region — should complete with empty match."""
        program = _compile_region_test()
        workflow = extract_workflow(program, "ResolveRegionTest")

        poller.register("osm.Region.ResolveRegion", _resolve_region_handler)

        result = run_to_completion(
            evaluator,
            poller,
            workflow,
            program,
            inputs={"name": "Atlantis"},
        )

        assert result.success
        assert result.status == ExecutionStatus.COMPLETED
        assert result.outputs["matched_name"] == ""
