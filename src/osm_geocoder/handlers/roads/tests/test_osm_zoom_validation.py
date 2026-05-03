"""Tests for the OSM zoom builder FFL file.

Verifies that osmzoombuilder.afl parses, validates, and compiles correctly,
and that the composed RoadZoomBuilder workflow in osmworkflows_composed.afl
also compiles with all dependencies.
"""

from pathlib import Path

from facetwork.cli import main
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


def _compile(*filenames: str) -> dict:
    """Compile one or more FFL files from the OSM example directory."""
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


class TestZoomBuilderCompilation:
    """Compilation tests for osmzoombuilder.afl."""

    def test_parse_osmzoombuilder(self):
        """osmzoombuilder.afl parses and validates with its dependency."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")
        assert program["type"] == "Program"

    def test_zoombuilder_namespace_exists(self):
        """The ZoomBuilder namespace is emitted."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")

        ns_names = []

        def _collect_ns(node, prefix=""):
            for decl in node.get("declarations", []):
                if decl.get("type") == "Namespace":
                    name = decl.get("name", "")
                    full = f"{prefix}.{name}" if prefix else name
                    ns_names.append(full)
                    _collect_ns(decl, full)

        _collect_ns(program)

        assert any("ZoomBuilder" in n for n in ns_names)

    def test_zoombuilder_schemas(self):
        """All six schemas are emitted."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")

        schema_names = []

        def _collect_schemas(node):
            for decl in node.get("declarations", []):
                if decl.get("type") == "SchemaDecl":
                    schema_names.append(decl["name"])
                elif decl.get("type") == "Namespace":
                    _collect_schemas(decl)

        _collect_schemas(program)

        assert "LogicalEdge" in schema_names
        assert "ZoomEdgeResult" in schema_names
        assert "ZoomBuilderResult" in schema_names
        assert "ZoomBuilderMetrics" in schema_names
        assert "ZoomBuilderConfig" in schema_names
        assert "CellBudget" in schema_names

    def test_zoombuilder_event_facets(self):
        """All nine event facets are emitted."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")

        facet_names = []

        def _collect_facets(node):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl":
                    facet_names.append(decl["name"])
                elif decl.get("type") == "Namespace":
                    _collect_facets(decl)

        _collect_facets(program)

        assert "BuildLogicalGraph" in facet_names
        assert "BuildAnchors" in facet_names
        assert "ComputeSBS" in facet_names
        assert "ComputeScores" in facet_names
        assert "DetectBypasses" in facet_names
        assert "DetectRings" in facet_names
        assert "SelectEdges" in facet_names
        assert "ExportZoomLayers" in facet_names
        assert "BuildZoomLayers" in facet_names

    def test_build_zoom_layers_params(self):
        """BuildZoomLayers has the expected parameters."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")

        def _find_facet(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_facet(decl, name)
                    if found:
                        return found
            return None

        facet = _find_facet(program, "BuildZoomLayers")

        assert facet is not None
        param_names = [p["name"] for p in facet["params"]]
        assert "cache" in param_names
        assert "graph" in param_names
        assert "min_population" in param_names
        assert "output_dir" in param_names
        assert "max_concurrent" in param_names

    def test_build_zoom_layers_defaults(self):
        """BuildZoomLayers has correct default values."""
        program = _compile("osmzoombuilder.ffl", "osmtypes.ffl")

        def _find_facet(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_facet(decl, name)
                    if found:
                        return found
            return None

        facet = _find_facet(program, "BuildZoomLayers")

        assert facet is not None
        defaults = {p["name"]: p.get("default") for p in facet["params"] if "default" in p}
        assert "min_population" in defaults
        assert "output_dir" in defaults
        assert "max_concurrent" in defaults

    def test_cli_check_osmzoombuilder(self, tmp_path):
        """The CLI --check flag succeeds for osmzoombuilder.afl."""
        result = main(
            [
                "--primary",
                str(_AFL_BY_NAME["osmzoombuilder.ffl"]),
                "--library",
                str(_AFL_BY_NAME["osmtypes.ffl"]),
                "--check",
            ]
        )
        assert result == 0


class TestComposedZoomWorkflow:
    """Tests for the RoadZoomBuilder composed workflow."""

    def test_compile_all_afl_files(self):
        """All FFL files compile together without errors."""
        filenames = sorted(_AFL_BY_NAME.keys())

        # Put osmworkflows_composed.afl first as primary
        filenames.remove("osmworkflows_composed.ffl")
        filenames.insert(0, "osmworkflows_composed.ffl")

        program = _compile(*filenames)
        assert program["type"] == "Program"

    def test_road_zoom_builder_workflow(self):
        """The RoadZoomBuilder workflow compiles with 3 steps."""
        filenames = sorted(_AFL_BY_NAME.keys())

        filenames.remove("osmworkflows_composed.ffl")
        filenames.insert(0, "osmworkflows_composed.ffl")

        program = _compile(*filenames)

        def _find_wf(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "WorkflowDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_wf(decl, name)
                    if found:
                        return found
            return None

        workflow = _find_wf(program, "RoadZoomBuilder")

        assert workflow is not None
        assert workflow["name"] == "RoadZoomBuilder"

        # Should have 2 steps: cache, f (delegates to RoadZoomBuilderFromCache)
        steps = workflow["body"]["steps"]
        assert len(steps) == 2
        step_names = [s["name"] for s in steps]
        assert step_names == ["cache", "f"]
        assert steps[1]["call"]["target"] == "RoadZoomBuilderFromCache"
