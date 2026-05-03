"""Tests for the OSM cache validation FFL file.

Verifies that osmvalidation.afl parses, validates, and compiles correctly,
and that the composed workflow in osmworkflows_composed.afl using the
validation facets also compiles.
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


class TestOsmValidationCompilation:
    """Compilation tests for osmvalidation.afl."""

    def test_parse_osmvalidation(self):
        """osmvalidation.afl parses and validates with its dependency."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")
        assert program["type"] == "Program"

    def test_validation_namespace_exists(self):
        """The Validation namespace is emitted."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")

        ns_names = []

        def _collect_ns(node, prefix=""):
            for decl in node.get("declarations", []):
                if decl.get("type") == "Namespace":
                    name = decl.get("name", "")
                    full = f"{prefix}.{name}" if prefix else name
                    ns_names.append(full)
                    _collect_ns(decl, full)

        _collect_ns(program)

        assert any("Validation" in n for n in ns_names)

    def test_validation_schemas(self):
        """Both schemas (ValidationStats, ValidationResult) are emitted."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")

        schema_names = []

        def _collect_schemas(node):
            for decl in node.get("declarations", []):
                if decl.get("type") == "SchemaDecl":
                    schema_names.append(decl["name"])
                elif decl.get("type") == "Namespace":
                    _collect_schemas(decl)

        _collect_schemas(program)

        assert "ValidationStats" in schema_names
        assert "ValidationResult" in schema_names

    def test_validation_event_facets(self):
        """All five event facets are emitted."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")

        facet_names = []

        def _collect_facets(node):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl":
                    facet_names.append(decl["name"])
                elif decl.get("type") == "Namespace":
                    _collect_facets(decl)

        _collect_facets(program)

        assert "ValidateCache" in facet_names
        assert "ValidateGeometry" in facet_names
        assert "ValidateTags" in facet_names
        assert "ValidateBounds" in facet_names
        assert "ValidationSummary" in facet_names

    def test_validate_cache_params(self):
        """ValidateCache has the expected parameters with defaults."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")

        def _find_facet(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_facet(decl, name)
                    if found:
                        return found
            return None

        facet = _find_facet(program, "ValidateCache")

        assert facet is not None
        param_names = [p["name"] for p in facet["params"]]
        assert "cache" in param_names
        assert "output_dir" in param_names
        assert "use_hdfs" in param_names

    def test_validate_bounds_defaults(self):
        """ValidateBounds has lat/lon defaults."""
        program = _compile("osmvalidation.ffl", "osmtypes.ffl")

        def _find_facet(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "EventFacetDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_facet(decl, name)
                    if found:
                        return found
            return None

        facet = _find_facet(program, "ValidateBounds")

        assert facet is not None
        param_names = [p["name"] for p in facet["params"]]
        assert "min_lat" in param_names
        assert "max_lat" in param_names
        assert "min_lon" in param_names
        assert "max_lon" in param_names

    def test_cli_check_osmvalidation(self, tmp_path):
        """The CLI --check flag succeeds for osmvalidation.afl."""
        result = main(
            [
                "--primary",
                str(_AFL_BY_NAME["osmvalidation.ffl"]),
                "--library",
                str(_AFL_BY_NAME["osmtypes.ffl"]),
                "--check",
            ]
        )
        assert result == 0


class TestComposedValidationWorkflow:
    """Tests for the Pattern 11 composed workflow."""

    def test_compile_validate_and_summarize(self):
        """The ValidateAndSummarize workflow compiles with all dependencies."""
        # Collect all FFL files needed (the composed workflows reference many namespaces)
        filenames = sorted(_AFL_BY_NAME.keys())

        # Put osmworkflows_composed.afl first as primary
        filenames.remove("osmworkflows_composed.ffl")
        filenames.insert(0, "osmworkflows_composed.ffl")

        program = _compile(*filenames)

        # Find the ValidateAndSummarize workflow
        def _find_wf(node, name):
            for decl in node.get("declarations", []):
                if decl.get("type") == "WorkflowDecl" and decl["name"] == name:
                    return decl
                if decl.get("type") == "Namespace":
                    found = _find_wf(decl, name)
                    if found:
                        return found
            return None

        workflow = _find_wf(program, "ValidateAndSummarize")

        assert workflow is not None
        assert workflow["name"] == "ValidateAndSummarize"

        # Should have 2 steps: cache, f (delegates to ValidateAndSummarizeFromCache)
        steps = workflow["body"]["steps"]
        assert len(steps) == 2
        step_names = [s["name"] for s in steps]
        assert step_names == ["cache", "f"]
        assert steps[1]["call"]["target"] == "ValidateAndSummarizeFromCache"
