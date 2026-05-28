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
* :func:`approx_route`   — snap A/B to nearest nodes, Dijkstra by length, return
  the route plus the closest reachable point to B (``reached_b`` / ``gap_to_b_km``).
* :func:`route_matrix`   — all-pairs over the small graph (one single-source
  Dijkstra per origin); accepts a JSON list, a GeoJSON path, or ``"lon,lat;..."``.

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

from ..shared._output import derive_output_path, ensure_dir, finalize_output_file, open_output
from ..shared.geojson_writer import GeoJSONStreamWriter, iter_geojson_features

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
    from shapely.geometry import Point, shape
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
class ApproxRouteResult:
    """A single approximate route (mirrors the FFL ApproxRouteResult)."""

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
class RouteMatrixResult:
    """All-pairs approximate routing result (mirrors the FFL RouteMatrixResult)."""

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

    # Keep only nodes that participate in a routable edge. A degenerate u==v
    # segment can mint a node via node_for() that never gets an edge; ``g`` has
    # no such isolated node (it only gains nodes through add_edge), so the
    # component count, graph.json, and nodes.geojson all stay mutually consistent.
    nodes = {nid: idx.nodes[nid] for nid in adjacency}

    comps = list(nx.connected_components(g))
    largest = max((len(c) for c in comps), default=0)
    frac = largest / len(nodes) if nodes else 0.0

    return NodedGraph(
        nodes=nodes,
        edges=edges,
        adjacency=adjacency,
        connected_components=len(comps),
        largest_component_frac=frac,
    )


def node_id_graph(features, ref_filter: str = "") -> NodedGraph:
    """Build a routable graph on SHARED OSM NODE IDs — exact topology, no tolerance.

    The fidelity-preserving counterpart to :func:`node_linestrings`. That function
    *reconstructs* connectivity by snapping GeoJSON coordinates within a tolerance,
    which is lossy at interchanges and across per-region extract seams (it both
    misses real connections and falsely joins grade-separated crossings). This one
    uses the OSM node-id sequence carried on each feature
    (``properties["node_ids"]``, aligned 1:1 with the LineString coordinates): two
    ways that reference the *same* OSM node id are connected, and only then —
    exactly how OSRM/pgRouting/Valhalla build their graphs. Because OSM node ids
    are global, separate per-region PBF extracts auto-stitch at shared border
    nodes with zero tolerance.

    Degree-2 interior nodes are contracted away so each edge spans junction→
    junction (a node referenced by >=2 ways, or a way endpoint), matching the
    ``edge=polyline`` artifact contract of :func:`node_linestrings`; the graph
    node ids ARE the OSM node ids. ``ref_filter`` keeps only ways whose ``ref``
    starts with the prefix.
    """
    _require_deps()

    # Pass 1: collect kept ways (node_ids + coords + ref/name); tally how many
    # ways reference each node (>=2 => an intersection vertex) and which nodes are
    # way endpoints (always vertices).
    ways: list[tuple[list[int], list, str, str]] = []
    way_count: dict[int, int] = {}
    endpoints: set[int] = set()
    for feat in features:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        props = feat.get("properties") or {}
        node_ids = props.get("node_ids")
        if not node_ids or len(node_ids) != len(coords) or len(node_ids) < 2:
            continue
        ref = _prop(props, "ref")
        if ref_filter and not ref.startswith(ref_filter):
            continue
        nids = [int(n) for n in node_ids]
        ways.append((nids, coords, ref, _prop(props, "name")))
        for nid in set(nids):
            way_count[nid] = way_count.get(nid, 0) + 1
        endpoints.add(nids[0])
        endpoints.add(nids[-1])

    if not ways:
        return NodedGraph()

    def _is_vertex(nid: int) -> bool:
        return way_count.get(nid, 0) >= 2 or nid in endpoints

    # Pass 2: walk each way, emitting one edge per junction->junction span with
    # the full polyline between them (parallel spans collapse to the shortest).
    g = nx.Graph()
    vcoord: dict[int, tuple[float, float]] = {}
    for nids, coords, ref, name in ways:
        vcoord[nids[0]] = (float(coords[0][0]), float(coords[0][1]))
        last_v = nids[0]
        seg = [[float(coords[0][0]), float(coords[0][1])]]
        for i in range(1, len(nids)):
            cx, cy = float(coords[i][0]), float(coords[i][1])
            seg.append([cx, cy])
            nid = nids[i]
            if not _is_vertex(nid):
                continue
            if nid != last_v and len(seg) >= 2:
                length_m = _line_length_m(seg)
                if length_m > 0:
                    if g.has_edge(last_v, nid):
                        if length_m < g[last_v][nid]["length_m"]:
                            g[last_v][nid].update(length_m=length_m, coords=seg, ref=ref, name=name)
                    else:
                        g.add_edge(last_v, nid, length_m=length_m, coords=seg, ref=ref, name=name)
            vcoord[nid] = (cx, cy)   # every vertex's coordinate, edge emitted or not
            last_v = nid
            seg = [[cx, cy]]

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

    nodes = {nid: vcoord[nid] for nid in adjacency if nid in vcoord}
    comps = list(nx.connected_components(g))
    largest = max((len(c) for c in comps), default=0)
    frac = largest / len(nodes) if nodes else 0.0

    return NodedGraph(
        nodes=nodes,
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


def _relative_path(edges_path: str, snap_tolerance_m: float, ref_filter: str,
                   node_id: bool = False) -> str:
    """Cache relative_path: source stem + the build params that change the graph.

    Node-id topology is tolerance-free, so it gets its own ``@nodeid`` suffix —
    a node-id build and a coordinate-snap build of the same source never collide.
    """
    stem = Path(str(edges_path)).name
    for suffix in (".geojson.gz", ".geojson", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    rel = f"{stem}@nodeid" if node_id else f"{stem}@tol{int(round(snap_tolerance_m))}"
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

    # Peek the first feature: if the extractor preserved OSM node-id sequences
    # (properties.node_ids), build the graph on SHARED node ids (exact topology,
    # tolerance-free); otherwise fall back to coordinate-snap noding. The two
    # modes live in separate cache entries (@nodeid vs @tol<N>).
    import itertools

    feats = iter_geojson_features(local_in, heartbeat)
    first = next(feats, None)
    use_node_ids = bool(first and (first.get("properties") or {}).get("node_ids"))
    if first is not None:
        feats = itertools.chain([first], feats)

    rel = _relative_path(edges_path, snap_tolerance_m, ref_filter, node_id=use_node_ids)
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

    if use_node_ids:
        graph = node_id_graph(feats, ref_filter=ref_filter)
    else:
        graph = node_linestrings(feats, snap_tolerance_m=snap_tolerance_m, ref_filter=ref_filter)

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
                    "topology": "node_id" if use_node_ids else "coord_snap",
                    "snap_tolerance_m": 0.0 if use_node_ids else snap_tolerance_m,
                    "ref_filter": ref_filter,
                },
                storage=s,
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    log.info("BuildNetwork[%s]: %s -> %d nodes / %d edges (%d components) at %s",
             "node_id" if use_node_ids else "coord_snap", rel, graph.node_count,
             graph.edge_count, graph.connected_components, cache_dir)
    return NetworkResult(
        network_path=cache_dir,
        node_count=graph.node_count,
        edge_count=graph.edge_count,
        connected_components=graph.connected_components,
        largest_component_frac=graph.largest_component_frac,
        snap_tolerance_m=snap_tolerance_m,
        extraction_date=_now(),
    )


# ---------------------------------------------------------------------------
# Network load (read-once-per-runner) + routing helpers.
# ---------------------------------------------------------------------------


@dataclass
class _LoadedNetwork:
    """An in-memory, ready-to-route network loaded from a cache artifact."""

    g: Any                                    # networkx.Graph (edge attrs: length_m, edge_idx)
    coords: dict[int, tuple[float, float]]    # node_id -> (lon, lat)
    edge_coords: dict[int, list]              # edge_idx -> [[lon, lat], ...]
    node_ids: list[int]
    tree: Any                                 # STRtree over node Points (for snapping)


# Module-level memo: each runner loads a given network once and answers all
# subsequent routes from RAM (the read-once-per-runner design). Keyed by
# (network_path, sidecar sha256) so a rebuilt artifact at the same path reloads.
_GRAPH_CACHE: dict[tuple[str, str], _LoadedNetwork] = {}


def _load_network(network_path: str, heartbeat=None) -> _LoadedNetwork:
    """Load (and memoize) a built network artifact into an in-memory routable graph."""
    _require_deps()
    from _osm_tools import storage as _storage

    s = _storage.get_storage()
    join = _storage.Storage.join
    meta_path = network_path + ".meta.json"
    sha = ""
    if s.exists(meta_path):
        try:
            sha = (json.loads(s.read_text(meta_path)) or {}).get("sha256", "")
        except Exception:  # pragma: no cover - defensive
            sha = ""

    key = (network_path, sha)
    cached = _GRAPH_CACHE.get(key)
    if cached is not None:
        return cached

    graph_json = json.loads(s.read_text(join(network_path, "graph.json")))
    nodes_fc = json.loads(s.read_text(join(network_path, "nodes.geojson")))
    edges_fc = json.loads(s.read_text(join(network_path, "edges.geojson")))

    g = nx.Graph()
    for nid, adj in graph_json.items():
        u = int(nid)
        for nbr, length_m, edge_idx in adj:
            g.add_edge(u, int(nbr), length_m=float(length_m), edge_idx=int(edge_idx))

    coords = {
        f["properties"]["node_id"]: (f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][1])
        for f in nodes_fc.get("features", [])
    }
    edge_coords = {
        f["properties"]["edge_idx"]: f["geometry"]["coordinates"]
        for f in edges_fc.get("features", [])
    }
    node_ids = list(coords)
    tree = STRtree([Point(coords[n][0], coords[n][1]) for n in node_ids])

    loaded = _LoadedNetwork(g=g, coords=coords, edge_coords=edge_coords,
                            node_ids=node_ids, tree=tree)
    _GRAPH_CACHE[key] = loaded
    if heartbeat:
        heartbeat()
    return loaded


def _snap(net: _LoadedNetwork, lon: float, lat: float) -> int:
    """Snap a coordinate to the nearest network node id."""
    return net.node_ids[int(net.tree.nearest(Point(lon, lat)))]


def _stitch_route(net: _LoadedNetwork, path: list[int]) -> list:
    """Concatenate the edge geometries along a node path into one coordinate list."""
    if len(path) < 2:
        return [list(net.coords[path[0]])] if path else []
    out: list = []
    for u, v in zip(path, path[1:], strict=False):
        data = net.g.get_edge_data(u, v) or {}
        seg = [list(c) for c in net.edge_coords.get(data.get("edge_idx"), [])]
        if not seg:
            seg = [list(net.coords[u]), list(net.coords[v])]
        ulon, ulat = net.coords[u]
        if (_haversine_m(ulon, ulat, seg[0][0], seg[0][1])
                > _haversine_m(ulon, ulat, seg[-1][0], seg[-1][1])):
            seg.reverse()  # orient u -> v
        out.extend(seg if not out else seg[1:])
    return out


def _reconstruct_path(pred: dict, source: int, target: int) -> list[int]:
    """Rebuild the node path ``source``→``target`` from a predecessor map.

    ``nx.dijkstra_predecessor_and_distance`` returns a predecessor map (one entry
    per node, O(V) memory) instead of ``single_source_dijkstra``'s full ``paths``
    dict (a complete node list to *every* node — O(V·path_len), tens of GB on a
    continental motorway+trunk graph). We reconstruct only the few routes we
    actually draw. Returns ``[]`` if ``target`` isn't reachable from ``source``.
    """
    path = [target]
    while path[-1] != source:
        preds = pred.get(path[-1])
        if not preds:
            return []
        path.append(preds[0])
    path.reverse()
    return path


class _NearestReachable:
    """Spatial index for the closest-reachable-node fallback (unreachable pairs).

    When a target city isn't reachable from a source, we route to the reachable
    node *nearest the target*. A linear ``min`` over the source's reachable set is
    O(reachable_nodes) **per unreachable pair** — crippling on a large, fragmented
    continent (e.g. Asia: 2.4 M edges, 3,098 components, ~70 k unreachable pairs).

    The reachable set from any node is its connected component (the graph is
    undirected), so one STRtree per component serves every source in it, turning
    the fallback into O(log n) per pair. Components and per-component trees are
    built lazily on first use, so a fully-connected network pays nothing.
    """

    def __init__(self, net: _LoadedNetwork) -> None:
        comps = list(nx.connected_components(net.g))
        self._net = net
        self._comp_of = {n: i for i, c in enumerate(comps) for n in c}
        self._comp_nodes = [list(c) for c in comps]
        self._trees: dict[int, tuple] = {}

    def nearest(self, source_node: int, lon: float, lat: float) -> int:
        cid = self._comp_of[source_node]
        entry = self._trees.get(cid)
        if entry is None:
            ids = self._comp_nodes[cid]
            tree = STRtree([Point(self._net.coords[n][0], self._net.coords[n][1]) for n in ids])
            entry = (tree, ids)
            self._trees[cid] = entry
        tree, ids = entry
        return ids[int(tree.nearest(Point(lon, lat)))]


def _simplify_coords(coords: list, tol_m: float) -> list:
    """Douglas–Peucker simplify a lon/lat polyline by ~``tol_m`` metres.

    Continental route geometries carry every trunk vertex, which makes the
    rendered HTML enormous (a 1.6 GB Africa map). Simplifying to a tolerance
    (e.g. ~500 m for a continent-scale view) cuts the vertex count 10–100× while
    the line stays visually identical at that zoom. ``tol_m <= 0`` is a no-op."""
    if tol_m <= 0 or len(coords) < 3:
        return coords
    from shapely.geometry import LineString

    simp = LineString(coords).simplify(tol_m / 111_320.0, preserve_topology=False)
    out = [list(c) for c in simp.coords]
    return out if len(out) >= 2 else coords


def _write_route_geojson(path: str, coords: list, props: dict) -> None:
    if len(coords) >= 2:
        geom = {"type": "LineString", "coordinates": coords}
    elif coords:
        geom = {"type": "Point", "coordinates": coords[0]}
    else:  # pragma: no cover - empty network guarded earlier
        geom = {"type": "LineString", "coordinates": []}
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": props, "geometry": geom}]}
    with open_output(path) as f:
        f.write(json.dumps(fc))


def approx_route(
    network_path: str,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> ApproxRouteResult:
    """Approximate A→B route over a built freeway network.

    Snaps A and B to the nearest network nodes, runs single-source Dijkstra from
    A by segment length, and returns the route. If B's node is unreachable from
    A's component, returns the route to the reachable node *closest* to B with
    ``reached_b=False``; ``gap_to_b_km`` is always the straight-line residual from
    the reached on-network point to B (the off-freeway last mile when reached,
    the unreachable gap when not).
    """
    _require_deps()
    net = _load_network(network_path, heartbeat)
    if net.g.number_of_nodes() == 0:
        raise ValueError(f"ApproxRoute: empty network at {network_path}")

    a = _snap(net, from_lon, from_lat)
    b = _snap(net, to_lon, to_lat)

    # Predecessor + distance (O(V) memory) rather than the full paths dict.
    pred, lengths = nx.dijkstra_predecessor_and_distance(net.g, a, weight="length_m")
    if heartbeat:
        heartbeat()

    if b in lengths:
        target, reached_b = b, True
    else:
        target = min(lengths, key=lambda n: _haversine_m(to_lon, to_lat, net.coords[n][0], net.coords[n][1]))
        reached_b = False

    path = _reconstruct_path(pred, a, target)
    rlon, rlat = net.coords[target]
    distance_km = lengths[target] / 1000.0
    gap_km = _haversine_m(to_lon, to_lat, rlon, rlat) / 1000.0

    if output_path is None:
        output_path = derive_output_path(
            "osm-network", "route",
            f"{from_lat:.4f}_{from_lon:.4f}", f"{to_lat:.4f}_{to_lon:.4f}",
            ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)
    _write_route_geojson(
        output_path, _stitch_route(net, path),
        {"distance_km": round(distance_km, 3), "reached_b": reached_b,
         "gap_to_b_km": round(gap_km, 3), "node_hops": len(path)},
    )

    return ApproxRouteResult(
        route_path=output_path,
        distance_km=round(distance_km, 3),
        reached_lat=rlat,
        reached_lon=rlon,
        gap_to_b_km=round(gap_km, 3),
        reached_b=reached_b,
        node_hops=len(path),
        extraction_date=_now(),
    )


def _representative_point(geom: dict) -> tuple[float | None, float | None]:
    """A single (lon, lat) for any geometry — coords for a Point, else the rep point."""
    if not geom:
        return (None, None)
    if geom.get("type") == "Point":
        c = geom.get("coordinates") or []
        return (float(c[0]), float(c[1])) if len(c) >= 2 else (None, None)
    if not geom.get("coordinates"):
        return (None, None)
    try:
        p = shape(geom).representative_point()
        return (p.x, p.y)
    except Exception:  # pragma: no cover - defensive
        return (None, None)


def _points_from_obj(obj) -> list[tuple[float, float, str]]:
    if isinstance(obj, dict) and obj.get("type") == "FeatureCollection":
        out: list[tuple[float, float, str]] = []
        for f in obj.get("features", []):
            props = f.get("properties") or {}
            name = str(props.get("name") or props.get("city") or props.get("place") or "")
            lon, lat = _representative_point(f.get("geometry") or {})
            if lon is not None:
                out.append((lon, lat, name))
        return out
    out = []
    for d in obj:
        if isinstance(d, (list, tuple)):
            lon, lat = d[0], d[1]
            name = str(d[2]) if len(d) > 2 else ""
        else:
            lon, lat, name = d.get("lon"), d.get("lat"), str(d.get("name", ""))
        out.append((float(lon), float(lat), name))
    return out


def _parse_points(points) -> list[tuple[float, float, str]]:
    """Accept a JSON points list, a GeoJSON FeatureCollection (inline or a file
    path), or a ``"lon,lat;lon,lat"`` string — closing the GeoJSON→waypoints gap
    so a merged-cities layer feeds straight into the matrix."""
    if not isinstance(points, str):
        return _points_from_obj(points)
    text = points
    if points.startswith(("s3://", "hdfs://")) or os.path.exists(points):
        from facetwork.runtime.storage import localize
        with open(localize(points)) as f:
            text = f.read()
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return _points_from_obj(json.loads(text))
    out = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        out.append((float(parts[0]), float(parts[1]), ""))
    return out


def route_matrix(
    network_path: str,
    points: str,
    output_path: str | None = None,
    heartbeat=None,
    run_id: str = "",
) -> RouteMatrixResult:
    """All-pairs approximate routing over the small network — pure, no engine.

    Snaps each point to its nearest network node and runs one single-source
    Dijkstra per origin (a full shortest-path tree), so the N×N matrix costs N
    Dijkstras. Each pair carries the on-network ``distance_km`` and ``reached_b``;
    unreachable pairs report the distance to the closest reachable node to the
    target with ``reached_b=false``. Accepts a JSON list, a GeoJSON path, or a
    ``"lon,lat;..."`` string for ``points``.
    """
    _require_deps()
    pts = _parse_points(points)
    if len(pts) < 2:
        raise ValueError(f"RouteMatrix needs >= 2 points, got {len(pts)}")

    net = _load_network(network_path, heartbeat)
    if net.g.number_of_nodes() == 0:
        raise ValueError(f"RouteMatrix: empty network at {network_path}")

    snapped = [
        (_snap(net, lon, lat), lon, lat, name or f"p{i}")
        for i, (lon, lat, name) in enumerate(pts)
    ]

    pairs: list[dict] = []
    reachable = 0
    nr = None  # lazy closest-reachable-node index, built on first unreachable pair
    for si, (snode, _slon, _slat, sname) in enumerate(snapped):
        # distances only — RouteMatrix never needs the (huge) paths dict.
        lengths = nx.single_source_dijkstra_path_length(net.g, snode, weight="length_m")
        if heartbeat:
            heartbeat()
        for ti, (tnode, tlon, tlat, tname) in enumerate(snapped):
            if ti == si:
                continue
            if tnode in lengths:
                dist_km = lengths[tnode] / 1000.0
                gap_km = _haversine_m(tlon, tlat, *net.coords[tnode]) / 1000.0
                reached = True
                reachable += 1
            elif lengths:
                if nr is None:
                    nr = _NearestReachable(net)
                best = nr.nearest(snode, tlon, tlat)
                dist_km = lengths[best] / 1000.0
                gap_km = _haversine_m(tlon, tlat, *net.coords[best]) / 1000.0
                reached = False
            else:  # pragma: no cover - source has no reachable nodes
                dist_km, gap_km, reached = 0.0, 0.0, False
            pairs.append({
                "from": sname, "to": tname,
                "distance_km": round(dist_km, 3), "reached_b": reached,
                "gap_km": round(gap_km, 3),
            })

    if output_path is None:
        output_path = derive_output_path(
            "osm-network", "matrix", str(len(pts)), ext="json", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)
    with open_output(output_path) as f:
        f.write(json.dumps({
            "operation": "route_matrix",
            "point_count": len(pts),
            "pair_count": len(pairs),
            "reachable_count": reachable,
            "pairs": pairs,
            "extraction_date": _now(),
        }, indent=2))

    return RouteMatrixResult(
        result_path=output_path,
        pair_count=len(pairs),
        reachable_count=reachable,
        extraction_date=_now(),
    )


@dataclass
class RouteLayerResult:
    """A drawable all-pairs routes layer (mirrors the FFL RouteLayerResult)."""

    output_path: str = ""
    route_count: int = 0
    reachable_count: int = 0
    point_count: int = 0
    extraction_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "route_count": self.route_count,
            "reachable_count": self.reachable_count,
            "point_count": self.point_count,
            "extraction_date": self.extraction_date,
        }


def route_layer(
    network_path: str,
    points: str,
    output_path: str | None = None,
    simplify_tolerance_m: float = 0.0,
    heartbeat=None,
    run_id: str = "",
) -> RouteLayerResult:
    """All-pairs route *geometries* as one drawable GeoJSON layer — the map
    companion to RouteMatrix (which gives distances, not lines).

    For each unordered pair (i<j) it reconstructs the shortest path over the
    network and writes one LineString feature (props: from/to/distance_km/
    reached_b). Unreachable pairs route to the closest reachable node to the
    target (reached_b=false). One single-source Dijkstra per origin. Accepts the
    same ``points`` forms as RouteMatrix (JSON list, GeoJSON path, "lon,lat;...").
    """
    _require_deps()
    pts = _parse_points(points)
    if len(pts) < 2:
        raise ValueError(f"RouteLayer needs >= 2 points, got {len(pts)}")

    net = _load_network(network_path, heartbeat)
    if net.g.number_of_nodes() == 0:
        raise ValueError(f"RouteLayer: empty network at {network_path}")

    snapped = [
        (_snap(net, lon, lat), lon, lat, name or f"p{i}")
        for i, (lon, lat, name) in enumerate(pts)
    ]

    if output_path is None:
        output_path = derive_output_path(
            "osm-network", "routes", str(len(pts)), ext="geojson", run_id=run_id or None,
        )
    output_path = str(output_path)
    ensure_dir(output_path)

    # Stream route features to a local temp (then finalize onto the backend), and
    # use a predecessor map per source instead of the full paths dict. So we never
    # hold all route polylines, the entire output GeoJSON, or every-node paths in
    # memory at once — what makes a continental motorway+trunk network (millions of
    # edges) routable without exhausting RAM.
    import tempfile

    from facetwork.config import get_temp_dir

    fd, tmp = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(fd)
    route_count = 0
    reachable = 0
    nr = None  # lazy closest-reachable-node index, built on first unreachable pair
    try:
        with GeoJSONStreamWriter(tmp) as writer:
            for i, (snode, _slon, _slat, sname) in enumerate(snapped):
                pred, lengths = nx.dijkstra_predecessor_and_distance(net.g, snode, weight="length_m")
                if heartbeat:
                    heartbeat()
                for j in range(i + 1, len(snapped)):
                    tnode, tlon, tlat, tname = snapped[j]
                    if tnode in lengths:
                        target, dist_km, reached = tnode, lengths[tnode] / 1000.0, True
                    elif lengths:
                        if nr is None:
                            nr = _NearestReachable(net)
                        target = nr.nearest(snode, tlon, tlat)
                        dist_km, reached = lengths[target] / 1000.0, False
                    else:  # pragma: no cover - isolated source
                        continue
                    if reached:
                        reachable += 1
                    coords = _stitch_route(net, _reconstruct_path(pred, snode, target))
                    if simplify_tolerance_m > 0:
                        coords = _simplify_coords(coords, simplify_tolerance_m)
                    if len(coords) < 2:
                        continue
                    writer.write_feature({
                        "type": "Feature",
                        "properties": {"from": sname, "to": tname,
                                       "distance_km": round(dist_km, 3), "reached_b": reached},
                        "geometry": {"type": "LineString", "coordinates": coords},
                    })
                    route_count += 1
                del pred, lengths  # free the per-source maps before the next source
        finalize_output_file(tmp, output_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return RouteLayerResult(
        output_path=output_path,
        route_count=route_count,
        reachable_count=reachable,
        point_count=len(pts),
        extraction_date=_now(),
    )
