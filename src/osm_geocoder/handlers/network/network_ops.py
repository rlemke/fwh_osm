"""Approximate freeway routing — the osm.Network compute core.

Pure, in-process graph routing over a small noded-freeway network artifact: the
engine-free alternative to a full OSRM/Valhalla build+daemon. See
docs/architecture/approximate-freeway-routing.md (facetwork repo) for the design,
the on-disk cache layout, and the cross-server sharing contract.

Three operations:

* :func:`build_network`  — node the freeway LineStrings (shapely ``unary_union``)
  into a routable graph and persist it as a cache *directory* artifact
  (``nodes.geojson`` + ``edges.geojson`` + ``graph.json``). ``graph.json`` is the
  authoritative, language-neutral adjacency list — every runner rebuilds a
  ``networkx`` graph from it in milliseconds.
* :func:`approx_route`   — snap A and B to the nearest network nodes, Dijkstra by
  segment length, return the route plus the closest reachable point to B (and the
  straight-line residual ``gap_to_b_km`` when B is off-network).
* :func:`route_matrix`   — all-pairs over the small graph, one single-source
  Dijkstra per origin.

Phase 0 ships the contract (FFL + these dataclasses/signatures + handler wiring +
tests). The noding/Dijkstra bodies land in Phase 1 — they currently raise
``NotImplementedError`` so the namespace is importable and loadable by a runner
without advertising behaviour that does not exist yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

try:
    import networkx as _nx  # noqa: F401

    HAS_NETWORKX = True
except ImportError:  # pragma: no cover - optional until Phase 1
    HAS_NETWORKX = False

try:
    from shapely.geometry import shape as _shape  # noqa: F401

    HAS_SHAPELY = True
except ImportError:  # pragma: no cover
    HAS_SHAPELY = False

# Cache layout (see cache-layout.agent-spec.yaml + the design doc).
NAMESPACE_CACHE = "osm"
CACHE_TYPE = "network"

_PHASE1 = (
    "osm.Network.{op} is scaffolded (Phase 0) but not yet implemented — the "
    "noding/Dijkstra core lands in Phase 1. See "
    "docs/architecture/approximate-freeway-routing.md."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_deps() -> None:
    """The whole layer is pure shapely + networkx; fail clearly if either is missing."""
    missing = [name for name, ok in (("shapely>=2.0", HAS_SHAPELY),
                                     ("networkx>=3.0", HAS_NETWORKX)) if not ok]
    if missing:
        raise RuntimeError(
            "osm.Network requires " + " and ".join(missing)
            + " (install the osm-geocoder package with its routing extras)"
        )


@dataclass
class NetworkResult:
    """A built, routable freeway graph (mirrors the FFL NetworkResult)."""

    network_path: str
    node_count: int = 0
    edge_count: int = 0
    connected_components: int = 0
    largest_component_frac: float = 0.0
    snap_tolerance_m: float = 25.0
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_path": self.network_path,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "connected_components": self.connected_components,
            "largest_component_frac": self.largest_component_frac,
            "snap_tolerance_m": self.snap_tolerance_m,
            "extraction_date": self.extraction_date,
        }


@dataclass
class RouteResult:
    """A single approximate route (mirrors the FFL RouteResult)."""

    route_path: str = ""
    distance_km: float = 0.0
    reached_lat: float = 0.0
    reached_lon: float = 0.0
    gap_to_b_km: float = 0.0
    reached_b: bool = False
    node_hops: int = 0
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_path": self.route_path,
            "distance_km": self.distance_km,
            "reached_lat": self.reached_lat,
            "reached_lon": self.reached_lon,
            "gap_to_b_km": self.gap_to_b_km,
            "reached_b": self.reached_b,
            "node_hops": self.node_hops,
            "extraction_date": self.extraction_date,
        }


@dataclass
class MatrixResult:
    """All-pairs approximate routing result (mirrors the FFL MatrixResult)."""

    result_path: str = ""
    pair_count: int = 0
    reachable_count: int = 0
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_path": self.result_path,
            "pair_count": self.pair_count,
            "reachable_count": self.reachable_count,
            "extraction_date": self.extraction_date,
        }


def build_network(
    edges_path: str,
    snap_tolerance_m: float = 25.0,
    ref_filter: str = "",
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> NetworkResult:
    """Node freeway LineStrings into a routable graph cached as a directory artifact.

    Phase 1: localize ``edges_path``; optionally keep only ways whose ``ref``
    starts with ``ref_filter``; ``shapely.ops.unary_union`` to node at crossings;
    merge endpoints within ``snap_tolerance_m``; assign node ids; build a
    ``networkx.Graph`` weighted by segment length; write ``nodes.geojson`` /
    ``edges.geojson`` / ``graph.json`` into staging and atomically publish the
    directory + sibling sidecar (with component counts in ``extra``).
    """
    _require_deps()
    raise NotImplementedError(_PHASE1.format(op="BuildNetwork"))


def approx_route(
    network_path: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> RouteResult:
    """Approximate A→B route over a built freeway network.

    Phase 1: load (and memoize) the graph from ``network_path/graph.json``; snap
    A and B to the nearest network node (projected point on the nearest edge);
    ``networkx.dijkstra_path`` by length. If B's node is unreachable from A's
    component, return the reachable node closest to B with ``reached_b=False`` and
    ``gap_to_b_km`` set. Emit the traversed segments as a GeoJSON LineString.
    """
    _require_deps()
    raise NotImplementedError(_PHASE1.format(op="ApproxRoute"))


def route_matrix(
    network_path: str,
    points: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> MatrixResult:
    """All-pairs approximate routing over the small network (pure in-process).

    Phase 1: parse ``points`` (JSON list of {lon,lat,name} or "lon,lat;..."),
    snap each to a network node, run one single-source Dijkstra per origin, and
    write the pairwise distances as JSON.
    """
    _require_deps()
    raise NotImplementedError(_PHASE1.format(op="RouteMatrix"))
