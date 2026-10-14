"""Connect a class-filtered road extract's car ferries to its road network.

A major-roads extract (``osmium tags-filter w/highway=motorway,...,primary
w/route=ferry``) keeps the ferry routes but not the port roads that reach them:
those are service/unclassified/residential ways inside the terminal. Each ferry
is then an island of its own, GraphHopper drops it with the other small
subnetworks, and every crossing is "no path". Measured 2026-10-13 on Europe at
motorway..primary: Great Britain (a 66,000 km network) and Ireland (10,000 km)
were separate pieces from the continent, so their cities could not be routed to.

This adds, for every car-ferry END, the SHORTEST chain of road ways linking it
to a way the filter kept -- nothing else. Flooding outward from a terminal would
pull in the port town's whole street grid; one shortest path per end adds a few
hundred metres per terminal.

Pipeline (one pass over the full source, the rest on small files):

1. scan the filtered extract: car-ferry ends + their coordinates;
2. ``osmium extract`` the source to a box around every end (complete ways);
3. keep car-routable highways from that local cut;
4. Dijkstra from each end to the nearest node of a kept non-ferry way;
5. ``osmium getid -r`` the ways on those paths, merge into the filtered file.

"Car ferry" is GraphHopper's own rule (CarAccessParser): ``route=ferry`` with
``motorcar``/``motor_vehicle`` = yes, or neither tag and no ``foot``/``bicycle``
tag. A passenger-only ferry is skipped by the router, so connecting it would add
roads that route nothing.

CLI::

    python -m _osm_tools.ferry_access SRC FILTERED OUT [--radius-km 3] [--work DIR]

Prints a JSON summary on stdout; logs on stderr; exit 0 on success.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field

EARTH_M = 6_371_008.8

# Highway values GraphHopper's car profile can drive. The local cut is filtered
# to these, so a footway is never used to "connect" a car ferry.
CAR_HIGHWAYS = (
    "motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,"
    "secondary_link,tertiary,tertiary_link,unclassified,residential,living_street,"
    "service,road"
)


def is_car_ferry(tags) -> bool:
    """GraphHopper CarAccessParser's ferry rule. ``tags`` is any mapping with .get."""
    if tags.get("route") not in ("ferry", "shuttle_train"):
        return False
    motorcar = tags.get("motorcar")
    if motorcar is None:
        motorcar = tags.get("motor_vehicle")
    if motorcar == "yes":
        return True
    return motorcar is None and tags.get("foot") is None and tags.get("bicycle") is None


def _hav_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(
        math.radians(lon2 - lon1) / 2
    ) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Summary:
    car_ferries: int = 0
    ends: int = 0
    ends_already_connected: int = 0
    ends_connected: int = 0
    ends_unreachable: int = 0
    ways_added: int = 0
    added_km: float = 0.0
    unreachable_sample: list = field(default_factory=list)


def _log(msg: str) -> None:
    print(f"ferry_access: {msg}", file=sys.stderr, flush=True)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _ferry_ends(filtered_pbf: str):
    """(car-ferry way count, {end node id: (lon, lat)}, {node id of a kept road way})."""
    import osmium

    class H(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.ferries = 0
            self.ends: dict[int, tuple[float, float]] = {}
            self.road_nodes: set[int] = set()

        def way(self, w) -> None:  # noqa: ANN001
            if is_car_ferry(w.tags):
                self.ferries += 1
                for nd in (w.nodes[0], w.nodes[len(w.nodes) - 1]):
                    if nd.location.valid():
                        self.ends[nd.ref] = (nd.location.lon, nd.location.lat)
            elif w.tags.get("highway"):
                self.road_nodes.update(nd.ref for nd in w.nodes)

    h = H()
    h.apply_file(filtered_pbf, locations=True, idx="flex_mem")
    return h.ferries, h.ends, h.road_nodes


def _boxes_geojson(ends: dict[int, tuple[float, float]], radius_km: float, path: str) -> None:
    """One MultiPolygon of a square around every end (overlaps are harmless)."""
    dlat = radius_km / 111.32
    polys = []
    for lon, lat in ends.values():
        dlon = dlat / max(0.05, math.cos(math.radians(lat)))
        w, e = max(-180.0, lon - dlon), min(180.0, lon + dlon)
        s, n = max(-90.0, lat - dlat), min(90.0, lat + dlat)
        polys.append([[[w, s], [e, s], [e, n], [w, n], [w, s]]])
    doc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "MultiPolygon", "coordinates": polys}}
        ],
    }
    with open(path, "w") as f:
        json.dump(doc, f)


def _local_graph(local_pbf: str):
    """Adjacency over the local car roads: node -> [(node, metres, way id)]."""
    import osmium

    adj: dict[int, list[tuple[int, float, int]]] = defaultdict(list)
    way_km: dict[int, float] = {}

    class H(osmium.SimpleHandler):
        def way(self, w) -> None:  # noqa: ANN001
            prev = None
            km = 0.0
            for nd in w.nodes:
                if not nd.location.valid():
                    prev = None
                    continue
                cur = (nd.ref, nd.location.lon, nd.location.lat)
                if prev is not None:
                    m = _hav_m(prev[1], prev[2], cur[1], cur[2])
                    adj[prev[0]].append((cur[0], m, w.id))
                    adj[cur[0]].append((prev[0], m, w.id))
                    km += m / 1000
                prev = cur
            way_km[w.id] = km

    H().apply_file(local_pbf, locations=True, idx="flex_mem")
    return adj, way_km


def _shortest_ways(adj, start: int, targets: set[int], max_m: float) -> list[int] | None:
    """Way ids on the shortest path from ``start`` to any target, or None.

    Undirected: this decides which roads to KEEP, and GraphHopper applies the
    real oneway rules to whatever is kept. A port road is rarely oneway in both
    directions of a terminal."""
    dist = {start: 0.0}
    via: dict[int, tuple[int, int]] = {}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        if u in targets and u != start:
            ways = []
            while u in via:
                u, wid = via[u]
                ways.append(wid)
            return ways
        if d > max_m:
            return None
        for v, m, wid in adj.get(u, ()):
            nd = d + m
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                via[v] = (u, wid)
                heapq.heappush(pq, (nd, v))
    return None


def add_ferry_access(
    src_pbf: str, filtered_pbf: str, out_pbf: str, radius_km: float = 3.0, work_dir: str = ""
) -> Summary:
    """Write ``out_pbf`` = ``filtered_pbf`` + the road ways linking each car-ferry end."""
    s = Summary()
    work = work_dir or tempfile.mkdtemp(prefix="ferry-access-")
    os.makedirs(work, exist_ok=True)
    s.car_ferries, ends, road_nodes = _ferry_ends(filtered_pbf)
    s.ends = len(ends)
    _log(f"{s.car_ferries} car ferries, {s.ends} ends")
    pending = {n: c for n, c in ends.items() if n not in road_nodes}
    s.ends_already_connected = s.ends - len(pending)
    if not pending:
        _run(["osmium", "cat", "--overwrite", "-o", out_pbf, filtered_pbf])
        return s

    boxes = os.path.join(work, "ferry-ends.geojson")
    _boxes_geojson(pending, radius_km, boxes)
    cut = os.path.join(work, "ferry-local.osm.pbf")
    local = os.path.join(work, "ferry-local-roads.osm.pbf")
    _log(f"cutting {len(pending)} unconnected ends from {src_pbf}")
    _run(["osmium", "extract", "--overwrite", "-s", "complete_ways", "-p", boxes,
          "-o", cut, src_pbf])
    _run(["osmium", "tags-filter", "--overwrite", "-o", local, cut,
          f"w/highway={CAR_HIGHWAYS}"])
    adj, way_km = _local_graph(local)

    keep: set[int] = set()
    max_m = radius_km * 1000 * 1.5
    for nid, (lon, lat) in pending.items():
        ways = _shortest_ways(adj, nid, road_nodes, max_m)
        if ways is None:
            s.ends_unreachable += 1
            if len(s.unreachable_sample) < 25:
                s.unreachable_sample.append({"node": nid, "lon": round(lon, 4), "lat": round(lat, 4)})
        else:
            s.ends_connected += 1
            keep.update(ways)
    s.ways_added = len(keep)
    s.added_km = round(sum(way_km.get(w, 0.0) for w in keep), 1)
    _log(f"connected {s.ends_connected}, unreachable {s.ends_unreachable}, "
         f"+{s.ways_added} ways ({s.added_km} km)")

    if not keep:
        _run(["osmium", "cat", "--overwrite", "-o", out_pbf, filtered_pbf])
        return s
    ids = os.path.join(work, "ferry-access-ids.txt")
    with open(ids, "w") as f:
        f.writelines(f"w{w}\n" for w in sorted(keep))
    access = os.path.join(work, "ferry-access.osm.pbf")
    _run(["osmium", "getid", "--overwrite", "-r", "-i", ids, "-o", access, local])
    _run(["osmium", "merge", "--overwrite", "-o", out_pbf, filtered_pbf, access])
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", help="full source extract the filtered one was cut from")
    ap.add_argument("filtered", help="class-filtered extract (keeps route=ferry)")
    ap.add_argument("out", help="output: filtered + ferry access roads")
    ap.add_argument("--radius-km", type=float, default=3.0)
    ap.add_argument("--work", default="", help="scratch dir (default: a temp dir)")
    a = ap.parse_args(argv)
    s = add_ferry_access(a.src, a.filtered, a.out, a.radius_km, a.work)
    print(json.dumps(asdict(s), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
