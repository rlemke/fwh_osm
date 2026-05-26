"""Phase 3: the CityRoutesByPopulation workflow composes cleanly across namespaces.

Guards against schema-name collisions (e.g. the osm.Network vs osm.Routing.Types
MatrixResult clash) and upstream signature drift by compiling the workflow
against the whole osm FFL library.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

pytest.importorskip("facetwork")

PKG = Path(__file__).resolve().parents[3]            # .../src/osm_geocoder
WF = PKG / "handlers" / "network" / "ffl" / "osmnetwork_workflows.ffl"


def _all_ffl() -> list[str]:
    return sorted({*glob.glob(str(PKG / "**" / "ffl" / "*.ffl"), recursive=True),
                   *glob.glob(str(PKG / "ffl" / "*.ffl"))})


def _compile(primary: str, files: list[str]):
    from facetwork.emitter import emit_dict
    from facetwork.parser import FFLParser
    from facetwork.source import CompilerInput, FileOrigin, SourceEntry
    from facetwork.validator import validate

    def entry(path, lib):
        return SourceEntry(text=Path(path).read_text(), origin=FileOrigin(path=str(path)), is_library=lib)

    libs = [f for f in files if str(f) != str(primary)]
    ci = CompilerInput(primary_sources=[entry(primary, False)],
                       library_sources=[entry(f, True) for f in libs])
    ast, _ = FFLParser().parse_sources(ci)
    return [str(e) for e in validate(ast).errors], emit_dict(ast, include_locations=False)


def test_workflow_introduces_no_new_validation_errors():
    files = _all_ffl()
    others = [f for f in files if Path(f) != WF]
    base, _ = _compile(others[0], others)
    full, _ = _compile(str(WF), files)
    new = set(full) - set(base)
    assert not new, "CityRoutesByPopulation introduced errors: " + "; ".join(sorted(new))


def test_workflow_is_declared():
    _errs, program = _compile(str(WF), _all_ffl())
    names: list[str] = []

    def walk(node):
        names.extend(w.get("name") for w in node.get("workflows", []))
        for decl in node.get("declarations", []):
            if decl.get("type") == "WorkflowDecl":
                names.append(decl.get("name"))
            if decl.get("type") == "Namespace":
                walk(decl)
        for ns in node.get("namespaces", []):
            walk(ns)

    walk(program)
    assert {"CityRoutesByPopulation", "RouteFanout"} <= set(names)
