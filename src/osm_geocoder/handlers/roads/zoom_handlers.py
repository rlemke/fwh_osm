"""Zoom builder event facet handlers.

Handles zoom-level road infrastructure events defined in osmzoombuilder.afl
under osm.Roads.ZoomBuilder.
"""

import json
import logging
import os
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from facetwork.config import get_output_base
from facetwork.runtime.handler_context import HandlerCancelled, HandlerContext

from ..shared._output import ensure_dir, open_output, read_storage_json


def _is_remote(path: str) -> bool:
    return path.startswith(("s3://", "hdfs://"))


def _publish_dir(local_dir: str, remote_dir: str) -> int:
    """Upload every file under ``local_dir`` to ``remote_dir``; return the count."""
    from facetwork.runtime.storage import get_storage_backend

    be = get_storage_backend(remote_dir)
    n = 0
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, local_dir).replace(os.sep, "/")
            # Streamed: a country build's graph file alone is 1.5 GB.
            with open(src, "rb") as fh, be.open(remote_dir.rstrip("/") + "/" + rel, "wb") as out:
                shutil.copyfileobj(fh, out, 16 * 1024 * 1024)
            n += 1
    return n


def _resolve_layers_dir(output_dir: str) -> str:
    """A RELATIVE output_dir names a place in the durable output store.

    ``lz-country/us-z2-z4`` -> ``s3://.../osm-output/lz-country/us-z2-z4`` on a
    fleet (remote storage), or a local directory under the output base on a
    laptop. Absolute paths and URIs are used as given, so a caller can still
    pin a host-local directory -- at the cost that only that host can read it.
    """
    if not output_dir or output_dir.startswith("/") or "://" in output_dir:
        return output_dir
    from ..shared._output import resolve_output_dir

    return resolve_output_dir(output_dir.strip("/"))


def _local_work_dir(remote_dir: str) -> str:
    """The local directory a remote output_dir is built in.

    Deterministic, not a fresh temp dir: the builder resumes routed zooms from
    checkpoints in its output directory (sbs_z*.json), and a reclaimed task
    landing back on this host must find them. Measured 2026-10-13: a retry
    reloaded z2-z4 from checkpoints and skipped ~9 h of routing.
    """
    import tempfile

    base = os.environ.get("FW_LOCAL_SCRATCH") or tempfile.gettempdir()
    rel = remote_dir.split("://", 1)[1].strip("/")
    path = os.path.join(base, "zoom-layers", rel)
    os.makedirs(path, exist_ok=True)
    return path
from ..shared.output_cache import cached_result, save_result_meta, with_output_cache
from .zoom_builder import (
    _empty_result,
    build_zoom_layers,
    plan_zoom_routing,
    recipe_fingerprint,
    detect_zoom_chunk,
    route_zoom_chunk,
)
from .zoom_detection import (
    detect_bypasses,
    detect_rings,
    load_bypass_flags,
    load_ring_flags,
    save_bypass_flags,
    save_ring_flags,
)
from .zoom_graph import (
    HAS_OSMIUM,
    RoadGraph,
    build_logical_graph,
)
from .zoom_sbs import (
    build_anchors,
    compute_sbs_for_zoom,
    load_sbs,
    save_anchors,
    save_sbs,
)
from .zoom_selection import (
    build_cell_budgets,
    enforce_monotonic_reveal,
)
from .zoom_selection import (
    compute_scores as _compute_scores,
)
from .zoom_selection import (
    select_edges as _select_edges,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Roads.ZoomBuilder"


def _make_build_logical_graph_handler(facet_name: str):
    """Create handler for BuildLogicalGraph event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"
    cache_params = {"kind": "logical_graph"}

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        pbf_path = cache.get("path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: building logical graph from {pbf_path}")
        log.info("%s building logical graph from %s", facet_name, pbf_path)

        if not HAS_OSMIUM or not pbf_path:
            return {"edge_count": 0, "node_count": 0, "graph_path": ""}

        try:
            out_dir = Path(pbf_path).parent / "zoom-builder"
            out_dir.mkdir(parents=True, exist_ok=True)
            graph_path = str(out_dir / "logical_graph.json")

            graph = build_logical_graph(pbf_path, output_path=graph_path)
            if step_log:
                step_log(
                    f"{facet_name}: built graph with {len(graph.edges)} edges, {len(graph.node_coords)} nodes",
                    level="success",
                )
            return {
                "edge_count": len(graph.edges),
                "node_count": len(graph.node_coords),
                "graph_path": graph_path,
            }
        except Exception as exc:
            log.error("Failed to build logical graph: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to build logical graph: {exc}", level="error")
            raise

    return with_output_cache(handler, qualified, cache_params)


def _make_build_anchors_handler(facet_name: str):
    """Create handler for BuildAnchors event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        cities_path = payload.get("cities_path", "")
        zoom_level = payload.get("zoom_level", 4)
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"zoom_level": zoom_level}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: building anchors for zoom {zoom_level}")
        log.info("%s building anchors for zoom %d", facet_name, zoom_level)

        if not graph_path:
            return {"anchors_path": "", "anchor_count": 0}

        try:
            graph = RoadGraph.load(graph_path)
            anchors = build_anchors(graph, cities_path, zoom_level)

            out_dir = Path(graph_path).parent
            anchors_path = str(out_dir / f"anchors_z{zoom_level}.json")
            save_anchors(anchors, anchors_path)

            if step_log:
                step_log(
                    f"{facet_name}: built {len(anchors)} anchors for zoom {zoom_level}",
                    level="success",
                )
            rv = {"anchors_path": anchors_path, "anchor_count": len(anchors)}
            save_result_meta(qualified, cache, {"zoom_level": zoom_level}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to build anchors: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to build anchors: {exc}", level="error")
            raise

    return handler


def _make_compute_sbs_handler(facet_name: str):
    """Create handler for ComputeSBS event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        _anchors_path = payload.get("anchors_path", "")
        gh_config = payload.get("graph", {})
        zoom_level = payload.get("zoom_level", 4)
        k_pairs = payload.get("k_pairs", 5000)

        graph_dir = gh_config.get("graphDir", "")
        profile = gh_config.get("profile", "car")
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(
            qualified, cache, {"zoom_level": zoom_level, "k_pairs": k_pairs}, step_log
        )
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: computing SBS for zoom {zoom_level}")
        log.info("%s computing SBS for zoom %d", facet_name, zoom_level)

        if not graph_path:
            return {"sbs_path": "", "route_count": 0}

        try:
            graph = RoadGraph.load(graph_path)
            cities_path = str(Path(graph_path).parent / "cities.geojson")

            sbs, anchor_count, route_count = compute_sbs_for_zoom(
                graph,
                cities_path,
                graph_dir,
                profile,
                zoom_level,
                k_pairs=k_pairs,
            )

            out_dir = Path(graph_path).parent
            sbs_path = str(out_dir / f"sbs_z{zoom_level}.json")
            save_sbs(sbs, sbs_path)

            if step_log:
                step_log(
                    f"{facet_name}: {route_count} routes computed for zoom {zoom_level}",
                    level="success",
                )
            rv = {"sbs_path": sbs_path, "route_count": route_count}
            save_result_meta(qualified, cache, {"zoom_level": zoom_level, "k_pairs": k_pairs}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to compute SBS: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute SBS: {exc}", level="error")
            raise

    return handler


def _make_compute_scores_handler(facet_name: str):
    """Create handler for ComputeScores event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        sbs_paths_str = payload.get("sbs_paths", "")
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"sbs_paths": sbs_paths_str}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: computing scores")
        log.info("%s computing scores", facet_name)

        if not graph_path:
            return {"scores_path": ""}

        try:
            graph = RoadGraph.load(graph_path)

            # Load SBS from comma-separated paths
            sbs_by_zoom: dict[int, dict[int, float]] = {}
            for path_entry in sbs_paths_str.split(","):
                path_entry = path_entry.strip()
                if not path_entry:
                    continue
                # Extract zoom from filename (e.g., sbs_z4.json)
                fname = Path(path_entry).stem
                for z in range(2, 8):
                    if f"z{z}" in fname:
                        sbs_by_zoom[z] = load_sbs(path_entry)
                        break

            scores = _compute_scores(graph, sbs_by_zoom)

            out_dir = Path(graph_path).parent
            scores_path = str(out_dir / "scores.json")
            ensure_dir(scores_path)
            with open_output(scores_path, "w") as f:
                serializable = {
                    str(z): {str(eid): s for eid, s in z_scores.items()}
                    for z, z_scores in scores.items()
                }
                json.dump(serializable, f)

            if step_log:
                step_log(
                    f"{facet_name}: computed scores for {len(scores)} zoom levels", level="success"
                )
            rv = {"scores_path": scores_path}
            save_result_meta(qualified, cache, {"sbs_paths": sbs_paths_str}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to compute scores: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute scores: {exc}", level="error")
            raise

    return handler


def _make_detect_bypasses_handler(facet_name: str):
    """Create handler for DetectBypasses event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        cities_path = payload.get("cities_path", "")
        gh_config = payload.get("graph", {})

        graph_dir = gh_config.get("graphDir", "")
        profile = gh_config.get("profile", "car")
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"kind": "bypasses"}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: detecting bypasses")
        log.info("%s detecting bypasses", facet_name)

        if not graph_path:
            return {"bypasses_path": "", "bypass_count": 0}

        try:
            graph = RoadGraph.load(graph_path)
            flags = detect_bypasses(graph, cities_path, graph_dir, profile)

            out_dir = Path(graph_path).parent
            bypasses_path = str(out_dir / "bypass_flags.json")
            save_bypass_flags(flags, bypasses_path)

            bypass_count = sum(1 for v in flags.values() if v == "bypass")
            if step_log:
                step_log(f"{facet_name}: detected {bypass_count} bypasses", level="success")
            rv = {"bypasses_path": bypasses_path, "bypass_count": bypass_count}
            save_result_meta(qualified, cache, {"kind": "bypasses"}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to detect bypasses: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to detect bypasses: {exc}", level="error")
            raise

    return handler


def _make_detect_rings_handler(facet_name: str):
    """Create handler for DetectRings event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        cities_path = payload.get("cities_path", "")
        gh_config = payload.get("graph", {})

        graph_dir = gh_config.get("graphDir", "")
        profile = gh_config.get("profile", "car")
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"kind": "rings"}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: detecting rings")
        log.info("%s detecting rings", facet_name)

        if not graph_path:
            return {"rings_path": "", "ring_count": 0}

        try:
            graph = RoadGraph.load(graph_path)
            flags = detect_rings(graph, cities_path, graph_dir, profile)

            out_dir = Path(graph_path).parent
            rings_path = str(out_dir / "ring_flags.json")
            save_ring_flags(flags, rings_path)

            if step_log:
                step_log(f"{facet_name}: detected {len(flags)} rings", level="success")
            rv = {"rings_path": rings_path, "ring_count": len(flags)}
            save_result_meta(qualified, cache, {"kind": "rings"}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to detect rings: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to detect rings: {exc}", level="error")
            raise

    return handler


def _make_select_edges_handler(facet_name: str):
    """Create handler for SelectEdges event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        graph_path = payload.get("graph_path", "")
        scores_path = payload.get("scores_path", "")
        cities_path = payload.get("cities_path", "")
        bypasses_path = payload.get("bypasses_path", "")
        rings_path = payload.get("rings_path", "")
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"scores_path": scores_path}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: selecting edges")
        log.info("%s selecting edges", facet_name)

        if not graph_path or not scores_path:
            return {"assignments_path": "", "selected_count": 0}

        try:
            graph = RoadGraph.load(graph_path)

            # Load scores
            raw = read_storage_json(scores_path)
            scores = {
                int(z): {int(eid): s for eid, s in z_scores.items()} for z, z_scores in raw.items()
            }

            bypass_flags = load_bypass_flags(bypasses_path)
            ring_flags = load_ring_flags(rings_path)

            # Build minimal anchors from cities
            anchors_by_zoom: dict[int, list[int]] = {}
            for z in range(2, 8):
                anchors_by_zoom[z] = build_anchors(graph, cities_path, z)

            budgets = build_cell_budgets(graph, anchors_by_zoom)
            selected_by_zoom = _select_edges(
                graph,
                scores,
                budgets,
                anchors_by_zoom,
                bypass_flags,
                ring_flags,
            )
            assignments = enforce_monotonic_reveal(selected_by_zoom)

            out_dir = Path(graph_path).parent
            assignments_path = str(out_dir / "assignments.json")
            ensure_dir(assignments_path)
            with open_output(assignments_path, "w") as f:
                json.dump({str(k): v for k, v in assignments.items()}, f)

            if step_log:
                step_log(f"{facet_name}: selected {len(assignments)} edges", level="success")
            rv = {
                "assignments_path": assignments_path,
                "selected_count": len(assignments),
            }
            save_result_meta(qualified, cache, {"scores_path": scores_path}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to select edges: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to select edges: {exc}", level="error")
            raise

    return handler


def _make_export_zoom_layers_handler(facet_name: str):
    """Create handler for ExportZoomLayers event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        assignments_path = payload.get("assignments_path", "")
        graph_path = payload.get("graph_path", "")
        output_dir = payload.get(
            "output_dir",
            os.path.join(get_output_base(), "osm", "zoom-export"),
        )
        step_log = payload.get("_step_log")
        cache = {"path": graph_path, "size": payload.get("graph_size", 0)}

        hit = cached_result(qualified, cache, {"assignments_path": assignments_path}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: exporting zoom layers to {output_dir}")
        log.info("%s exporting zoom layers to %s", facet_name, output_dir)

        if not assignments_path or not graph_path:
            return {"result": _empty_result(output_dir)}

        try:
            from .zoom_builder import _export_zoom_geojson

            graph = RoadGraph.load(graph_path)
            raw = read_storage_json(assignments_path)
            assignments = {int(k): v for k, v in raw.items()}

            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)

            for z in range(2, 8):
                geojson_path = str(out / f"roads_z{z}.geojson")
                _export_zoom_geojson(graph, assignments, z, geojson_path)

            if step_log:
                step_log(
                    f"{facet_name}: exported {len(assignments)} edges across 6 zoom levels to {output_dir}",
                    level="success",
                )
            rv = {
                "result": {
                    "output_dir": output_dir,
                    "total_logical_edges": len(graph.edges),
                    "selected_edges": len(assignments),
                    "zoom_distribution": "",
                    "city_count": 0,
                    "pair_count": 0,
                    "route_count": 0,
                    "csv_path": "",
                    "metrics_path": "",
                    "format": "GeoJSON",
                    "extraction_date": datetime.now(UTC).isoformat(),
                }
            }
            save_result_meta(qualified, cache, {"assignments_path": assignments_path}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to export zoom layers: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to export zoom layers: {exc}", level="error")
            raise

    return handler


def _rss_gb() -> float | None:
    """This process's resident set, GB (Linux /proc; None elsewhere)."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        return None
    return None


class _PeakRss:
    """Sample process RSS during a build; record the peak.

    The zoom build's memory floor (``memory_gb``) was fitted to two measured
    points and nothing recorded the peak of any other run, so a floor that was
    too low showed up only as an OOM kill with no error text (Minas Gerais,
    five times). This makes every build report its peak, and logs each new
    2 GB milestone from 6 GB up so an OOM-killed run still leaves its last
    reading in the step log. ⚠️ Process-wide: other tasks running in the same
    runner process are included, which is also what the OOM killer counts.
    """

    def __init__(self, step_log=None, interval_s: float = 2.0) -> None:
        self.start = _rss_gb()
        self.peak = self.start or 0.0
        self._step_log = step_log
        self._interval = interval_s
        self._next_mark = 6.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="zoom-peak-rss")

    def __enter__(self) -> "_PeakRss":
        if self.start is not None:
            self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._sample()

    def _sample(self) -> None:
        cur = _rss_gb()
        if cur is None:
            return
        self.peak = max(self.peak, cur)
        if self._step_log and self.peak >= self._next_mark:
            try:
                self._step_log(f"BuildZoomLayers: memory reached {self.peak:.1f} GB RSS")
            except Exception:  # noqa: BLE001 - a log line must not fail the build
                pass
            while self._next_mark <= self.peak:
                self._next_mark += 2.0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()


def _make_build_zoom_layers_handler(facet_name: str):
    """Create handler for BuildZoomLayers event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        gh_config = payload.get("graph", {})
        min_population = payload.get("min_population", 50000)
        output_dir = _resolve_layers_dir(payload.get(
            "output_dir",
            os.path.join(get_output_base(), "osm", "zoom-builder"),
        ))
        max_concurrent = payload.get("max_concurrent", 16)
        route_max_zoom = int(payload.get("route_max_zoom", 7) or 7)
        if not 2 <= route_max_zoom <= 7:
            raise ValueError(f"route_max_zoom must be 2..7, got {route_max_zoom}")
        max_pairs = int(payload.get("max_pairs", 0) or 0)
        # Votes written by RouteZoomChunk tasks; zooms they fully cover are merged
        # instead of routed. Not part of the cache key: same layers either way.
        chunk_vote_paths = [p for p in (payload.get("chunk_votes") or []) if p]
        # Likewise bypass / ring flags written by DetectZoomChunk tasks.
        detect_paths = [p for p in (payload.get("detect_results") or []) if p]
        step_log = payload.get("_step_log")

        # Everything that changes the OUTPUT must be in the key, not just the
        # one param that happens to come from the payload: the destination
        # (a cached result carries its own output_dir and paths), the routing
        # graph the betweenness was sampled against, and a fingerprint of the
        # algorithm's tunables. See build_zoom_layers.recipe_fingerprint.
        cache_params = {
            "min_population": min_population,
            "output_dir": output_dir,
            "graph_dir": (gh_config or {}).get("graphDir", ""),
            "profile": (gh_config or {}).get("profile", "car"),
            "recipe": recipe_fingerprint(),
        }
        # Only when it differs from the default, so every existing cached
        # result (all routed to z7) keeps its key.
        if route_max_zoom != 7:
            cache_params["route_max_zoom"] = route_max_zoom
        if max_pairs:
            cache_params["max_pairs"] = max_pairs
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: running full pipeline to {output_dir}")
        log.info("%s running full pipeline to %s", facet_name, output_dir)

        # Liveness + cooperative cancellation: a state-sized region routes and
        # snaps ~280k pairs and outlives the stuck-task watchdog otherwise
        # (step logs alone do not count as progress).
        ctx = HandlerContext.from_payload(payload)

        # A remote output_dir (s3://, hdfs://) is what lets the NEXT steps run on
        # any host. Measured 2026-10-02 on the fleet: the layers were written to
        # one host's local scratch, the six tile steps were claimed by three
        # different hosts, and each failed on a missing file until a retry
        # happened to land back on the host that had it. So: build in local
        # scratch (the pipeline writes with local file APIs), upload, and hand
        # back the remote paths, which BuildVectorTiles localizes anywhere.
        work_dir = _local_work_dir(output_dir) if _is_remote(output_dir) else output_dir
        rss = _PeakRss(step_log)
        try:
            with rss:
                result, metrics = build_zoom_layers(
                    cache=cache,
                    graph_config=gh_config,
                    min_population=min_population,
                    output_dir=work_dir,
                    max_concurrent=max_concurrent,
                    heartbeat=lambda msg: ctx.heartbeat(progress_message=msg),
                    check_cancel=ctx.raise_if_cancelled,
                    route_max_zoom=route_max_zoom,
                    max_pairs=max_pairs,
                    chunk_votes=[read_storage_json(p) for p in chunk_vote_paths] or None,
                    detect_results=[read_storage_json(p) for p in detect_paths] or None,
                )
            if work_dir != output_dir:
                ctx.heartbeat(progress_message=f"uploading layers to {output_dir}")
                n = _publish_dir(work_dir, output_dir)
                result = {k: (v.replace(work_dir, output_dir.rstrip("/"), 1)
                              if isinstance(v, str) and v.startswith(work_dir) else v)
                          for k, v in result.items()}
                if step_log:
                    step_log(f"{facet_name}: uploaded {n} file(s) to {output_dir}")
                shutil.rmtree(work_dir, ignore_errors=True)
            if step_log:
                # A run with no cities is WEAKER, not failed: anchors fall back
                # to high-degree graph nodes and bypass/ring detection returns
                # nothing. Say so where the run is read, not only in the log.
                if not metrics.get("city_count"):
                    step_log(
                        f"{facet_name}: no cities extracted from the PBF — anchors are "
                        "high-degree graph nodes and bypass/ring flags will be empty",
                        level="warning",
                    )
                step_log(
                    f"{facet_name}: built zoom layers ({result.get('selected_edges', 0)} edges "
                    f"selected, {metrics.get('city_count', 0)} cities)",
                    level="success",
                )
                if rss.start is not None:
                    step_log(
                        f"{facet_name}: peak memory {rss.peak:.1f} GB RSS "
                        f"(started at {rss.start:.1f} GB; process-wide)"
                    )
            if rss.start is not None:
                metrics = {**metrics, "peak_rss_gb": round(rss.peak, 2),
                           "start_rss_gb": round(rss.start, 2)}
            rv = {"result": result, "metrics": metrics}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except HandlerCancelled:
            # A clean stop (run terminated / task reclaimed / watchdog): not a
            # failure, no retry — let the runner record it as cancelled.
            if step_log:
                step_log(f"{facet_name}: cancelled ({ctx.cancellation_reason})", level="warning")
            raise
        except Exception as exc:
            log.error("Failed to build zoom layers: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to build zoom layers: {exc}", level="error")
            raise

    return handler


def _routing_paths(chunk_path: str) -> tuple[str, str, str]:
    """(routing dir, index dir, votes path) for a chunk at <routing>/chunks/<name>."""
    chunks_dir = chunk_path.rsplit("/", 1)[0]
    routing = chunks_dir.rsplit("/", 1)[0]
    name = chunk_path.rsplit("/", 1)[1]
    return routing, routing + "/index", routing + "/votes/" + name


def _local_index(index_dir: str, ctx: HandlerContext | None = None) -> str:
    """A local directory holding the snap index arrays at ``index_dir``.

    Remote: downloaded once per host per PLAN (the planner's ID file), so the
    chunks of one plan claimed by the same host share one copy, and a re-plan
    is never served the previous graph's arrays. Written to a temp name and
    renamed, so two chunks fetching at once cannot see a half-written copy."""
    if not _is_remote(index_dir):
        return index_dir
    import tempfile

    from facetwork.runtime.storage import get_storage_backend

    be = get_storage_backend(index_dir)
    with be.open(index_dir + "/ID", "r") as fh:
        plan_id = fh.read().strip()
    base = os.path.join(os.environ.get("FW_LOCAL_SCRATCH") or tempfile.gettempdir(),
                        "zoom-snap-index")
    final = os.path.join(base, plan_id)
    if os.path.exists(os.path.join(final, ".complete")):
        return final
    os.makedirs(base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=f".{plan_id}-", dir=base)
    for name in be.listdir(index_dir):
        name = name.rstrip("/").rsplit("/", 1)[-1]
        if ctx is not None:
            ctx.heartbeat(progress_message=f"fetching snap index {name}")
        with be.open(index_dir + "/" + name, "rb") as src, open(os.path.join(tmp, name), "wb") as dst:
            shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
    open(os.path.join(tmp, ".complete"), "w").close()
    try:
        os.rename(tmp, final)
    except OSError:  # another chunk on this host finished first
        shutil.rmtree(tmp, ignore_errors=True)
    # Older plans' copies: only the current one is ever read again.
    for old in os.listdir(base):
        if old != plan_id and not old.startswith("."):
            shutil.rmtree(os.path.join(base, old), ignore_errors=True)
    return final


def _make_plan_zoom_routing_handler(facet_name: str):
    """PlanZoomRouting: steps 0-2 and the routing chunks, written to the store."""

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        gh_config = payload.get("graph", {})
        output_dir = _resolve_layers_dir(payload.get("output_dir", ""))
        if not output_dir:
            raise ValueError("PlanZoomRouting needs output_dir")
        route_max_zoom = int(payload.get("route_max_zoom", 7) or 7)
        if not 2 <= route_max_zoom <= 7:
            raise ValueError(f"route_max_zoom must be 2..7, got {route_max_zoom}")
        step_log = payload.get("_step_log")
        ctx = HandlerContext.from_payload(payload)
        # Its own local dir: BuildZoomLayers publishes ITS work dir whole, and
        # the index (GBs at country scale) must not ride along a second time.
        work_dir = (_local_work_dir(output_dir) + "-plan") if _is_remote(output_dir) else output_dir
        os.makedirs(work_dir, exist_ok=True)
        plan = plan_zoom_routing(
            cache, gh_config, int(payload.get("min_population", 50000)), work_dir,
            route_max_zoom=route_max_zoom, max_pairs=int(payload.get("max_pairs", 0) or 0),
            chunk_pairs=int(payload.get("chunk_pairs", 500) or 500),
            heartbeat=lambda msg: ctx.heartbeat(progress_message=msg),
            check_cancel=ctx.raise_if_cancelled,
            detect_pairs=int(payload.get("detect_pairs", 1500) or 0),
        )
        routing_remote = output_dir.rstrip("/") + "/routing"
        if work_dir != output_dir:
            ctx.heartbeat(progress_message=f"uploading routing plan to {routing_remote}")
            n = _publish_dir(plan["routing_dir"], routing_remote)
            shutil.rmtree(work_dir, ignore_errors=True)
            if step_log:
                step_log(f"{facet_name}: uploaded {n} file(s) to {routing_remote}")
        chunks = [output_dir.rstrip("/") + "/" + c for c in plan["chunks"]]
        detect_chunks = [output_dir.rstrip("/") + "/" + c for c in plan["detect_chunks"]]
        if step_log:
            step_log(f"{facet_name}: {len(chunks)} routing chunks "
                     + ", ".join(f"z{z}: {v['pairs']} pairs/{v['chunks']}"
                                 for z, v in plan["zooms"].items()), level="success")
            d = plan.get("detect") or {}
            if d:
                step_log(f"{facet_name}: {len(detect_chunks)} detection chunks: "
                         f"{d['bypass']['pairs']:,} bypass pairs from {d['bypass']['items']:,} "
                         f"settlements, {d['ring']['pairs']:,} ring pairs from "
                         f"{d['ring']['items']:,} cities", level="success")
        return {"chunks": chunks, "routing_dir": routing_remote,
                "chunk_count": len(chunks), "zooms": json.dumps(plan["zooms"]),
                "detect_chunks": detect_chunks}

    return handler


def _make_route_zoom_chunk_handler(facet_name: str):
    """RouteZoomChunk: route + snap one chunk on whichever host claims it."""

    def handler(payload: dict) -> dict:
        chunk_path = payload["chunk"]
        step_log = payload.get("_step_log")
        ctx = HandlerContext.from_payload(payload)
        _routing, index_dir, votes_path = _routing_paths(chunk_path)
        chunk = read_storage_json(chunk_path)
        # Idempotent: a re-delivered task (at-least-once) finds its own result.
        try:
            prior = read_storage_json(votes_path)
            if prior.get("key") == chunk["key"]:
                if step_log:
                    step_log(f"{facet_name}: already routed ({prior.get('routed')} routes)")
                return {"votes_path": votes_path, "routed": int(prior.get("routed", 0))}
        except Exception:  # noqa: BLE001 - absent or unreadable: route it
            pass
        local = _local_index(index_dir, ctx)
        doc = route_zoom_chunk(
            chunk, local, int(payload.get("max_concurrent", 16) or 16),
            heartbeat=lambda msg: ctx.heartbeat(progress_message=msg),
            check_cancel=ctx.raise_if_cancelled,
        )
        ensure_dir(votes_path)
        with open_output(votes_path, "w") as fh:
            json.dump(doc, fh)
        if step_log:
            step_log(f"{facet_name}: z{doc['zoom']} chunk {doc['index'] + 1}/{doc['count']}: "
                     f"{doc['routed']}/{len(chunk['pairs'])} routed, votes on "
                     f"{len(doc['votes'])} edges", level="success")
        return {"votes_path": votes_path, "routed": doc["routed"]}

    return handler


def _make_detect_zoom_chunk_handler(facet_name: str):
    """DetectZoomChunk: bypass or ring detection for one chunk, on any host."""

    def handler(payload: dict) -> dict:
        chunk_path = payload["chunk"]
        step_log = payload.get("_step_log")
        ctx = HandlerContext.from_payload(payload)
        _routing, index_dir, result_path = _routing_paths(chunk_path)
        chunk = read_storage_json(chunk_path)
        # Idempotent: a re-delivered task (at-least-once) finds its own result.
        try:
            prior = read_storage_json(result_path)
            if prior.get("key") == chunk["key"] and prior.get("kind") == chunk["kind"]:
                if step_log:
                    step_log(f"{facet_name}: already detected ({len(prior['flags'])} flags)")
                return {"result_path": result_path, "flags": len(prior["flags"])}
        except Exception:  # noqa: BLE001 - absent or unreadable: detect it
            pass
        local = _local_index(index_dir, ctx)
        doc = detect_zoom_chunk(
            chunk, local,
            heartbeat=lambda msg: ctx.heartbeat(progress_message=msg),
            check_cancel=ctx.raise_if_cancelled,
        )
        ensure_dir(result_path)
        with open_output(result_path, "w") as fh:
            json.dump(doc, fh)
        if step_log:
            step_log(f"{facet_name}: {doc['kind']} chunk {doc['index'] + 1}/{doc['count']}: "
                     f"{doc['items']} {'settlements' if doc['kind'] == 'bypass' else 'cities'}, "
                     f"flags on {len(doc['flags'])} edges", level="success")
        return {"result_path": result_path, "flags": len(doc["flags"])}

    return handler


# Event facet definitions for handler registration
ZOOM_FACETS = [
    ("BuildLogicalGraph", _make_build_logical_graph_handler),
    ("BuildAnchors", _make_build_anchors_handler),
    ("ComputeSBS", _make_compute_sbs_handler),
    ("ComputeScores", _make_compute_scores_handler),
    ("DetectBypasses", _make_detect_bypasses_handler),
    ("DetectRings", _make_detect_rings_handler),
    ("SelectEdges", _make_select_edges_handler),
    ("ExportZoomLayers", _make_export_zoom_layers_handler),
    ("BuildZoomLayers", _make_build_zoom_layers_handler),
    ("PlanZoomRouting", _make_plan_zoom_routing_handler),
    ("RouteZoomChunk", _make_route_zoom_chunk_handler),
    ("DetectZoomChunk", _make_detect_zoom_chunk_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in ZOOM_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = handler_factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_zoom_handlers(poller) -> None:
    """Register all zoom builder event facet handlers with the poller."""
    if not HAS_OSMIUM:
        return
    for facet_name, handler_factory in ZOOM_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered zoom handler: %s", qualified_name)
