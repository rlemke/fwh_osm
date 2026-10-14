"""Structural Betweenness Sampling (SBS) for zoom-level road importance.

Builds anchor sets from cities, samples OD pairs, routes via GraphHopper HTTP API
with parallel requests, and accumulates betweenness votes on logical edges.
"""

import json
import logging
import math
import os
import random
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from facetwork.runtime.errors import PermanentError
except ImportError:  # keep the handler importable outside a runtime checkout

    class PermanentError(RuntimeError):  # type: ignore[no-redef]
        pass


from ..shared._output import ensure_dir, open_output, read_storage_json
from .zoom_graph import RoadGraph, _haversine_m

GRAPHHOPPER_API_URL = os.environ.get("GRAPHHOPPER_API_URL", "http://localhost:8989")

# Population thresholds per zoom level for anchor selection (spec §5.1)
ANCHOR_POP_THRESHOLDS: dict[int, int] = {
    2: 500_000,
    3: 200_000,
    4: 80_000,
    5: 30_000,
    6: 10_000,
    7: 5_000,
}

# OSM `place` values that are SETTLEMENTS -- what routes run between. The cities
# scan keeps every place node carrying `population`, and that includes admin
# areas: measured 2026-10-07 across the 51 US-state scans, 48 `state` nodes
# (each state's centroid, e.g. "Washington" at 7,958,180 -- the largest
# "city" in the state, so a z2 anchor), 2,983 `county`, 323 `municipality`, and
# `country` "United States" at 331,893,745, sitting in Kansas. Routing to a
# state's centroid is not routing between cities. A feature with NO place tag
# is kept: unknown is not evidence of an admin area.
SETTLEMENT_PLACES: frozenset[str] = frozenset({"city", "town", "village", "hamlet"})


def is_settlement(props: dict) -> bool:
    place = props.get("place", props.get("place_type"))
    return not place or place in SETTLEMENT_PLACES


# Target anchor counts per zoom (approximate upper bounds)
ANCHOR_TARGETS: dict[int, int] = {
    2: 50,
    3: 200,
    4: 1_000,
    5: 5_000,
    6: 20_000,
    7: 20_000,
}

# Default OD pair counts per zoom (spec §5.2)
DEFAULT_K_PAIRS: dict[int, int] = {
    2: 5_000,
    3: 20_000,
    4: 50_000,
    5: 150_000,
    6: 400_000,
    7: 400_000,
}

# Minimum straight-line distance for OD pairs in km (spec §5.2)
MIN_PAIR_DISTANCE_KM: dict[int, float] = {
    2: 300.0,
    3: 150.0,
    4: 60.0,
    5: 20.0,
    6: 5.0,
    7: 5.0,
}

# --- region scaling ------------------------------------------------------------
#
# MIN_PAIR_DISTANCE_KM and ANCHOR_POP_THRESHOLDS are CONTINENTAL: a z2 route
# crosses countries. Measured 2026-10-08 on 14 US states: z2 routed NOTHING in
# any of them and z3 in only 5 -- Arizona's three z2 cities are 23/155/171 km
# apart (all under 300), Colorado's z3 Denver–Colorado Springs is 101 km (under
# 150), and 9 of 10 states had exactly one 500k city, so no z2 pair existed.
#
# Two rules make a REGION's low zooms route between its own biggest cities:
#   1. the pair minimum scales with the region's extent (never below 5 km, and
#      never above the spec, so a continent behaves exactly as before);
#   2. z2/z3 take at least ANCHOR_MIN_COUNT cities, the largest that are
#      also far enough apart to route -- a floor of the N largest alone would
#      pick one metro (Boise/Meridian/Nampa are within 20 km) and route nothing.

# Region diagonal at which the spec distances apply unscaled.
REGION_REF_KM: float = 2_000.0
MIN_PAIR_FLOOR_KM: float = 5.0
ANCHOR_MIN_COUNT: dict[int, int] = {2: 3, 3: 6}


def region_scale(graph: RoadGraph) -> float:
    """Diagonal of the region's road network over REGION_REF_KM, capped at 1.

    Uses the 2nd–98th percentile of node coordinates: an extract can carry a
    ferry line to another state (Washington's reaches Alaska), and a raw bbox
    would read that as a 3,000 km region and scale nothing."""
    coords = list(graph.node_coords.values())
    if len(coords) < 2:
        return 1.0
    lons = sorted(c[0] for c in coords)
    lats = sorted(c[1] for c in coords)
    lo, hi = int(len(lons) * 0.02), max(0, int(len(lons) * 0.98) - 1)
    diag_km = _haversine_m(lons[lo], lats[lo], lons[hi], lats[hi]) / 1000.0
    return max(0.0, min(1.0, diag_km / REGION_REF_KM))


def min_pair_km(zoom_level: int, scale: float = 1.0) -> float:
    """The OD pair minimum for this zoom in a region of this scale."""
    spec = MIN_PAIR_DISTANCE_KM.get(zoom_level, 5.0)
    return max(min(spec, MIN_PAIR_FLOOR_KM), spec * min(1.0, scale))


class SegmentIndex:
    """Grid-based spatial index for snapping route coordinates to logical edges.

    Held as flat numpy arrays, not Python objects, for two reasons:

    * size -- the object form (a list of 4-tuples per edge plus a dict of
      (edge, segment) tuples per ~500 m cell) is most of a country builder's
      resident memory;
    * sharing -- ``save`` writes the arrays to a directory and ``load`` maps
      them read-only, so the snap worker processes (``SnapPool``) share ONE copy
      through the page cache. Forking the object form instead would copy it page
      by page as each worker's reference counting touched it.

    Arrays (S segments, E edges, C occupied cells):
      seg_xy     (S, 4) float64  x1, y1, x2, y2 of each segment, edge by edge
      seg_edge   (S,)   int32    row of the edge owning each segment
      edge_start (E+1,) int64    first segment of each edge row
      edge_ids   (E,)   int64    edge_id of each row
      cell_keys  (C,)   int64    sorted cell keys
      cell_start (C+1,) int64    first entry of each cell in cell_segs
      cell_segs  (N,)   int32    segment ids, grouped by cell
    """

    CELL_SIZE = 0.005  # ~500m in degrees
    _CELL_OFF = 1 << 19  # keeps cell coordinates positive (|lat|/0.005 <= 36,000)
    _CELL_MUL = 1 << 20

    ARRAYS = ("seg_xy", "seg_edge", "edge_start", "edge_ids", "cell_keys", "cell_start", "cell_segs")

    # Memo grid for snap_route: routes overlap heavily (every long route rides
    # the same backbone), so the same coordinate is snapped thousands of times.
    # 1e-4 deg ≈ 11 m at these latitudes, well inside the 50 m tolerance.
    SNAP_MEMO_SCALE = 10_000
    _MEMO_MUL = 4_000_000  # > the span of int(lat * SNAP_MEMO_SCALE)

    # Entries kept in the snap memo before it is dropped and refilled. Measured
    # 2026-10-12: unbounded, on the US major-roads graph the builder reached
    # ~40 GiB and the kernel killed it three times (each a 3-hour reroute after
    # the reclaim). SnapPool divides this between its workers.
    SNAP_MEMO_MAX = 10_000_000

    # Edges whose segment coordinates are kept as Python lists for the
    # follow-the-edge loop (reading numpy scalars there is several times slower).
    EDGE_CACHE_MAX = 100_000

    def __init__(self, graph: RoadGraph | None = None, *, arrays: dict | None = None) -> None:
        if arrays is None:
            arrays = self._build_arrays(graph)
        for name in self.ARRAYS:
            setattr(self, "_" + name, arrays[name])
        self._snap_memo: dict[int, int] = {}
        self._edge_cache: dict[int, tuple[int, int, int, list]] = {}
        self.snap_hits = 0
        self.snap_misses = 0
        self.snap_sticky = 0
        self.memo_resets = 0

    # -- construction ---------------------------------------------------------

    @classmethod
    def _build_arrays(cls, graph) -> dict:
        import itertools
        from array import array

        import numpy as np

        flat = array("d")
        npts = array("q")
        ids = array("q")
        for edge in graph.edges:
            coords = edge.coords
            ids.append(edge.edge_id)
            npts.append(len(coords))
            flat.extend(itertools.chain.from_iterable((c[0], c[1]) for c in coords))
        pts = np.frombuffer(flat, dtype=np.float64).reshape(-1, 2) if len(flat) else np.zeros((0, 2))
        npts_a = np.frombuffer(npts, dtype=np.int64) if len(npts) else np.zeros(0, np.int64)
        edge_ids = np.frombuffer(ids, dtype=np.int64).copy() if len(ids) else np.zeros(0, np.int64)
        nseg = np.maximum(npts_a - 1, 0)
        edge_start = np.zeros(len(nseg) + 1, dtype=np.int64)
        np.cumsum(nseg, out=edge_start[1:])
        # A segment starts at every point except the last of its edge.
        is_start = np.ones(len(pts), dtype=bool)
        pend = np.cumsum(npts_a)
        is_start[pend[npts_a > 0] - 1] = False
        starts = np.flatnonzero(is_start)
        seg_xy = np.ascontiguousarray(np.concatenate([pts[starts], pts[starts + 1]], axis=1))
        seg_edge = np.repeat(np.arange(len(nseg), dtype=np.int32), nseg)

        # Each segment goes into every cell its bounding box touches; a query
        # searches the point's cell and its 8 neighbours. A cell is ~500 m (at
        # least ~170 m of longitude at 70° N), so any segment within the 50 m
        # tolerance of a point lies in one of those nine cells.
        c = cls.CELL_SIZE
        x1, y1, x2, y2 = seg_xy[:, 0], seg_xy[:, 1], seg_xy[:, 2], seg_xy[:, 3]
        cx0 = np.floor(np.minimum(x1, x2) / c).astype(np.int64)
        cx1 = np.floor(np.maximum(x1, x2) / c).astype(np.int64)
        cy0 = np.floor(np.minimum(y1, y2) / c).astype(np.int64)
        cy1 = np.floor(np.maximum(y1, y2) / c).astype(np.int64)
        w = cx1 - cx0 + 1
        n = w * (cy1 - cy0 + 1)
        total = int(n.sum())
        seg_rep = np.repeat(np.arange(len(seg_xy), dtype=np.int64), n)
        k = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(n) - n, n)
        wr = w[seg_rep]
        keys = cls._cell_key_np(cx0[seg_rep] + k % wr, cy0[seg_rep] + k // wr)
        del k, wr
        order = np.argsort(keys, kind="stable")
        keys = keys[order]
        cell_segs = seg_rep[order].astype(np.int32)
        del order, seg_rep
        cell_keys, first = np.unique(keys, return_index=True)
        cell_start = np.append(first, total).astype(np.int64)
        return {
            "seg_xy": seg_xy, "seg_edge": seg_edge, "edge_start": edge_start,
            "edge_ids": edge_ids, "cell_keys": cell_keys, "cell_start": cell_start,
            "cell_segs": cell_segs,
        }

    @classmethod
    def _cell_key_np(cls, cx, cy):
        return (cx + cls._CELL_OFF) * cls._CELL_MUL + (cy + cls._CELL_OFF)

    def save(self, directory: str) -> None:
        """Write the arrays to ``directory`` (one .npy each) for ``load``."""
        import numpy as np

        os.makedirs(directory, exist_ok=True)
        for name in self.ARRAYS:
            np.save(os.path.join(directory, name + ".npy"), getattr(self, "_" + name))

    @classmethod
    def load(cls, directory: str, mmap: bool = True) -> "SegmentIndex":
        """An index over arrays ``save`` wrote; memory-mapped read-only by default."""
        import numpy as np

        mode = "r" if mmap else None
        return cls(arrays={n: np.load(os.path.join(directory, n + ".npy"), mmap_mode=mode)
                           for n in cls.ARRAYS})

    @property
    def nbytes(self) -> int:
        return sum(int(getattr(self, "_" + n).nbytes) for n in self.ARRAYS)

    @property
    def segment_count(self) -> int:
        return len(self._seg_xy)

    def clear_memo(self) -> None:
        """Drop the snap memo and edge cache (the grid index itself is kept)."""
        self._snap_memo = {}
        self._edge_cache = {}

    # -- lookup ---------------------------------------------------------------

    @staticmethod
    def _point_to_segment_dist_m(
        px: float, py: float, x1: float, y1: float, x2: float, y2: float
    ) -> float:
        """Perpendicular distance from point to line segment in meters.

        Uses flat-earth approximation with cos(lat) scaling.
        """
        cos_lat = math.cos(math.radians(py))
        # Convert to approximate meters
        dx = (x2 - x1) * cos_lat
        dy = y2 - y1
        if dx == 0 and dy == 0:
            return _haversine_m(px, py, x1, y1)

        t = ((px - x1) * cos_lat * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))

        proj_lon = x1 + t * (x2 - x1)
        proj_lat = y1 + t * (y2 - y1)
        return _haversine_m(px, py, proj_lon, proj_lat)

    def _nearest_segment(self, lon: float, lat: float, tolerance_m: float) -> int:
        """Global id of the nearest segment within ``tolerance_m``, or -1.

        Vectorised form of ``_point_to_segment_dist_m`` over the nine cells."""
        import numpy as np

        keys_arr = self._cell_keys
        if not len(keys_arr):
            return -1
        c = self.CELL_SIZE
        cx, cy = math.floor(lon / c), math.floor(lat / c)
        want = np.array([self._cell_key_np(cx + dx, cy + dy)
                         for dx in (-1, 0, 1) for dy in (-1, 0, 1)], dtype=np.int64)
        pos = np.searchsorted(keys_arr, want)
        cs = self._cell_start
        parts = []
        for k, p in zip(want.tolist(), pos.tolist(), strict=True):
            if p < len(keys_arr) and int(keys_arr[p]) == k:
                parts.append(self._cell_segs[int(cs[p]):int(cs[p + 1])])
        if not parts:
            return -1
        cand = np.concatenate(parts)
        seg = self._seg_xy[cand]
        x1, y1, x2, y2 = seg[:, 0], seg[:, 1], seg[:, 2], seg[:, 3]
        cos_lat = math.cos(math.radians(lat))
        dx = (x2 - x1) * cos_lat
        dy = y2 - y1
        den = dx * dx + dy * dy
        num = (lon - x1) * cos_lat * dx + (lat - y1) * dy
        t = np.clip(np.divide(num, den, out=np.zeros_like(num), where=den != 0), 0.0, 1.0)
        plon = x1 + t * (x2 - x1)
        plat = y1 + t * (y2 - y1)
        # _haversine_m, vectorised
        p1, p2 = math.radians(lat), np.radians(plat)
        a = (np.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * np.cos(p2) * np.sin(np.radians(plon - lon) / 2) ** 2)
        d = 6_371_000 * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
        j = int(np.argmin(d))
        return int(cand[j]) if d[j] < tolerance_m else -1

    def _edge_of(self, seg: int) -> tuple[int, int, int, list]:
        """(first segment, end segment, edge_id, segment coords) of ``seg``'s edge."""
        row = int(self._seg_edge[seg])
        hit = self._edge_cache.get(row)
        if hit is None:
            lo, hi = int(self._edge_start[row]), int(self._edge_start[row + 1])
            hit = (lo, hi, int(self._edge_ids[row]), self._seg_xy[lo:hi].tolist())
            if len(self._edge_cache) >= self.EDGE_CACHE_MAX:
                self._edge_cache = {}
            self._edge_cache[row] = hit
        return hit

    # Interpolation step along a route before snapping, in metres.
    DENSIFY_STEP_M: float = 20.0

    @classmethod
    def densify(cls, route_coords: list[list[float]]) -> Iterator[tuple[float, float]]:
        """The route's vertices plus points every DENSIFY_STEP_M between them.

        ⚠️ GraphHopper simplifies the geometry it returns (Douglas-Peucker), so on
        a straight run consecutive points can be kilometres apart, and snapping
        only the vertices never touches a short edge lying between them -- a
        bridge, an interchange segment. Measured 2026-10-09 on California-north:
        208 US-101 edges unrouted with routed edges on BOTH ends, median 193 m.
        The route drove them; the matcher never looked."""
        prev = None
        for c in route_coords:
            lon, lat = c[0], c[1]
            if prev is not None:
                d = _haversine_m(prev[0], prev[1], lon, lat)
                n = int(d // cls.DENSIFY_STEP_M)
                for i in range(1, n + 1):
                    t = i / (n + 1)
                    yield prev[0] + (lon - prev[0]) * t, prev[1] + (lat - prev[1]) * t
            yield lon, lat
            prev = (lon, lat)

    # Follow-the-edge matching (see snap_route): a point within this distance of
    # the edge the previous point matched stays on it without a grid search.
    # Route geometry lies ON the OSM way (GraphHopper returns its nodes, and the
    # densified points sit on straight chords between them), so riding an edge
    # reads ~0 m from it, while a parallel carriageway or frontage road is
    # 10+ m away -- inside this distance the current edge IS the nearest one.
    STICKY_M = 5.0
    # Segments either side of the last matched one to try before giving up.
    STICKY_WINDOW = 3

    def snap_route(self, route_coords: list[list[float]], tolerance_m: float = 50.0) -> set[int]:
        """Snap a route to the logical edge IDs it rides (within tolerance).

        ⚠️ Follows the current edge before searching the grid. Measured
        2026-10-12 on the US major-roads graph: with a grid search (memoised at
        ~11 m) per 20 m point, a 4,700 km route is ~235k points, nearly all cache
        misses at continental scale -- 24 s per route, 2.5 routes/min, and a memo
        that grew to 26 GiB. Points that ride the edge just matched cost a few
        segment distances instead; the grid search runs at junctions."""
        matched_edges: set[int] = set()
        memo = self._snap_memo
        scale, mul = self.SNAP_MEMO_SCALE, self._MEMO_MUL
        last_key: int | None = None
        cur: tuple[int, int, int, list] | None = None
        cur_seg = -1
        sticky_m, window = self.STICKY_M, self.STICKY_WINDOW
        dist = self._point_to_segment_dist_m

        for lon, lat in self.densify(route_coords):
            if cur is not None:
                lo, hi, _eid, xy = cur
                best_i, best_d = -1, sticky_m
                for i in range(max(lo, cur_seg - window), min(hi, cur_seg + window + 1)):
                    x1, y1, x2, y2 = xy[i - lo]
                    d = dist(lon, lat, x1, y1, x2, y2)
                    if d <= best_d:
                        best_i, best_d = i, d
                if best_i >= 0:
                    cur_seg = best_i
                    self.snap_sticky += 1
                    continue
            key = int(lon * scale) * mul + int(lat * scale)
            if key == last_key:
                continue
            last_key = key
            seg = memo.get(key)
            if seg is not None:
                self.snap_hits += 1
            else:
                self.snap_misses += 1
                seg = self._nearest_segment(lon, lat, tolerance_m)
                if len(memo) >= self.SNAP_MEMO_MAX:
                    memo = self._snap_memo = {}
                    self.memo_resets += 1
                memo[key] = seg
            if seg >= 0:
                cur = self._edge_of(seg)
                cur_seg = seg
                matched_edges.add(cur[2])
            else:
                cur, cur_seg = None, -1

        return matched_edges


# -- parallel snapping ---------------------------------------------------------

_WORKER_INDEX: "SegmentIndex | None" = None


def _snap_worker_init(index_dir: str, memo_max: int) -> None:
    global _WORKER_INDEX
    _WORKER_INDEX = SegmentIndex.load(index_dir)
    _WORKER_INDEX.SNAP_MEMO_MAX = memo_max


def _snap_worker(coords: list[list[float]]) -> tuple[list[int], int, int, int]:
    """(edge ids, memo hits, misses, sticky) for one route, in a pool worker."""
    idx = _WORKER_INDEX
    assert idx is not None, "snap worker used before its initializer ran"
    h, m, s = idx.snap_hits, idx.snap_misses, idx.snap_sticky
    eids = idx.snap_route(coords)
    return list(eids), idx.snap_hits - h, idx.snap_misses - m, idx.snap_sticky - s


def snap_workers_default() -> int:
    """Worker processes for SnapPool: ``FW_ZOOM_SNAP_WORKERS``, else cores - 2,
    at most 8. 0 or 1 snaps in-process (no pool)."""
    raw = os.environ.get("FW_ZOOM_SNAP_WORKERS", "").strip()
    if raw:
        return max(0, int(raw))
    return max(1, min(8, (os.cpu_count() or 2) - 2))


class SnapPool:
    """Snap routes in worker processes sharing one memory-mapped SegmentIndex.

    Measured 2026-10-14 on the US major-roads graph: routing 15,317 z3+z4 pairs
    took 2 h 30 m with GraphHopper at ~0% CPU and the builder at 100% of ONE
    core -- the snap, not the router, was the whole cost.

    Workers are SPAWNED, not forked: the runner process has threads (heartbeat,
    the routing pool, the Mongo client), and a fork can copy a lock held by one
    of them. The index goes to ``work_dir`` once and every worker maps it, so the
    pool adds each worker's memo (SNAP_MEMO_MAX split between them) and an
    interpreter, not a copy of the index.

    Measured 2026-10-14 on Texas (457k edges, 3.66M segments, 400 z4 routes):
    the object index snapped 277 routes/min and built in 9 s; this array index
    snaps 636/min in-process (identical edges on all 400 routes) and builds in
    1.1 s; 8 workers snap 3,300/min.

    ⚠️ Results are not bit-identical to an in-process run, and never were. The
    memo answers for an ~11 m cell with whatever the FIRST point in it matched,
    so at a junction it depends on which routes came before -- one process
    snapping the same 400 routes in reverse order differs on 461 of 224,218
    route-edges. Eight workers each see a different order: 341 differ. Same
    noise, not a new error.
    """

    def __init__(self, index: SegmentIndex, workers: int, work_dir: str) -> None:
        import multiprocessing
        import shutil
        import tempfile
        from concurrent.futures import ProcessPoolExecutor

        self.workers = workers
        self._rmtree = shutil.rmtree
        self.dir = tempfile.mkdtemp(prefix="snap-index-", dir=work_dir or None)
        index.save(self.dir)
        memo_max = max(10_000, SegmentIndex.SNAP_MEMO_MAX // max(1, workers))
        self._pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_snap_worker_init,
            initargs=(self.dir, memo_max),
        )
        log.info("SnapPool: %d workers over a %.0f MB index (%d segments) in %s",
                 workers, index.nbytes / 1e6, index.segment_count, self.dir)

    def submit(self, coords: list[list[float]]):
        return self._pool.submit(_snap_worker, coords)

    def close(self, cancel: bool = False) -> None:
        self._pool.shutdown(wait=not cancel, cancel_futures=cancel)
        self._rmtree(self.dir, ignore_errors=True)

    def __enter__(self) -> "SnapPool":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(cancel=exc_type is not None)


def build_anchors(
    graph: RoadGraph,
    cities_path: str,
    zoom_level: int,
    scale: float = 1.0,
    chosen_out: list[dict] | None = None,
    carry: list[tuple] | None = None,
) -> list[int]:
    """Build anchor node set for a zoom level from city data.

    Args:
        graph: The logical edge graph.
        cities_path: Path to cities GeoJSON file.
        zoom_level: Zoom level (2–7).
        scale: region_scale() -- sets the spacing the z2/z3 floor enforces.
        chosen_out: when given, filled with the city features actually anchored
            (the map draws exactly these, rather than re-deriving the rule).
        carry: cities anchored at the LOWER zooms, as returned through
            ``carry`` by the previous call -- they stay anchors here, and the
            list is extended with this zoom's picks for the next call.

    Returns:
        List of anchor node IDs.
    """
    pop_threshold = ANCHOR_POP_THRESHOLDS.get(zoom_level, 10_000)
    target_count = ANCHOR_TARGETS.get(zoom_level, 1_000)

    # Load every settlement; the threshold and the floor choose from them.
    pool: list[tuple[float, float, int, dict]] = []  # (lon, lat, population, props)
    try:
        geojson = read_storage_json(cities_path)
        for feat in geojson.get("features", []):
            props = feat.get("properties", {})
            if not is_settlement(props):
                continue
            pop = props.get("population", 0)
            if not isinstance(pop, (int, float)):
                try:
                    pop = int(pop)
                except (ValueError, TypeError):
                    pop = 0
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [])
            if len(coords) >= 2 and pop > 0:
                pool.append((coords[0], coords[1], int(pop), props))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load cities from %s: %s", cities_path, e)

    pool.sort(key=lambda c: c[2], reverse=True)
    # Cumulative: a city anchored at a lower zoom stays an anchor here. Without
    # this the z2/z3 floors made a HOLE -- Montana's z3 had 6 cities (floor)
    # while z4's own threshold (80k) admitted only Billings, so z4 routed
    # nothing between two zooms that did.
    cities = list(carry or [])
    for c in pool:
        if len(cities) >= max(target_count, len(carry or [])):
            break
        if c[2] >= pop_threshold and c not in cities:
            cities.append(c)

    # Floor (z2/z3): too few cities at the threshold -> add the next-largest
    # that are at least this zoom's pair minimum from every city already in,
    # so each one added can actually be routed to.
    floor = ANCHOR_MIN_COUNT.get(zoom_level, 0)
    added = 0
    if len(cities) < floor:
        min_m = min_pair_km(zoom_level, scale) * 1000.0
        for c in pool:
            if len(cities) >= floor:
                break
            if c in cities:
                continue
            if all(_haversine_m(c[0], c[1], o[0], o[1]) >= min_m for o in cities):
                cities.append(c)
                added += 1
        if added:
            log.info(
                "z%d: %d city(ies) below %d added to reach the floor of %d (>= %.0f km apart)",
                zoom_level, added, pop_threshold, floor, min_m / 1000.0,
            )

    if carry is not None:
        carry[:] = cities

    # Snap each city to nearest routable node
    anchors: list[int] = []
    for lon, lat, _pop, props in cities:
        best_node = _snap_to_nearest_node(graph, lon, lat)
        if best_node is not None and best_node not in anchors:
            anchors.append(best_node)
            if chosen_out is not None:
                chosen_out.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "name": props.get("name") or "(unnamed)",
                            "place": props.get("place") or "",
                            "population": _pop,
                        },
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    }
                )

    # No topology top-up. This used to pad any zoom with fewer than
    # max(10, target/10) anchors with the highest-degree road junctions -- so
    # Washington's z2 routed between Seattle, its own state centroid and 48
    # junctions, and the "routed" roads ran between places that are not on the
    # map. A zoom with fewer than two settlements now routes nothing, which is
    # the honest answer: its roads come from class and name alone.
    if len(anchors) < 2:
        log.info(
            "z%d: %d settlement(s) at or above %d -- no routing at this zoom",
            zoom_level, len(anchors), pop_threshold,
        )

    log.info(
        "Built %d anchors for zoom %d (pop threshold %d)", len(anchors), zoom_level, pop_threshold
    )
    return anchors


# Grid cell for the nearest-node index, degrees. ~11 km of latitude: a city
# snaps within a few cells, and a cell holds a few thousand nodes at most.
NODE_GRID_DEG = 0.1
# Metres per degree on the haversine sphere, shaded down 1% so the ring bound
# below never overstates a distance.
_M_PER_DEG = 0.99 * math.radians(1.0) * 6_371_000.0


class NodeGrid:
    """Nearest-node lookup over a graph's nodes, bucketed by NODE_GRID_DEG cells.

    Replaces a scan of EVERY node per city. Measured 2026-10-11 on the US
    major-roads graph: 10-20 min per zoom with that scan and no heartbeat, so the
    stuck-task watchdog reclaimed the builder five times and dead-lettered it.
    Returns exactly what the scan returned: the nearest node by haversine within
    max_dist_m, ties going to the node that comes first in node_coords."""

    def __init__(self, node_coords: dict[int, tuple[float, float]]) -> None:
        self.cells: dict[tuple[int, int], list[tuple[int, int, float, float]]] = defaultdict(list)
        for order, (nid, (lon, lat)) in enumerate(node_coords.items()):
            self.cells[self._cell(lon, lat)].append((order, nid, lon, lat))

    @staticmethod
    def _cell(lon: float, lat: float) -> tuple[int, int]:
        return (math.floor(lon / NODE_GRID_DEG), math.floor(lat / NODE_GRID_DEG))

    def nearest(self, lon: float, lat: float, max_dist_m: float) -> int | None:
        cx, cy = self._cell(lon, lat)
        best: tuple[float, int, int] | None = None  # (distance, order, node)
        r = 0
        while True:
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue  # ring r only
                    for order, nid, nlon, nlat in self.cells.get((cx + dx, cy + dy), ()):
                        d = _haversine_m(lon, lat, nlon, nlat)
                        if d < max_dist_m and (best is None or (d, order) < best[:2]):
                            best = (d, order, nid)
            # Every node outside ring r is at least r cells away on one axis.
            # Bound the east-west metres at the most poleward latitude it could
            # have, so the bound never overstates the distance.
            far_lat = min(89.9, abs(lat) + (r + 1) * NODE_GRID_DEG)
            lower = r * NODE_GRID_DEG * _M_PER_DEG * math.cos(math.radians(far_lat))
            if lower >= max_dist_m or (best is not None and lower > best[0]):
                return None if best is None else best[2]
            r += 1


def _snap_to_nearest_node(
    graph: RoadGraph, lon: float, lat: float, max_dist_m: float = 50_000.0
) -> int | None:
    """Find the nearest graph node to a (lon, lat) point (indexed; built once per graph)."""
    grid = getattr(graph, "_node_grid", None)
    if grid is None or getattr(graph, "_node_grid_n", -1) != len(graph.node_coords):
        grid = NodeGrid(graph.node_coords)
        try:
            graph._node_grid = grid
            graph._node_grid_n = len(graph.node_coords)
        except AttributeError:
            pass
    return grid.nearest(lon, lat, max_dist_m)


def sample_od_pairs(
    anchors: list[int],
    zoom_level: int,
    graph: RoadGraph,
    k_pairs: int | None = None,
    scale: float = 1.0,
) -> list[tuple[int, int]]:
    """Sample origin-destination pairs from anchors for SBS.

    Args:
        anchors: List of anchor node IDs.
        zoom_level: Zoom level (2–7).
        graph: The road graph (for distance filtering).
        k_pairs: Number of pairs to sample (default per zoom level).

    Returns:
        Sorted list of (origin, destination) node ID tuples.
    """
    if k_pairs is None:
        k_pairs = DEFAULT_K_PAIRS.get(zoom_level, 5_000)

    min_dist_km = min_pair_km(zoom_level, scale)
    min_dist_m = min_dist_km * 1_000

    rng = random.Random(42)

    # Generate all valid pairs
    valid_pairs: list[tuple[int, int]] = []
    for i, a in enumerate(anchors):
        for b in anchors[i + 1 :]:
            if a in graph.node_coords and b in graph.node_coords:
                alon, alat = graph.node_coords[a]
                blon, blat = graph.node_coords[b]
                d = _haversine_m(alon, alat, blon, blat)
                if d >= min_dist_m:
                    valid_pairs.append((a, b))

    # Sample
    if len(valid_pairs) > k_pairs:
        rng.shuffle(valid_pairs)
        valid_pairs = valid_pairs[:k_pairs]

    valid_pairs.sort()
    log.info(
        "Sampled %d OD pairs for zoom %d from %d anchors (min %.0f km)",
        len(valid_pairs),
        zoom_level,
        len(anchors),
        min_dist_km,
    )
    return valid_pairs


# How a routing attempt ended. The distinction is load-bearing: two of these mean
# the SERVER is wrong and the run must stop, and one means the DATA has no route
# and the run must continue.
#   ok         a path came back
#   no_path    GraphHopper resolved BOTH points into this graph and found no route
#              between them. Measured 2026-09-25 on Hawaii: zoom 2 samples pairs at
#              least 300 km apart, which is inter-island, and no car route exists.
#              This is an answer about the data, and it PROVES the right region is
#              loaded, because an unloaded point could not have been snapped.
#   oob        "Point N is out of bounds" — the graph does not cover these
#              coordinates, i.e. the server holds another region.
#   transport  the server did not answer at all (down, restarting, unreachable).
#   other      any other non-200.
ROUTE_OK = "ok"
ROUTE_NO_PATH = "no_path"
ROUTE_OOB = "oob"
ROUTE_TRANSPORT = "transport"
ROUTE_OTHER = "other"
# Reasons that indict the SERVER rather than the pair.
ROUTE_SERVER_FAULTS = frozenset({ROUTE_OOB, ROUTE_TRANSPORT, ROUTE_OTHER})


def _classify(status: int, body: str) -> str:
    if "out of bounds" in body or "cannot find point" in body.lower():
        return ROUTE_OOB
    if "ConnectionNotFound" in body or "Connection between locations not found" in body:
        return ROUTE_NO_PATH
    return ROUTE_OTHER


def _route_pair(
    from_node: int,
    to_node: int,
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
    tally: dict[str, int] | None = None,
    detail: list[str] | None = None,
) -> list[list[float]] | None:
    """Query GraphHopper HTTP API for fastest route between two nodes.

    ``tally`` counts outcomes by the reasons above, so a caller can tell a region
    the server cannot serve from pairs the map genuinely cannot connect.
    """
    if not HAS_REQUESTS:
        return None

    from_coord = node_coords.get(from_node)
    to_coord = node_coords.get(to_node)
    if not from_coord or not to_coord:
        return None

    from_lon, from_lat = from_coord
    to_lon, to_lat = to_coord

    try:
        resp = requests.get(
            f"{GRAPHHOPPER_API_URL}/route",
            params={
                "point": [f"{from_lat},{from_lon}", f"{to_lat},{to_lon}"],
                "profile": profile,
                "type": "json",
                "points_encoded": "false",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            paths = data.get("paths", [])
            if paths:
                if tally is not None:
                    tally[ROUTE_OK] = tally.get(ROUTE_OK, 0) + 1
                points = paths[0].get("points", {})
                return points.get("coordinates", [])
            if tally is not None:
                tally[ROUTE_NO_PATH] = tally.get(ROUTE_NO_PATH, 0) + 1
            return None
        reason = _classify(resp.status_code, resp.text)
        if tally is not None:
            tally[reason] = tally.get(reason, 0) + 1
        if detail is not None and reason in ROUTE_SERVER_FAULTS:
            detail.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if reason is ROUTE_OTHER or reason == ROUTE_OTHER:
            log.debug("Route %d→%d HTTP %s: %s", from_node, to_node,
                      resp.status_code, resp.text[:200])
    except Exception as e:
        if tally is not None:
            tally[ROUTE_TRANSPORT] = tally.get(ROUTE_TRANSPORT, 0) + 1
        if detail is not None:
            detail.append(f"{type(e).__name__}: {e}")
        log.debug("Route failed %d→%d: %s", from_node, to_node, e)

    return None


def _route_pair_with_time(
    from_node: int,
    to_node: int,
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
) -> tuple[list[list[float]] | None, float]:
    """Route and return (coordinates, time_ms)."""
    if not HAS_REQUESTS:
        return None, 0.0

    from_coord = node_coords.get(from_node)
    to_coord = node_coords.get(to_node)
    if not from_coord or not to_coord:
        return None, 0.0

    from_lon, from_lat = from_coord
    to_lon, to_lat = to_coord

    try:
        resp = requests.get(
            f"{GRAPHHOPPER_API_URL}/route",
            params={
                "point": [f"{from_lat},{from_lon}", f"{to_lat},{to_lon}"],
                "profile": profile,
                "type": "json",
                "points_encoded": "false",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            paths = data.get("paths", [])
            if paths:
                points = paths[0].get("points", {})
                time_ms = paths[0].get("time", 0)
                return points.get("coordinates", []), time_ms
    except Exception as e:
        log.debug("Route failed %d→%d: %s", from_node, to_node, e)

    return None, 0.0


# How often the long loops report liveness. Both loops are minutes-to-hours at
# the pipeline's own sampling sizes (zoom 5 alone routes ~114k pairs), and the
# stuck-task watchdog reaps a task after FW_STUCK_TIMEOUT_MS (30 min) with no
# heartbeat — measured 2026-09-22: zoom 3's vote accumulation alone exceeded it,
# the task was reclaimed, and a second runner started the same job from scratch
# while the first kept burning a core. Step logs do NOT count as progress.
HEARTBEAT_EVERY = 500
# ...and at least this often by the CLOCK. A count alone assumes pairs are
# cheap: on the US major-roads graph a z2 pair is a 300-4,700 km route,
# densified to 20 m before snapping, and 500 of them did not finish inside the
# 30 min the watchdog allows (2026-10-12).
HEARTBEAT_SECONDS = 60.0


class _Pulse:
    """When a loop should report liveness: every HEARTBEAT_EVERY items or
    HEARTBEAT_SECONDS, whichever comes first."""

    def __init__(self) -> None:
        self._last = time.monotonic()

    def due(self, n: int) -> bool:
        now = time.monotonic()
        if n % HEARTBEAT_EVERY == 0 or now - self._last >= HEARTBEAT_SECONDS:
            self._last = now
            return True
        return False


def route_batch_parallel(
    pairs: list[tuple[int, int]],
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
    max_concurrent: int = 16,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> dict[tuple[int, int], list[list[float]]]:
    """Route all OD pairs in parallel using ThreadPoolExecutor.

    ``heartbeat(message)`` is called every HEARTBEAT_EVERY completed pairs and
    ``check_cancel()`` between them; a cancellation raised there abandons the
    pending routes (the pool is shut down with its queue cancelled).
    """
    if not HAS_REQUESTS:
        log.warning("requests not available, skipping routing")
        return {}

    results: dict[tuple[int, int], list[list[float]]] = {}
    total = len(pairs)

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = {
            pool.submit(_route_pair, a, b, node_coords, graph_dir, profile): (a, b)
            for a, b in pairs
        }
        done = 0
        pulse = _Pulse()
        try:
            for future in as_completed(futures):
                pair = futures[future]
                done += 1
                if done % 1000 == 0:
                    log.info("Routed %d / %d pairs", done, total)
                if pulse.due(done):
                    if check_cancel is not None:
                        check_cancel()
                    if heartbeat is not None:
                        heartbeat(f"routed {done:,}/{total:,} pairs")
                try:
                    coords = future.result()
                    if coords:
                        results[pair] = coords
                except Exception:
                    pass
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    log.info("Completed routing: %d / %d pairs succeeded", len(results), total)
    if heartbeat is not None:
        heartbeat(f"routed {len(results):,}/{total:,} pairs")
    return results


def _server_covers(node_coords: dict[int, tuple[float, float]]) -> float | None:
    """Fraction of ``node_coords`` inside the routing server's bbox (/info), or
    None if the server cannot be asked. A server holding another region covers
    ~0% of these nodes; one holding this region covers ~100%."""
    try:
        r = requests.get(f"{GRAPHHOPPER_API_URL}/info", timeout=10)
        bbox = r.json().get("bbox") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 - unanswerable is "unknown", not "covered"
        return None
    if not bbox or len(bbox) != 4 or not node_coords:
        return None
    minlon, minlat, maxlon, maxlat = bbox
    pts = list(node_coords.values())
    step = max(1, len(pts) // 2000)
    sample = pts[::step]
    inside = sum(1 for lon, lat in sample if minlon <= lon <= maxlon and minlat <= lat <= maxlat)
    return inside / len(sample)


def probe_router(
    node_coords: dict[int, tuple[float, float]],
    pairs: list[tuple[int, int]],
    profile: str,
    attempts: int = 8,
) -> None:
    """Refuse to start the SBS loop unless the router can serve THIS region.

    graphhopper-web serves exactly ONE region: ``_route_pair`` sends only
    lon/lat + profile, so a server holding another region answers **HTTP 400
    "Point 0 is out of bounds"** for every pair. That path returns ``None``
    like any other miss, so the whole loop completes, betweenness is empty, and
    the step reports success — measured 2026-09-22 on Washington with no server
    at all ("0 / 114163 pairs succeeded") and again 2026-09-24 against the
    Washington server with Oregon and Idaho coordinates. A hollow map is worse
    than a failed run, because nothing downstream can tell.

    ⚠️ **A pair with no route is not a wrong region.** GraphHopper answers HTTP
    400 for both, and the first version of this probe conflated them: Hawaii
    failed on 2026-09-25 because zoom 2 samples pairs at least 300 km apart,
    which is inter-island, and no car route exists between islands. But
    ``ConnectionNotFound`` is positive evidence — to report it the server had to
    snap BOTH points into the loaded graph — so it PASSES the probe. Only
    out-of-bounds, a dead server, or an unrecognised response indict the server.

    ``PermanentError``: a graph that does not cover these coordinates will not
    start covering them on retry.
    """
    if not HAS_REQUESTS:
        raise PermanentError(
            "low-zoom routing needs the `requests` package; this runner has none, "
            "so every route would silently return no path"
        )
    if not pairs:
        return

    tally: dict[str, int] = {}
    detail: list[str] = []
    probed = 0
    for from_node, to_node in pairs[:attempts]:
        if from_node not in node_coords or to_node not in node_coords:
            continue
        probed += 1
        _route_pair(from_node, to_node, node_coords, graph_dir="", profile=profile,
                    tally=tally, detail=detail)
        if tally.get(ROUTE_OK) or tally.get(ROUTE_NO_PATH):
            log.info(
                "Router probe OK: %s serves this region for profile %r (%d routed, "
                "%d with no path)",
                GRAPHHOPPER_API_URL, profile,
                tally.get(ROUTE_OK, 0), tally.get(ROUTE_NO_PATH, 0),
            )
            return

    if not probed:
        return  # nothing had usable coordinates; nothing to prove

    # Every probe pair refused is weak evidence when there were only one or two:
    # on an island region the sampled points can all lie off the car network.
    # Measured 2026-10-03: Tonga, Vanuatu and Kiribati failed here on 1-2 pairs.
    # Ask the server what it holds: if its bounds cover most of THIS region's
    # nodes, it is serving this region, whatever those pairs said.
    covered = _server_covers(node_coords)
    if covered is not None and covered >= 0.8:
        log.warning(
            "Router probe: %d probe pair(s) refused (%s), but the server's bounds cover "
            "%.0f%% of this region's nodes -- it is serving this region; the refused "
            "points are off the routable network",
            probed, ", ".join(f"{k}={v}" for k, v in sorted(tally.items())), covered * 100)
        return

    faults = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    raise PermanentError(
        f"the routing server at {GRAPHHOPPER_API_URL} cannot serve this region "
        f"(profile {profile!r}): {probed} probe pairs, outcomes {faults or 'none'}; "
        f"last response -> {detail[-1] if detail else 'nothing'}. "
        "Every one was refused for a reason that indicts the SERVER, not the pair "
        "(out of bounds, unreachable, or an unrecognised response) — graphhopper-web "
        "serves ONE region+profile, so a server holding a different state rejects "
        "every pair and the run would produce a map with no betweenness at all. "
        "Point the graphhopper role at this region "
        "(`fw fleet set --graphhopper-region <region>`) and wait for it to be healthy."
    )


def route_and_accumulate(
    pairs: list[tuple[int, int]],
    node_coords: dict[int, tuple[float, float]],
    graph_dir: str,
    profile: str,
    segment_index: SegmentIndex,
    max_concurrent: int = 16,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    submit_window: int = 4096,
    snapper: "SnapPool | None" = None,
) -> tuple[dict[int, int], int]:
    """Route OD pairs and snap each route to logical edges AS IT COMPLETES.

    Streaming counterpart of ``route_batch_parallel`` + ``accumulate_votes``,
    which retain every route's full coordinate list for a zoom before snapping
    any. With only a MINIMUM pair distance, a state's zoom-5 sample (~100k
    pairs) is dominated by cross-state routes of ~2,000 points each, and that
    retained set is what killed the runner: measured 2026-09-22 on Washington,
    the kernel OOM-killed the process at 25.5 GB resident after 59,000 routes.
    Here nothing is kept past the snap, and at most ``submit_window`` futures
    are outstanding, so peak memory is the graph plus what is in flight.

    With ``snapper`` the snap runs in its worker processes: a completed route
    is handed to the pool and its votes are added when the worker returns, with
    at most ``4 * snapper.workers`` routes waiting so memory stays bounded.

    Returns ``(votes, routed_count)``.
    """
    bc: dict[int, int] = defaultdict(int)
    if not HAS_REQUESTS:
        log.warning("requests not available, skipping routing")
        return dict(bc), 0
    tally: dict[str, int] = {}

    total = len(pairs)
    routed = 0
    done = 0
    pair_of: dict = {}
    witness: list[tuple[int, int]] = []  # one pair that routed: proof of the region
    pulse = _Pulse()
    snapping: set = set()
    snapped = 0
    stats = [0, 0, 0]  # memo hits, misses, sticky -- from the workers

    def _add_snap(sfut) -> None:
        nonlocal snapped
        eids, h, m, st = sfut.result()  # a worker's exception is a code error: raise
        for eid in eids:
            bc[eid] += 1
        stats[0] += h
        stats[1] += m
        stats[2] += st
        snapped += 1

    def _drain(limit: int) -> None:
        """Collect finished snaps; block until at most ``limit`` are outstanding."""
        from concurrent.futures import FIRST_COMPLETED, wait

        for sfut in [f for f in snapping if f.done()]:
            snapping.discard(sfut)
            _add_snap(sfut)
        while len(snapping) > limit:
            finished, _ = wait(snapping, return_when=FIRST_COMPLETED)
            for sfut in finished:
                snapping.discard(sfut)
                _add_snap(sfut)

    def _consume(fut) -> None:
        nonlocal routed, done
        done += 1
        # NOT caught. _route_pair classifies every routing outcome itself (no
        # path, out of bounds, transport) into `tally` and returns None, so
        # anything that escapes it is a CODE error, not a pair without a route.
        # Swallowing it here counted every such failure as "no route" and left
        # `tally` empty -- which the server-fault guard below reads as "nothing
        # answered", so a run in which every call raised reported SUCCESS with
        # zero votes and published a hollow map (found 2026-09-30 when a stale
        # test mock raised TypeError on every call and the suite still saw a
        # clean return). The pool is shut down by the except below.
        coords = fut.result()
        pair = pair_of.pop(fut, None)
        if coords:
            routed += 1
            if not witness and pair is not None:
                witness.append(pair)
            if snapper is not None:
                snapping.add(snapper.submit(coords))
            else:
                for eid in segment_index.snap_route(coords):
                    bc[eid] += 1
        if snapper is not None:
            _drain(4 * snapper.workers)
        if done % 1000 == 0:
            if snapper is not None:
                log.info("Routed %d / %d pairs, snapped %d (memo hits %d, misses %d, "
                         "on-edge %d; %d workers)", done, total, snapped, stats[0], stats[1],
                         stats[2], snapper.workers)
            else:
                log.info("Routed+snapped %d / %d pairs (memo hits %d, misses %d)", done, total,
                         segment_index.snap_hits, segment_index.snap_misses)
        if pulse.due(done):
            if check_cancel is not None:
                check_cancel()
            if heartbeat is not None:
                heartbeat(f"routed {done:,}/{total:,} pairs")

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        pending: set = set()
        it = iter(pairs)
        try:
            for a, b in it:
                fut = pool.submit(_route_pair, a, b, node_coords, graph_dir, profile, tally)
                pair_of[fut] = (a, b)
                pending.add(fut)
                if len(pending) >= submit_window:
                    finished = next(as_completed(pending))
                    pending.discard(finished)
                    _consume(finished)
            for finished in as_completed(pending):
                _consume(finished)
            if snapper is not None:
                _drain(0)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            for sfut in snapping:
                sfut.cancel()
            raise

    log.info("Completed routing: %d / %d pairs succeeded", routed, total)
    log.info("Accumulated votes on %d edges from %d routes", len(bc), routed)
    # The probe proves the server CAN route this region; this catches it stopping
    # mid-loop (restarted, OOM-killed, repointed at another state by a concurrent
    # `fleet set`). Zero votes is not a sparse result, it is no result, and every
    # downstream score would be uniformly 0 while the step reported success.
    faults = sum(tally.get(r, 0) for r in ROUTE_SERVER_FAULTS)
    answered = sum(tally.values())
    if answered:
        log.info("Routing outcomes: %s", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    # Judge the SERVER, not the map. A pair with no route is data — Hawaii's
    # inter-island zoom-2 sample is almost entirely unroutable by car, and a
    # blanket "under 5% returned a path" floor failed that state on 2026-09-25.
    # Out-of-bounds and transport failures are the ones that mean this run cannot
    # produce betweenness, and a repointed or dead server drives them to ~100%.
    if answered and faults * 5 > answered and witness:
        # A high out-of-bounds share is NOT proof of a wrong server. Measured
        # 2026-10-03 on small regions (islands, the provinces of a country cut
        # into pieces): 3-9 of 7-13 pairs were out of bounds on EVERY host while
        # other pairs in the same run routed -- points with no car road near
        # them, which a small sample makes a large fraction. So ask the server
        # directly: re-route a pair that already succeeded in this run. If it
        # still routes, the region is loaded and the faults were data.
        recheck: dict[str, int] = {}
        a, b = witness[0]
        if _route_pair(a, b, node_coords, graph_dir, profile, recheck):
            log.warning(
                "%d of %d routing requests were refused (%s), but the server still "
                "routes a pair from this run, so it is serving this region: treating "
                "them as points outside the routable network",
                faults, answered, ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
            if heartbeat is not None:
                heartbeat(f"routed {routed:,}/{total:,} pairs")
            return dict(bc), routed
    if answered and faults * 5 > answered and not witness:
        covered = _server_covers(node_coords)
        if covered is not None and covered >= 0.8:
            log.warning(
                "%d of %d routing requests were refused and none routed, but the server's "
                "bounds cover %.0f%% of this region's nodes: it is serving this region",
                faults, answered, covered * 100)
            return dict(bc), routed
    if answered and faults * 5 > answered:
        raise PermanentError(
            f"the routing server at {GRAPHHOPPER_API_URL} stopped serving this region "
            f"mid-run: {faults:,} of {answered:,} requests failed in a way that indicts "
            f"the server ({', '.join(f'{k}={v}' for k, v in sorted(tally.items()))}). "
            "Betweenness computed from this would be meaningless, so the run stops "
            "here rather than publishing a hollow map."
        )
    if heartbeat is not None:
        heartbeat(f"routed {routed:,}/{total:,} pairs")
    return dict(bc), routed


def accumulate_votes(
    routes: dict[tuple[int, int], list[list[float]]],
    segment_index: SegmentIndex,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> dict[int, int]:
    """Accumulate betweenness centrality votes from routed paths.

    For each route, snap to logical edges and increment vote count. Heartbeats
    every HEARTBEAT_EVERY routes (see route_batch_parallel for why).
    """
    bc: dict[int, int] = defaultdict(int)
    total = len(routes)

    pulse = _Pulse()
    for n, ((_a, _b), coords) in enumerate(routes.items(), 1):
        matched = segment_index.snap_route(coords)
        for eid in matched:
            bc[eid] += 1
        if pulse.due(n):
            if check_cancel is not None:
                check_cancel()
            if heartbeat is not None:
                heartbeat(f"snapped {n:,}/{total:,} routes")
            if n % 2000 == 0:
                log.info("Snapped %d / %d routes (memo hits %d, misses %d)", n, total,
                         segment_index.snap_hits, segment_index.snap_misses)

    log.info("Accumulated votes on %d edges from %d routes", len(bc), len(routes))
    return dict(bc)


def normalize_sbs(bc: dict[int, int]) -> dict[int, float]:
    """Normalize betweenness counts to SB_z scores (spec §5.4).

    SB_z(e) = log(1 + BC_z[e]) / log(1 + P95_z)
    """
    if not bc:
        return {}

    values = sorted(bc.values())
    p95_idx = int(len(values) * 0.95)
    p95 = values[min(p95_idx, len(values) - 1)]

    if p95 == 0:
        return dict.fromkeys(bc, 0.0)

    denom = math.log(1 + p95)
    result: dict[int, float] = {}
    for eid, count in bc.items():
        result[eid] = min(1.0, math.log(1 + count) / denom)

    return result


def compute_sbs_for_zoom(
    graph: RoadGraph,
    cities_path: str,
    graph_dir: str,
    profile: str,
    zoom_level: int,
    k_pairs: int | None = None,
    max_concurrent: int = 16,
) -> tuple[dict[int, float], int, int]:
    """Full SBS pipeline for one zoom level.

    Returns:
        (sbs_scores, anchor_count, route_count)
    """
    anchors = build_anchors(graph, cities_path, zoom_level)
    pairs = sample_od_pairs(anchors, zoom_level, graph, k_pairs=k_pairs)

    segment_index = SegmentIndex(graph)
    routes = route_batch_parallel(
        pairs,
        graph.node_coords,
        graph_dir,
        profile,
        max_concurrent,
    )

    bc = accumulate_votes(routes, segment_index)
    sbs = normalize_sbs(bc)

    return sbs, len(anchors), len(routes)


def save_sbs(sbs: dict[int, float], path: str) -> None:
    """Save SBS scores to JSON file."""
    ensure_dir(path)
    with open_output(path, "w") as f:
        json.dump({str(k): v for k, v in sbs.items()}, f)


def load_sbs(path: str) -> dict[int, float]:
    """Load SBS scores from JSON file."""
    data = read_storage_json(path)
    return {int(k): v for k, v in data.items()}


def save_anchors(anchors: list[int], path: str) -> None:
    """Save anchor node list to JSON file."""
    ensure_dir(path)
    with open_output(path, "w") as f:
        json.dump(anchors, f)


def load_anchors(path: str) -> list[int]:
    """Load anchor node list from JSON file."""
    return read_storage_json(path)
