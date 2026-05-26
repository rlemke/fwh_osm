"""Phase 0 scaffold tests for osm.Network (BuildNetwork / ApproxRoute / RouteMatrix).

Asserts the *contract* is real and review-ready:

* the osmnetwork.ffl is validator-clean and declares the three facets,
* the compute-core dataclasses round-trip via ``to_dict``,
* the handler dispatch table exposes all three facets and short-circuits on
  empty input,
* the not-yet-implemented Phase-1 bodies fail loudly (so the namespace is
  importable/loadable without silently advertising missing behaviour).

The behavioural noding/Dijkstra tests are added in Phase 1 alongside the impl.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FFL = Path(__file__).resolve().parents[1] / "ffl" / "osmnetwork.ffl"
EXPECTED_FACETS = {"BuildNetwork", "ApproxRoute", "RouteMatrix"}


# --- the FFL contract ----------------------------------------------------------


def _compile():
    from facetwork.emitter import emit_dict
    from facetwork.parser import FFLParser
    from facetwork.source import CompilerInput, FileOrigin, SourceEntry
    from facetwork.validator import validate

    text = FFL.read_text()
    ci = CompilerInput(
        primary_sources=[SourceEntry(text=text, origin=FileOrigin(path=str(FFL)))],
        library_sources=[],
    )
    ast, _ = FFLParser().parse_sources(ci)
    result = validate(ast)
    assert not result.errors, "; ".join(str(e) for e in result.errors)
    return emit_dict(ast, include_locations=False)


def _namespace(program: dict, name: str) -> dict:
    for decl in program.get("declarations", []):
        if decl.get("type") == "Namespace" and decl.get("name") == name:
            return decl
    raise AssertionError(f"namespace {name} not found")


def test_ffl_is_validator_clean_and_declares_facets():
    pytest.importorskip("facetwork")
    program = _compile()
    ns = _namespace(program, "osm.Network")
    facets = {
        d["name"] for d in ns.get("declarations", [])
        if d.get("type") == "EventFacetDecl"
    }
    assert EXPECTED_FACETS <= facets, f"missing: {EXPECTED_FACETS - facets}"


def test_ffl_facets_are_pure():
    """The whole point of the Network layer: pure facets that fan out freely.

    Effect/Cost ride on each facet as annotation mixins; assert the source keeps
    them so the capability index classifies these as pure (not external).
    """
    text = FFL.read_text()
    for facet in EXPECTED_FACETS:
        assert f"facet {facet}" in text
    assert text.count('Effect(kind = "pure")') == len(EXPECTED_FACETS)


# --- the compute-core dataclasses ----------------------------------------------


def test_result_dataclasses_round_trip():
    from osm_geocoder.handlers.network import network_ops as ops

    net = ops.NetworkResult(network_path="/c/osm/network/x", node_count=10, edge_count=12,
                            connected_components=1, largest_component_frac=1.0)
    assert net.to_dict()["network_path"] == "/c/osm/network/x"
    assert net.to_dict()["edge_count"] == 12

    route = ops.RouteResult(route_path="/c/r.geojson", distance_km=615.0, reached_b=True)
    assert route.to_dict()["reached_b"] is True
    assert route.to_dict()["distance_km"] == 615.0

    mtx = ops.MatrixResult(result_path="/c/m.json", pair_count=9, reachable_count=8)
    assert mtx.to_dict()["reachable_count"] == 8


# --- the handler dispatch contract ---------------------------------------------


def test_dispatch_exposes_all_facets():
    from osm_geocoder.handlers.network import network_handlers as h

    assert set(h._DISPATCH) == {f"osm.Network.{f}" for f in EXPECTED_FACETS}


def test_handlers_short_circuit_on_empty_input():
    from osm_geocoder.handlers.network import network_handlers as h

    # No edges/network path → empty result, never touches the (unimplemented) core.
    assert h.handle({"_facet_name": "osm.Network.BuildNetwork", "edges_path": ""})["result"]["node_count"] == 0
    assert h.handle({"_facet_name": "osm.Network.ApproxRoute", "network_path": ""})["result"]["reached_b"] is False
    assert h.handle({"_facet_name": "osm.Network.RouteMatrix", "network_path": "", "points": ""})["result"]["pair_count"] == 0


# --- Phase-1 bodies fail loudly (no silent empty defaults) ----------------------


@pytest.mark.parametrize("op,kwargs", [
    ("build_network", {"edges_path": "/tmp/edges.geojson"}),
    ("approx_route", {"network_path": "/tmp/net", "from_lat": 37.8, "from_lon": -122.4,
                      "to_lat": 34.0, "to_lon": -118.2}),
    ("route_matrix", {"network_path": "/tmp/net", "points": "[]"}),
])
def test_phase1_core_not_yet_implemented(op, kwargs):
    pytest.importorskip("shapely")
    pytest.importorskip("networkx")
    from osm_geocoder.handlers.network import network_ops as ops

    with pytest.raises(NotImplementedError, match="Phase 1"):
        getattr(ops, op)(**kwargs)
