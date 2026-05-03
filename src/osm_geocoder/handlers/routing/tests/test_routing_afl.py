"""Tests that routing adapter FFL files compile without errors."""

from pathlib import Path

import pytest

from facetwork.parser import parse

AFL_DIR = Path(__file__).resolve().parent.parent / "afl"

# All routing FFL files that should compile
AFL_FILES = sorted(AFL_DIR.glob("*.ffl"))

# Dependency files needed for use statements
DEPS_DIR = Path(__file__).resolve().parent.parent.parent
TYPES_AFL = DEPS_DIR / "cache" / "afl" / "osmtypes.ffl"
VIZ_AFL = list((DEPS_DIR / "visualization").rglob("*.ffl"))


def _read(path: Path) -> str:
    return path.read_text()


def _compile_with_deps(*afl_paths: Path):
    """Compile FFL files with their dependencies. Returns a Program AST."""
    sources = []
    # Always include types
    if TYPES_AFL.exists():
        sources.append(_read(TYPES_AFL))
    # Include viz for workflow files that use osm.viz
    for v in VIZ_AFL:
        sources.append(_read(v))
    # Include routing types before other routing files
    types_file = AFL_DIR / "routing_types.ffl"
    if types_file.exists():
        sources.append(_read(types_file))
    for p in afl_paths:
        if p.name != "routing_types.ffl":
            sources.append(_read(p))
    combined = "\n\n".join(sources)
    return parse(combined)


def _all_schema_names(program) -> list[str]:
    """Extract all schema names from all namespaces in a Program."""
    names = [s.name for s in program.schemas]
    for ns in program.namespaces:
        names.extend(s.name for s in ns.schemas)
    return names


def _all_event_facet_names(program) -> list[str]:
    """Extract all event facet names from all namespaces in a Program."""
    names = [ef.sig.name for ef in program.event_facets]
    for ns in program.namespaces:
        names.extend(ef.sig.name for ef in ns.event_facets)
    return names


@pytest.mark.parametrize("afl_file", AFL_FILES, ids=lambda p: p.name)
def test_afl_compiles(afl_file: Path):
    """Each routing FFL file should compile without errors."""
    _compile_with_deps(afl_file)


def test_all_routing_afl_compile_together():
    """All routing FFL files should compile together without conflicts."""
    _compile_with_deps(*AFL_FILES)


def test_routing_types_defines_schemas():
    """routing_types.afl should define the expected schemas."""
    program = _compile_with_deps(AFL_DIR / "routing_types.ffl")
    schema_names = _all_schema_names(program)
    assert "Waypoint" in schema_names
    assert "RouteResult" in schema_names
    assert "PointToPointResult" in schema_names
    assert "MultiStopResult" in schema_names
    assert "IsochroneResult" in schema_names


def test_api_adapter_defines_facets():
    """routing_api.afl should define Route, MultiStopRoute, Isochrone facets."""
    program = _compile_with_deps(AFL_DIR / "routing_api.ffl")
    facet_names = _all_event_facet_names(program)
    assert "Route" in facet_names
    assert "MultiStopRoute" in facet_names
    assert "Isochrone" in facet_names


def test_osrm_adapter_defines_facets():
    """routing_osrm.afl should define Route, MultiStopRoute, Isochrone facets."""
    program = _compile_with_deps(AFL_DIR / "routing_osrm.ffl")
    facet_names = _all_event_facet_names(program)
    assert "Route" in facet_names
    assert "MultiStopRoute" in facet_names
    assert "Isochrone" in facet_names
