"""Approximate freeway routing — the osm.Network compute core.

Pure, in-process graph routing over a small noded-freeway network artifact: the
engine-free alternative to a full OSRM/Valhalla build+daemon. See
docs/architecture/approximate-freeway-routing.md (facetwork repo) for the design,
the on-disk cache layout, and the cross-server sharing contract.

Three operations:

* :func:`build_network`  — node the freeway LineStrings (shapely ``unary_union``)
  into a routable graph and persist it as a cache *directory* artifact
  (``nodes.geojson`` + ``edges.geojson`` + ``graph.json``) under the ``osm/network``
  cache_type. ``graph.json`` is the authoritative, language-neutral adjacency
  list — every runner rebuilds a ``networkx`` graph from it in milliseconds.
* :func:`approx_route`   — Phase 2.
* :func:`route_matrix`   — Phase 3.

``build_network`` writes a durable, content-addressed cache entry (keyed by the
input sha256 + snap tolerance + ref filter) via the shared sidecar protocol, so
it is built once and shared across the fleet exactly like the graphhopper/osrm
artifacts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..shared.geojson_writer import iter_geojson_features

log = logging.getLogger(__name__)

# Make the tools/ dir importable so we can reach the shared sidecar cache API
# (the same side-effect shared.pbf_convert / shared.pbf_cache rely on).
# _osm_tools holds the canonical cache-layout implementation used by every
# durable OSM artifact.
_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

try:
    import networkx as nx

    HAS_NETWORKX = True
except ImportError:  # pragma: no cover - optional until Phase 1
    HAS_NETWORKX = False

try:
    import shapely
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    HAS_SHAPELY = True
except ImportError:  # pragma: no cover
    HAS_SHAPELY = False

try:
    from pyproj import Geod

    _GEOD = Geod(ellps="WGS84")
    HAS_PYPROJ = True
except ImportError:  # pragma: no cover
    HAS_PYPROJ = False

# Cache layout (see cache-layout.agent-spec.yaml + the design doc).
NAMESPACE_CACHE = "osm"
CACHE_TYPE = "network"
_EARTH_R_M = 6371008.8  # mean Earth radius (m), haversine fallback when no pyproj

_PHASE = (
    "osm.Network.{op} is scaffolded but not yet implemented — it lands in "
    "{phase}. See docs/architecture/approximate-freeway-routing.md."
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


# ---------------------------------------------------------------------------
# Result dataclasses (mirror the FFL schemas).
# ---------------------------------------------------------------------------


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


@dataclass
class NodedGraph:
    """The pure (I/O-free) result of noding a set of freeway LineStrings."""

    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lon, lat)
    edges: list[dict] = field(default_factory=list)                     # u/v/length_m/ref/name/edge_idx/coords
    adjacency: dict[int, list] = field(default_factory=dict)            # id -> [[nbr, length_m, edge_idx], ...]
    connected_components: int = 0
    largest_component_frac: float = 0.0

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


# ---------------------------------------------------------------------------
# Geometry helpers.
# ---------------------------------------------------------------------------


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def _line_length_m(coords: list) -> float:
    """Geodesic length of a coordinate sequence in meters."""
    if HAS_PYPROJ and len(coords) >= 2:
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return abs(_GEOD.line_length(lons, lats))
    total = 0.0
    for a, b in zip(coords, coords[1:], strict=False):
        total += _haversine_m(a[0], a[1], b[0], b[1])
    return total


def _prop(props: dict, key: str) -> str:
    """Read a tag from a feature's properties, falling back to a nested ``tags`` dict."""
    if key in props and props[key] is not None:
        return str(props[key])
    tags = props.get("tags")
    if isinstance(tags, dict) and tags.get(key) is not None:
        return str(tags[key])
    return ""


class _NodeIndex:
    """Assigns a stable node id to each endpoint, merging endpoints within ``tol_m``.

    A coarse lon/lat grid (cell ≈ tolerance) bounds the neighbour search to the
    9 surrounding cells, so snapping is ~O(1) per endpoint instead of O(N²).
    """

    def __init__(self, tol_m: float) -> None:
        self.tol_m = max(tol_m, 0.0)
        self.cell = max(self.tol_m, 1e-6) / 111_320.0  # meters -> degrees of latitude
        self.grid: dict[tuple[int, int], list[int]] = {}
        self.nodes: dict[int, tuple[float, float]] = {}
        self._next = 0

    def node_for(self, lon: float, lat: float) -> int:
        cx, cy = int(math.floor(lon / self.cell)), int(math.floor(lat / self.cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for nid in self.grid.get((cx + dx, cy + dy), ()):  # noqa: B007
                    nlon, nlat = self.nodes[nid]
                    if _haversine_m(lon, lat, nlon, nlat) <= self.tol_m:
                        return nid
        nid = self._next
        self._next += 1
        self.nodes[nid] = (lon, lat)
        self.grid.setdefault((cx, cy), []).append(nid)
        return nid


def _attr_for_segment(seg, tree, attrs: list) -> tuple[str, str]:
    """Recover (ref, name) for a noded segment via nearest original line at its midpoint.

    ``unary_union`` discards per-feature properties, so we re-associate each noded
    segment with its source line by querying the STRtree of originals.
    """
    try:
        mid = seg.interpolate(0.5, normalized=True)
        idx = int(tree.nearest(mid))
        return attrs[idx]
    except Exception:  # pragma: no cover - defensive
        return ("", "")


# ---------------------------------------------------------------------------
# Pure noding core (no I/O — unit-testable on synthetic geometry).
# ---------------------------------------------------------------------------


def node_linestrings(features, snap_tolerance_m: float = 25.0, ref_filter: str = "") -> NodedGraph:
    """Node freeway LineStrings into a routable graph (the pure, I/O-free core).

    Splits all lines at their mutual intersections (``shapely.ops.unary_union``),
    snaps segment endpoints within ``snap_tolerance_m`` to shared nodes, weights
    each edge by its geodesic length, collapses parallel edges to the shortest,
    and reports connectivity. ``ref_filter`` keeps only ways whose ``ref`` tag
    starts with the prefix (empty keeps all).
    """
    _require_deps()

    geoms: list = []
    attrs: list[tuple[str, str]] = []
    for feat in features:
        geom_json = feat.get("geometry")
        if not geom_json:
            continue
        try:
            geom = shape(geom_json)
        except Exception:
            continue
        if geom.is_empty:
            continue
        props = feat.get("properties") or {}
        ref = _prop(props, "ref")
        if ref_filter and not ref.startswith(ref_filter):
            continue
        name = _prop(props, "name")
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            if part.geom_type != "LineString" or part.is_empty:
                continue
            geoms.append(part)
            attrs.append((ref, name))

    if not geoms:
        return NodedGraph()

    noded = unary_union(geoms)
    segments = list(noded.geoms) if noded.geom_type == "MultiLineString" else [noded]
    tree = STRtree(geoms)
    idx = _NodeIndex(snap_tolerance_m)

    g = nx.Graph()
    for seg in segments:
        if seg.geom_type != "LineString" or seg.is_empty or len(seg.coords) < 2:
            continue
        coords = [[float(x), float(y)] for x, y, *_ in seg.coords]
        u = idx.node_for(coords[0][0], coords[0][1])
        v = idx.node_for(coords[-1][0], coords[-1][1])
        if u == v:
            continue  # zero-length / degenerate loop
        length_m = _line_length_m(coords)
        ref, name = _attr_for_segment(seg, tree, attrs)
        if g.has_edge(u, v):
            if length_m < g[u][v]["length_m"]:
                g[u][v].update(length_m=length_m, coords=coords, ref=ref, name=name)
        else:
            g.add_edge(u, v, length_m=length_m, coords=coords, ref=ref, name=name)

    for nid in idx.nodes:
        g.add_node(nid)

    edges: list[dict] = []
    for edge_idx, (u, v, data) in enumerate(g.edges(data=True)):
        data["edge_idx"] = edge_idx
        edges.append({
            "u": u, "v": v, "length_m": data["length_m"],
            "ref": data["ref"], "name": data["name"],
            "edge_idx": edge_idx, "coords": data["coords"],
        })

    adjacency: dict[int, list] = {}
    for u, v, data in g.edges(data=True):
        adjacency.setdefault(u, []).append([v, data["length_m"], data["edge_idx"]])
        adjacency.setdefault(v, []).append([u, data["length_m"], data["edge_idx"]])

    comps = list(nx.connected_components(g))
    largest = max((len(c) for c in comps), default=0)
    frac = largest / len(idx.nodes) if idx.nodes else 0.0

    return NodedGraph(
        nodes=dict(idx.nodes),
        edges=edges,
        adjacency=adjacency,
        connected_components=len(comps),
        largest_component_frac=frac,
    )


# ---------------------------------------------------------------------------
# Cache I/O helpers.
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", value).lower()


def _relative_path(edges_path: str, snap_tolerance_m: float, ref_filter: str) -> str:
    """Cache relative_path: source stem + the build params that change the graph."""
    stem = Path(str(edges_path)).name
    for suffix in (".geojson.gz", ".geojson", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    rel = f"{stem}@tol{int(round(snap_tolerance_m))}"
    if ref_filter:
        rel += f"-{_slug(ref_filter)}"
    return rel


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _write_nodes_geojson(path: Path, nodes: dict[int, tuple[float, float]]) -> None:
    feats = [
        {"type": "Feature", "properties": {"node_id": nid},
         "geometry": {"type": "Point", "coordinates": [lon, lat]}}
        for nid, (lon, lat) in sorted(nodes.items())
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def _write_edges_geojson(path: Path, edges: list[dict]) -> None:
    feats = [
        {"type": "Feature",
         "properties": {"u": e["u"], "v": e["v"], "length_m": round(e["length_m"], 3),
                        "ref": e["ref"], "name": e["name"], "edge_idx": e["edge_idx"]},
         "geometry": {"type": "LineString", "coordinates": e["coords"]}}
        for e in edges
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def _write_graph_json(path: Path, adjacency: dict[int, list]) -> None:
    # JSON object keys must be strings; values stay [[neighbor, length_m, edge_idx], ...].
    obj = {str(nid): adj for nid, adj in sorted(adjacency.items())}
    path.write_text(json.dumps(obj))


def _staging_dir() -> str:
    """A staging directory, preferably on the same filesystem as the cache root."""
    try:
        from _osm_tools import storage as _storage

        root = _storage.tmp_root()
        os.makedirs(root, exist_ok=True)
        return tempfile.mkdtemp(prefix="osm-network-", dir=root)
    except Exception:
        return tempfile.mkdtemp(prefix="osm-network-")


# ---------------------------------------------------------------------------
# Public operations.
# ---------------------------------------------------------------------------


def build_network(
    edges_path: str,
    snap_tolerance_m: float = 25.0,
    ref_filter: str = "",
    recreate: bool = False,
    heartbeat=None,
    run_id: str = "",
) -> NetworkResult:
    """Node freeway LineStrings into a routable graph cached as a directory artifact.

    Reads ``edges_path`` (GeoJSON LineStrings), optionally keeps only ways whose
    ``ref`` starts with ``ref_filter``, nodes them, and publishes
    ``nodes.geojson`` / ``edges.geojson`` / ``graph.json`` under the ``osm/network``
    cache_type via the staging-then-atomic-rename sidecar protocol. The entry is
    content-addressed (input sha256 + tolerance + ref filter), so a re-run with an
    unchanged input is a cache hit.
    """
    _require_deps()
    from _osm_tools import sidecar, storage
    from facetwork.runtime.storage import localize

    edges_path = str(edges_path)
    local_in = localize(edges_path)
    if not os.path.exists(local_in):
        raise FileNotFoundError(f"BuildNetwork: edges_path not found: {edges_path}")

    input_sha = _sha256_file(local_in)
    rel = _relative_path(edges_path, snap_tolerance_m, ref_filter)
    s = storage.get_storage()
    cache_dir = sidecar.cache_path(NAMESPACE_CACHE, CACHE_TYPE, rel, s)

    existing = sidecar.read_sidecar(NAMESPACE_CACHE, CACHE_TYPE, rel, s)
    if (existing and not recreate
            and (existing.get("source") or {}).get("sha256") == input_sha
            and sidecar.exists_and_valid(NAMESPACE_CACHE, CACHE_TYPE, rel, s)):
        ex = existing.get("extra") or {}
        if heartbeat:
            heartbeat()
        return NetworkResult(
            network_path=cache_dir,
            node_count=ex.get("node_count", 0),
            edge_count=ex.get("edge_count", 0),
            connected_components=ex.get("connected_components", 0),
            largest_component_frac=ex.get("largest_component_frac", 0.0),
            snap_tolerance_m=ex.get("snap_tolerance_m", snap_tolerance_m),
            extraction_date=existing.get("generated_at", ""),
        )

    graph = node_linestrings(
        iter_geojson_features(local_in, heartbeat),
        snap_tolerance_m=snap_tolerance_m, ref_filter=ref_filter,
    )

    staging = _staging_dir()
    try:
        _write_nodes_geojson(Path(staging) / "nodes.geojson", graph.nodes)
        _write_edges_geojson(Path(staging) / "edges.geojson", graph.edges)
        _write_graph_json(Path(staging) / "graph.json", graph.adjacency)
        # Compute the sidecar's size + primary sha from the LOCAL staging dir,
        # before finalize moves it (works for both local and HDFS backends).
        primary_sha = _sha256_file(str(Path(staging) / "graph.json"))
        size_bytes = _dir_size(Path(staging))

        with sidecar.entry_lock(NAMESPACE_CACHE, CACHE_TYPE, rel, storage=s):
            s.finalize_dir_from_local(staging, cache_dir)
            sidecar.write_sidecar(
                NAMESPACE_CACHE, CACHE_TYPE, rel,
                kind="directory", size_bytes=size_bytes, sha256=primary_sha,
                source={"sha256": input_sha, "input_path": edges_path},
                tool={
                    "command": "osm.Network.BuildNetwork",
                    "library": "shapely+networkx",
                    "shapely": getattr(shapely, "__version__", ""),
                    "networkx": getattr(nx, "__version__", ""),
                },
                extra={
                    "node_count": graph.node_count,
                    "edge_count": graph.edge_count,
                    "connected_components": graph.connected_components,
                    "largest_component_frac": round(graph.largest_component_frac, 6),
                    "snap_tolerance_m": snap_tolerance_m,
                    "ref_filter": ref_filter,
                },
                storage=s,
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    log.info("BuildNetwork: %s -> %d nodes / %d edges (%d components) at %s",
             rel, graph.node_count, graph.edge_count, graph.connected_components, cache_dir)
    return NetworkResult(
        network_path=cache_dir,
        node_count=graph.node_count,
        edge_count=graph.edge_count,
        connected_components=graph.connected_components,
        largest_component_frac=graph.largest_component_frac,
        snap_tolerance_m=snap_tolerance_m,
        extraction_date=_now(),
    )


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
    """Approximate A→B route over a built freeway network (Phase 2)."""
    _require_deps()
    raise NotImplementedError(_PHASE.format(op="ApproxRoute", phase="Phase 2"))


def route_matrix(
    network_path: str,
    points: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> MatrixResult:
    """All-pairs approximate routing over the small network (Phase 3)."""
    _require_deps()
    raise NotImplementedError(_PHASE.format(op="RouteMatrix", phase="Phase 3"))
