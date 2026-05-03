"""Logical edge graph construction from OSM PBF data.

Builds a topology of logical edges (road segments between decision nodes)
from OSM PBF files. Extends the pyosmium pattern from road_extractor.py.
"""

import heapq
import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass

from facetwork.runtime.storage import localize

from ..shared._output import ensure_dir, open_output
from ..shared.scan_progress import ScanProgressTracker, get_file_size

log = logging.getLogger(__name__)

try:
    import osmium

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False

# Functional class scores (spec §4.2)
FC_SCORES: dict[str, float] = {
    "motorway": 1.0,
    "trunk": 0.88,
    "primary": 0.75,
    "secondary": 0.60,
    "tertiary": 0.45,
    "unclassified": 0.30,
    "residential": 0.18,
    "service": 0.08,
    "track": 0.04,
    "path": 0.02,
}

# Road class mapping from OSM highway tag
HIGHWAY_TO_FC: dict[str, str] = {
    "motorway": "motorway",
    "motorway_link": "motorway",
    "trunk": "trunk",
    "trunk_link": "trunk",
    "primary": "primary",
    "primary_link": "primary",
    "secondary": "secondary",
    "secondary_link": "secondary",
    "tertiary": "tertiary",
    "tertiary_link": "tertiary",
    "residential": "residential",
    "living_street": "residential",
    "service": "service",
    "unclassified": "unclassified",
    "track": "track",
    "path": "path",
    "footway": "path",
    "cycleway": "path",
    "bridleway": "path",
}

# Minimum functional classes to consider for zoom pipeline
ROUTABLE_FCS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
}

PAVED_SURFACES = {
    "asphalt",
    "concrete",
    "paved",
    "concrete:plates",
    "concrete:lanes",
    "paving_stones",
    "sett",
    "cobblestone",
}
UNPAVED_SURFACES = {
    "unpaved",
    "gravel",
    "dirt",
    "sand",
    "grass",
    "ground",
    "earth",
    "mud",
    "compacted",
    "fine_gravel",
}

RESTRICTED_ACCESS = {"private", "no", "customers", "delivery"}


@dataclass
class LogicalEdge:
    """One merged road segment between decision nodes."""

    edge_id: int
    from_node: int
    to_node: int
    osm_way_ids: list[int]
    coords: list[tuple[float, float]]  # (lon, lat) polyline
    length_m: float
    fc: str
    fc_score: float
    ref: str
    name: str
    maxspeed: int
    lanes: int
    bridge: bool
    tunnel: bool
    oneway: bool
    surface_unpaved: bool


class RoadGraph:
    """Graph of logical edges with adjacency and spatial lookup."""

    def __init__(self) -> None:
        self.edges: list[LogicalEdge] = []
        self.adj: dict[int, list[int]] = defaultdict(list)  # node → edge IDs
        self.node_coords: dict[int, tuple[float, float]] = {}  # node → (lon, lat)
        self.edge_by_id: dict[int, LogicalEdge] = {}

    def add_edge(self, edge: LogicalEdge) -> None:
        self.edges.append(edge)
        self.edge_by_id[edge.edge_id] = edge
        self.adj[edge.from_node].append(edge.edge_id)
        self.adj[edge.to_node].append(edge.edge_id)

    def neighbors(self, node_id: int) -> list[int]:
        """Return neighbor node IDs reachable from node_id."""
        result = []
        for eid in self.adj.get(node_id, []):
            e = self.edge_by_id[eid]
            other = e.to_node if e.from_node == node_id else e.from_node
            result.append(other)
        return result

    def edges_of(self, node_id: int) -> list[LogicalEdge]:
        """Return all edges incident to node_id."""
        return [self.edge_by_id[eid] for eid in self.adj.get(node_id, [])]

    def shortest_path(self, a: int, b: int) -> list[int]:
        """Dijkstra shortest path returning list of edge IDs."""
        if a == b:
            return []
        dist: dict[int, float] = {a: 0.0}
        prev: dict[int, tuple[int, int]] = {}  # node → (prev_node, edge_id)
        heap = [(0.0, a)]

        while heap:
            d, u = heapq.heappop(heap)
            if u == b:
                break
            if d > dist.get(u, float("inf")):
                continue
            for eid in self.adj.get(u, []):
                e = self.edge_by_id[eid]
                v = e.to_node if e.from_node == u else e.from_node
                nd = d + e.length_m
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = (u, eid)
                    heapq.heappush(heap, (nd, v))

        if b not in prev:
            return []

        path_edges: list[int] = []
        cur = b
        while cur != a:
            p, eid = prev[cur]
            path_edges.append(eid)
            cur = p
        path_edges.reverse()
        return path_edges

    def to_dict(self) -> dict:
        """Serialize graph to a JSON-compatible dict."""
        features = []
        for e in self.edges:
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "edge_id": e.edge_id,
                        "from_node": e.from_node,
                        "to_node": e.to_node,
                        "osm_way_ids": ",".join(str(w) for w in e.osm_way_ids),
                        "length_m": round(e.length_m, 1),
                        "fc": e.fc,
                        "fc_score": round(e.fc_score, 4),
                        "ref": e.ref,
                        "name": e.name,
                        "maxspeed": e.maxspeed,
                        "lanes": e.lanes,
                        "bridge": e.bridge,
                        "tunnel": e.tunnel,
                        "oneway": e.oneway,
                        "surface_unpaved": e.surface_unpaved,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": list(e.coords),
                    },
                }
            )

        node_list = {str(nid): list(coord) for nid, coord in self.node_coords.items()}
        adj_list = {str(nid): eids for nid, eids in self.adj.items()}

        return {
            "geojson": {"type": "FeatureCollection", "features": features},
            "nodes": node_list,
            "adjacency": adj_list,
        }

    def save(self, path: str) -> None:
        """Save graph to JSON file."""
        ensure_dir(path)
        with open_output(path) as f:
            json.dump(self.to_dict(), f)
        log.info(
            "Saved road graph: %d edges, %d nodes → %s",
            len(self.edges),
            len(self.node_coords),
            path,
        )

    @classmethod
    def load(cls, path: str) -> "RoadGraph":
        """Load graph from JSON file."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        graph = cls()

        # Restore nodes
        for nid_str, coord in data.get("nodes", {}).items():
            graph.node_coords[int(nid_str)] = (coord[0], coord[1])

        # Restore edges from GeoJSON features
        for feat in data["geojson"]["features"]:
            props = feat["properties"]
            coords_raw = feat["geometry"]["coordinates"]
            coords = [(c[0], c[1]) for c in coords_raw]
            way_ids = [int(w) for w in props["osm_way_ids"].split(",") if w]

            edge = LogicalEdge(
                edge_id=props["edge_id"],
                from_node=props["from_node"],
                to_node=props["to_node"],
                osm_way_ids=way_ids,
                coords=coords,
                length_m=props["length_m"],
                fc=props["fc"],
                fc_score=props["fc_score"],
                ref=props.get("ref", ""),
                name=props.get("name", ""),
                maxspeed=props.get("maxspeed", 0),
                lanes=props.get("lanes", 0),
                bridge=props.get("bridge", False),
                tunnel=props.get("tunnel", False),
                oneway=props.get("oneway", False),
                surface_unpaved=props.get("surface_unpaved", False),
            )
            graph.add_edge(edge)

        log.info(
            "Loaded road graph: %d edges, %d nodes from %s",
            len(graph.edges),
            len(graph.node_coords),
            path,
        )
        return graph


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Haversine distance in meters between two (lon, lat) points."""
    R = 6_371_000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _polyline_length_m(coords: list[tuple[float, float]]) -> float:
    """Calculate polyline length in meters from (lon, lat) coordinate list."""
    total = 0.0
    for i in range(len(coords) - 1):
        total += _haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
    return total


def _classify_fc(highway: str) -> str:
    """Map OSM highway tag to functional class string."""
    return HIGHWAY_TO_FC.get(highway, "")


def _compute_fc_score(
    fc: str, ref: str, bridge: bool, tunnel: bool, surface_unpaved: bool, access_restricted: bool
) -> float:
    """Compute functional class score with modifiers (spec §4.2)."""
    base = FC_SCORES.get(fc, 0.0)
    score = base
    if ref:
        score += 0.05
    if bridge or tunnel:
        score += 0.03
    if surface_unpaved:
        score -= 0.10
    if access_restricted:
        score -= 0.15
    return max(0.0, min(1.0, score))


def _parse_speed(value: str | None) -> int:
    """Parse maxspeed tag to integer km/h."""
    if not value:
        return 0
    try:
        if "mph" in value.lower():
            cleaned = value.lower().replace("mph", "").strip()
            return int(float(cleaned) * 1.60934)
        cleaned = value.replace("km/h", "").replace("kmh", "").strip()
        return int(float(cleaned))
    except (ValueError, TypeError):
        return 0


def _parse_lanes(value: str | None) -> int:
    """Parse lanes tag to integer."""
    if not value:
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


@dataclass
class _WayData:
    """Intermediate storage for a collected OSM way."""

    way_id: int
    node_refs: list[int]
    fc: str
    ref: str
    name: str
    maxspeed: int
    lanes: int
    bridge: bool
    tunnel: bool
    oneway: bool
    surface_unpaved: bool
    access_restricted: bool


class TopologyHandler(osmium.SimpleHandler if HAS_OSMIUM else object):
    """Two-pass PBF handler that builds logical edge topology.

    Pass 1 (node + way): cache coordinates, collect highway-tagged ways.
    finalize(): identify decision nodes, split ways, merge degree-2 chains.
    """

    def __init__(self, progress: ScanProgressTracker | None = None) -> None:
        if HAS_OSMIUM:
            super().__init__()
        self._progress = progress
        self._node_coords: dict[int, tuple[float, float]] = {}
        self._ways: list[_WayData] = []
        self._node_way_count: dict[int, int] = defaultdict(int)
        self._node_fc_set: dict[int, set[str]] = defaultdict(set)
        self._node_ref_set: dict[int, set[str]] = defaultdict(set)

    def _tags_to_dict(self, tags) -> dict[str, str]:
        return {t.k: t.v for t in tags}

    def node(self, n) -> None:
        if self._progress:
            self._progress.tick("node")
        self._node_coords[n.id] = (n.location.lon, n.location.lat)

    def way(self, w) -> None:
        if self._progress:
            self._progress.tick("way")
        tags = self._tags_to_dict(w.tags)
        highway = tags.get("highway", "")
        fc = _classify_fc(highway)
        if not fc or fc not in ROUTABLE_FCS:
            return

        node_refs = [n.ref for n in w.nodes]
        if len(node_refs) < 2:
            return

        ref_tag = tags.get("ref", "")
        name = tags.get("name", "")
        surface = tags.get("surface", "")
        access = tags.get("access", "")

        wd = _WayData(
            way_id=w.id,
            node_refs=node_refs,
            fc=fc,
            ref=ref_tag,
            name=name,
            maxspeed=_parse_speed(tags.get("maxspeed")),
            lanes=_parse_lanes(tags.get("lanes")),
            bridge="bridge" in tags and tags.get("bridge") != "no",
            tunnel="tunnel" in tags and tags.get("tunnel") != "no",
            oneway=tags.get("oneway", "") in ("yes", "1", "true"),
            surface_unpaved=surface in UNPAVED_SURFACES,
            access_restricted=access in RESTRICTED_ACCESS,
        )
        self._ways.append(wd)

        # Track node usage for decision node detection
        for nid in node_refs:
            self._node_way_count[nid] += 1
            self._node_fc_set[nid].add(fc)
            if ref_tag:
                self._node_ref_set[nid].add(ref_tag)

    def finalize(self) -> RoadGraph:
        """Build logical edge graph from collected ways."""
        # Identify decision nodes (spec §4.1)
        decision_nodes: set[int] = set()
        for nid, count in self._node_way_count.items():
            # Junction: referenced by 3+ ways
            if count >= 3:
                decision_nodes.add(nid)
            # FC change
            elif len(self._node_fc_set.get(nid, set())) > 1:
                decision_nodes.add(nid)
            # Ref change
            elif len(self._node_ref_set.get(nid, set())) > 1:
                decision_nodes.add(nid)

        # Add endpoints of all ways (degree-1 dead ends)
        for wd in self._ways:
            decision_nodes.add(wd.node_refs[0])
            decision_nodes.add(wd.node_refs[-1])

        # Split ways at decision nodes into segments
        segments: list[tuple[list[int], _WayData]] = []
        for wd in self._ways:
            current_seg: list[int] = [wd.node_refs[0]]
            for nid in wd.node_refs[1:]:
                current_seg.append(nid)
                if nid in decision_nodes and len(current_seg) >= 2:
                    segments.append((list(current_seg), wd))
                    current_seg = [nid]

        # Build adjacency for degree-2 merging
        # node → list of (segment_index, is_start)
        node_seg_map: dict[int, list[tuple[int, bool]]] = defaultdict(list)
        for idx, (seg, _wd) in enumerate(segments):
            node_seg_map[seg[0]].append((idx, True))
            node_seg_map[seg[-1]].append((idx, False))

        # Merge degree-2 chains into logical edges
        used = [False] * len(segments)
        graph = RoadGraph()
        edge_id = 0

        for idx, (seg, wd) in enumerate(segments):
            if used[idx]:
                continue
            used[idx] = True

            chain_nodes = list(seg)
            chain_ways = [wd.way_id]
            chain_fc = wd.fc

            # Try to extend forward
            while True:
                tail = chain_nodes[-1]
                candidates = node_seg_map.get(tail, [])
                next_seg = None
                for sidx, is_start in candidates:
                    if used[sidx]:
                        continue
                    s, swd = segments[sidx]
                    # Only merge if same FC
                    if swd.fc != chain_fc:
                        continue
                    # Must be degree-2 junction node
                    if len(node_seg_map.get(tail, [])) != 2:
                        continue
                    if is_start and s[0] == tail:
                        next_seg = (sidx, s, swd, False)
                        break
                    elif not is_start and s[-1] == tail:
                        next_seg = (sidx, s, swd, True)
                        break

                if next_seg is None:
                    break

                sidx, s, swd, reverse = next_seg
                used[sidx] = True
                chain_ways.append(swd.way_id)
                if reverse:
                    chain_nodes.extend(reversed(s[:-1]))
                else:
                    chain_nodes.extend(s[1:])

            # Build coordinate list
            coords: list[tuple[float, float]] = []
            for nid in chain_nodes:
                if nid in self._node_coords:
                    coords.append(self._node_coords[nid])

            if len(coords) < 2:
                continue

            from_node = chain_nodes[0]
            to_node = chain_nodes[-1]
            length_m = _polyline_length_m(coords)

            fc_score = _compute_fc_score(
                wd.fc,
                wd.ref,
                wd.bridge,
                wd.tunnel,
                wd.surface_unpaved,
                wd.access_restricted,
            )

            edge = LogicalEdge(
                edge_id=edge_id,
                from_node=from_node,
                to_node=to_node,
                osm_way_ids=chain_ways,
                coords=coords,
                length_m=length_m,
                fc=wd.fc,
                fc_score=fc_score,
                ref=wd.ref,
                name=wd.name,
                maxspeed=wd.maxspeed,
                lanes=wd.lanes,
                bridge=wd.bridge,
                tunnel=wd.tunnel,
                oneway=wd.oneway,
                surface_unpaved=wd.surface_unpaved,
            )

            graph.add_edge(edge)
            edge_id += 1

            # Record decision node coords
            if from_node in self._node_coords:
                graph.node_coords[from_node] = self._node_coords[from_node]
            if to_node in self._node_coords:
                graph.node_coords[to_node] = self._node_coords[to_node]

        # Free memory
        self._node_coords.clear()
        self._ways.clear()
        self._node_way_count.clear()
        self._node_fc_set.clear()
        self._node_ref_set.clear()

        log.info(
            "Built logical graph: %d edges, %d decision nodes",
            len(graph.edges),
            len(graph.node_coords),
        )
        return graph


def build_logical_graph(pbf_path: str, output_path: str | None = None, step_log=None) -> RoadGraph:
    """Build logical edge graph from a PBF file.

    Args:
        pbf_path: Path to the OSM PBF file.
        output_path: Optional path to save the graph JSON.
        step_log: Optional callback for progress reporting.

    Returns:
        RoadGraph instance.
    """
    if not HAS_OSMIUM:
        raise RuntimeError("pyosmium is required for graph construction")

    local_path = localize(pbf_path)
    file_size = get_file_size(str(local_path))
    progress = ScanProgressTracker(file_size, step_log, label="BuildGraph")

    handler = TopologyHandler(progress=progress)
    handler.apply_file(str(local_path), locations=False)
    progress.finish()
    graph = handler.finalize()

    if output_path:
        graph.save(output_path)

    return graph
