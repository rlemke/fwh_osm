"""Integration test: multi-file FFL compilation with real region resolver.

Compiles from real FFL source files (osmregion.afl + osmtypes.afl) and
uses the real region_resolver.py (pure Python, no network). The download
step is replaced with a mock cache to avoid HTTP calls.

No external dependencies beyond MongoDB and the region_resolver module.

Run:
    pytest tests/real/py/test_region_resolution.py -v --mongodb
"""

from helpers import (
    EXAMPLE_FW_FILES,
    INTEGRATION_FW_DIR,
    compile_afl_files,
    extract_workflow,
    run_to_completion,
)

from facetwork.runtime import ExecutionStatus

# The package is installed via pyproject.toml, so handlers import
# package-qualified — no sys.path manipulation needed.
from osm_geocoder.handlers.shared.region_resolver import (  # noqa: E402
    region_from_path,
    resolve_batch,
)


def _resolve_region_handler(params: dict) -> dict:
    """Handle ResolveRegion using the real resolver, returning a typed Region.

    Mirrors the real ``osm.Region.ResolveRegion`` handler
    (``cache/region_handlers.py::handle_resolve_region``): it runs the batch
    resolver for accurate qualifier-suffix / continent matching and returns
    ``{"region": <Region dict>}`` — the modern single-output contract. The
    legacy ``(cache, resolution)`` shape has been retired. No network I/O.
    """
    name = params["name"]
    prefer_continent = params.get("prefer_continent", "") or None

    result = resolve_batch(names=[name], prefer_continent=prefer_continent, strict=False)

    if not result.regions:
        # No match — return an all-empty Region (matches the real handler).
        return {"region": region_from_path("", query=name).to_dict()}

    return {"region": result.regions[0].to_dict()}


def _compile_region_test():
    """Compile the region test workflow with its dependencies."""
    return compile_afl_files(
        INTEGRATION_FW_DIR / "resolve_region_test.ffl",
        EXAMPLE_FW_FILES["osmtypes.ffl"],
        EXAMPLE_FW_FILES["osmregion.ffl"],
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
