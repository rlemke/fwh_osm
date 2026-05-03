"""Zoom builder event facet handlers.

Handles zoom-level road infrastructure events defined in osmzoombuilder.afl
under osm.Roads.ZoomBuilder.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from facetwork.config import get_output_base

from ..shared.output_cache import cached_result, save_result_meta, with_output_cache
from .zoom_builder import (
    _empty_metrics,
    _empty_result,
    build_zoom_layers,
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
            with open(scores_path, "w", encoding="utf-8") as f:
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
            with open(scores_path, encoding="utf-8") as f:
                raw = json.load(f)
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
            with open(assignments_path, "w", encoding="utf-8") as f:
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
            with open(assignments_path, encoding="utf-8") as f:
                raw = json.load(f)
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


def _make_build_zoom_layers_handler(facet_name: str):
    """Create handler for BuildZoomLayers event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        gh_config = payload.get("graph", {})
        min_population = payload.get("min_population", 50000)
        output_dir = payload.get(
            "output_dir",
            os.path.join(get_output_base(), "osm", "zoom-builder"),
        )
        max_concurrent = payload.get("max_concurrent", 16)
        step_log = payload.get("_step_log")

        # Dynamic cache check (min_population comes from payload)
        hit = cached_result(qualified, cache, {"min_population": min_population}, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: running full pipeline to {output_dir}")
        log.info("%s running full pipeline to %s", facet_name, output_dir)

        try:
            result, metrics = build_zoom_layers(
                cache=cache,
                graph_config=gh_config,
                min_population=min_population,
                output_dir=output_dir,
                max_concurrent=max_concurrent,
            )
            if step_log:
                step_log(
                    f"{facet_name}: built zoom layers ({result.get('selected_edges', 0)} edges selected)",
                    level="success",
                )
            rv = {"result": result, "metrics": metrics}
            save_result_meta(qualified, cache, {"min_population": min_population}, rv)
            return rv
        except Exception as exc:
            log.error("Failed to build zoom layers: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to build zoom layers: {exc}", level="error")
            raise

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
