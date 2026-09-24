"""Pipeline orchestrator and export for Low-Zoom Road Infrastructure Builder.

Wires together graph construction, SBS computation, bypass/ring detection,
scoring, selection, and export into a complete pipeline.
"""

import csv
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

from facetwork.config import get_output_base

from ..combined.combined_handler import combined_scan
from ..combined.plugin_base import plugin_output
from ..shared._output import ensure_dir, open_output, read_storage_json

_LOCAL_OUTPUT = get_output_base()

from .zoom_detection import (
    detect_bypasses,
    detect_rings,
    save_bypass_flags,
    save_ring_flags,
)
from .zoom_graph import HAS_OSMIUM, RoadGraph, build_logical_graph
from .zoom_sbs import (
    HAS_REQUESTS,
    SegmentIndex,
    build_anchors,
    normalize_sbs,
    route_and_accumulate,
    sample_od_pairs,
    save_anchors,
    save_sbs,
)
from .zoom_selection import (
    build_cell_budgets,
    compute_scores,
    enforce_monotonic_reveal,
    select_edges,
)


def recipe_fingerprint() -> str:
    """Short hash of every tunable that changes what this pipeline selects.

    ⚠️ The output cache keys on the handler's declared params, and
    BuildZoomLayers declared only ``min_population`` — so a cached result was
    served to any later run of the same region, whatever else had changed.
    Measured repeatedly on 2026-09-23/24: a run into a NEW output_base came
    back with the old run's ``output_dir``, and every algorithm change had to
    be forced through by bumping ``min_population`` by 1. A hollow result is
    indistinguishable from a correct one to a key that cannot see the
    difference.

    Hashing the constants means retuning the ladder, the score weights, the
    budgets or the sampling automatically invalidates what they produced.
    """
    from .zoom_detection import (
        BYPASS_CORE_FRACTION_MAX,
        BYPASS_FC_ADVANTAGE,
        BYPASS_TIME_RATIO,
        RING_MIN_POPULATION,
        SETTLEMENT_RADII,
    )
    from .zoom_sbs import (
        ANCHOR_POP_THRESHOLDS,
        ANCHOR_TARGETS,
        DEFAULT_K_PAIRS,
        MIN_PAIR_DISTANCE_KM,
    )
    from .zoom_selection import (
        BASE_KM,
        MIN_FC_BY_ZOOM,
        MIN_KM,
        SKELETON_FCS,
        W_FC,
        W_SB,
    )

    recipe = json.dumps(
        {
            "min_fc_by_zoom": MIN_FC_BY_ZOOM,
            "skeleton": sorted(SKELETON_FCS),
            "w_sb": W_SB, "w_fc": W_FC,
            "base_km": BASE_KM, "min_km": MIN_KM,
            "anchor_pop": ANCHOR_POP_THRESHOLDS, "anchor_targets": ANCHOR_TARGETS,
            "k_pairs": DEFAULT_K_PAIRS, "min_pair_km": MIN_PAIR_DISTANCE_KM,
            "bypass": [BYPASS_TIME_RATIO, BYPASS_CORE_FRACTION_MAX, BYPASS_FC_ADVANTAGE],
            "settlement_radii": SETTLEMENT_RADII, "ring_min_pop": RING_MIN_POPULATION,
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(recipe.encode()).hexdigest()[:12]


def build_zoom_layers(
    cache: dict,
    graph_config: dict,
    min_population: int = 50_000,
    output_dir: str = "",
    max_concurrent: int = 16,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> tuple[dict, dict]:
    """Orchestrate the full Low-Zoom roads pipeline.

    ``heartbeat``/``check_cancel`` are threaded into the long loops (routing and
    vote accumulation) and called at every step boundary; without them a
    state-sized region outlives the stuck-task watchdog and gets reclaimed.

    Args:
        cache: OSMCache dict with 'path' key pointing to PBF file.
        graph_config: GraphHopperCache dict with 'graphDir' and 'profile'.
        min_population: Minimum population for city anchors.
        output_dir: Directory for output files.
        max_concurrent: Max concurrent GraphHopper requests.

    Returns:
        Tuple of (result_dict, metrics_dict).
    """
    if not output_dir:
        output_dir = os.path.join(_LOCAL_OUTPUT, "zoom-builder")
    t0 = time.time()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pbf_path = cache.get("path", "")
    graph_dir = graph_config.get("graphDir", "")
    profile = graph_config.get("profile", "car")

    # Determine cities path (look for existing cities GeoJSON)
    cities_path = str(out / "cities.geojson")
    city_count = _ensure_cities_file(
        pbf_path, cities_path, output_dir=str(out), heartbeat=heartbeat, cancel_check=check_cancel
    )

    # 1. Build logical edge graph from PBF
    log.info("Step 1: Building logical graph from %s", pbf_path)
    if heartbeat is not None:
        heartbeat("step 1")
    graph_path = str(out / "logical_graph.json")

    if not HAS_OSMIUM or not pbf_path:
        log.warning("pyosmium not available or no PBF path, returning empty result")
        return _empty_result(output_dir), _empty_metrics()

    road_graph = build_logical_graph(pbf_path, output_path=graph_path)

    if not road_graph.edges:
        log.warning("No logical edges built from PBF")
        return _empty_result(output_dir), _empty_metrics()

    # 2. Build anchor sets per zoom
    log.info("Step 2: Building anchor sets")
    if heartbeat is not None:
        heartbeat("step 2")
    anchors_by_zoom: dict[int, list[int]] = {}
    for z in range(2, 8):
        anchors_by_zoom[z] = build_anchors(road_graph, cities_path, z)
        anchors_path = str(out / f"anchors_z{z}.json")
        save_anchors(anchors_by_zoom[z], anchors_path)

    # 3. Compute SBS per zoom (the expensive step)
    log.info("Step 3: Computing SBS per zoom level")
    sbs_by_zoom: dict[int, dict[int, float]] = {}
    segment_index = SegmentIndex(road_graph)
    total_route_count = 0

    for z in range(2, 7):  # z2..z6 (z7 reuses z6)
        log.info("  SBS for zoom %d", z)
        if check_cancel is not None:
            check_cancel()
        if heartbeat is not None:
            heartbeat(f"step 3: SBS zoom {z}")
        pairs = sample_od_pairs(anchors_by_zoom[z], z, road_graph)

        if HAS_REQUESTS and graph_dir:
            # Stream: snap each route as it completes, retain none (see
            # route_and_accumulate for the 25 GB measurement behind this).
            bc, routed = route_and_accumulate(
                pairs,
                road_graph.node_coords,
                graph_dir,
                profile,
                segment_index,
                max_concurrent,
                heartbeat=heartbeat,
                check_cancel=check_cancel,
            )
            total_route_count += routed
        else:
            bc = {}
            log.info("  Skipping routing (no requests lib or graphDir)")

        sbs_by_zoom[z] = normalize_sbs(bc)
        save_sbs(sbs_by_zoom[z], str(out / f"sbs_z{z}.json"))

    # z7 reuses z6 SBS
    sbs_by_zoom[7] = sbs_by_zoom.get(6, {})

    # 4. Detect bypasses and rings
    log.info("Step 4: Detecting bypasses and rings")
    if heartbeat is not None:
        heartbeat("step 4")
    if HAS_REQUESTS and graph_dir:
        bypass_flags = detect_bypasses(
            road_graph, cities_path, graph_dir, profile,
            heartbeat=heartbeat, check_cancel=check_cancel,
        )
        ring_flags = detect_rings(
            road_graph, cities_path, graph_dir, profile,
            heartbeat=heartbeat, check_cancel=check_cancel,
        )
    else:
        bypass_flags = {}
        ring_flags = {}

    save_bypass_flags(bypass_flags, str(out / "bypass_flags.json"))
    save_ring_flags(ring_flags, str(out / "ring_flags.json"))

    # 5. Compute per-zoom scores
    log.info("Step 5: Computing scores")
    if heartbeat is not None:
        heartbeat("step 5")
    scores = compute_scores(road_graph, sbs_by_zoom, bypass_flags, ring_flags)

    # 6. Build cell budgets
    log.info("Step 6: Building cell budgets")
    if heartbeat is not None:
        heartbeat("step 6")
    budgets = build_cell_budgets(road_graph, anchors_by_zoom)

    # 7. Budgeted selection + backbone repair
    log.info("Step 7: Selecting edges")
    if heartbeat is not None:
        heartbeat("step 7")
    backbone_by_zoom: dict[int, set[int]] = {}
    selected_by_zoom = select_edges(
        road_graph,
        scores,
        budgets,
        anchors_by_zoom,
        bypass_flags,
        ring_flags,
        backbone_out=backbone_by_zoom,
    )

    # 8. Enforce monotonic reveal → assign minZoom
    log.info("Step 8: Enforcing monotonic reveal")
    if heartbeat is not None:
        heartbeat("step 8")
    assignments = enforce_monotonic_reveal(selected_by_zoom)

    # 9. Export
    log.info("Step 9: Exporting results")
    if heartbeat is not None:
        heartbeat("step 9")
    result, metrics = _export_results(
        road_graph,
        assignments,
        scores,
        sbs_by_zoom,
        bypass_flags,
        ring_flags,
        anchors_by_zoom,
        total_route_count,
        output_dir,
        city_count,
        backbone_by_zoom,
        time.time() - t0,
    )

    return result, metrics


def _ensure_cities_file(
    pbf_path: str,
    cities_path: str,
    output_dir: str = "",
    heartbeat: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    """Ensure a cities GeoJSON exists at ``cities_path``; return how many it holds.

    ⚠️ This used to write an EMPTY FeatureCollection and return — satisfying its
    own name with nothing. The consequences were invisible: ``build_anchors``
    finds no city over the population threshold and tops the set up with
    high-degree graph nodes ("Sparse anchor set (0) for z2"), so OD pairs are
    sampled between arbitrary junctions instead of population centres, and
    ``detect_bypasses``/``detect_rings`` return ``{}`` outright on an empty
    cities file — the bypass/ring flags were empty in every run ever made.

    It now extracts places from the PBF with the same single-pass scan the
    ``osm.POIs.Cities`` facet uses (the ``population`` plugin emits exactly the
    Point + ``properties.population`` shape ``build_anchors`` reads), so no
    caller has to supply a file the pipeline never produced. The scan is a full
    PBF pass, hence heartbeat/cancel-aware; it runs once per output dir because
    an existing file short-circuits.

    A failed extraction is a DEGRADATION, not a failure: the caller still gets a
    usable (topology-only) run. It returns 0 and says so loudly rather than
    leaving the caller to infer it from an anchor log line.
    """
    if Path(cities_path).exists():
        try:
            existing = read_storage_json(cities_path)
            return len(existing.get("features", []))
        except (OSError, json.JSONDecodeError):
            return 0

    features: list[dict] = []
    try:
        if heartbeat is not None:
            heartbeat("extracting cities from the PBF")
        scan = combined_scan(
            pbf_path,
            ["population"],
            output_dir=output_dir or str(Path(cities_path).parent),
            heartbeat=heartbeat,
            cancel_check=cancel_check,
        )
        src, _ = plugin_output(scan.results.get("population"))
        if src:
            features = read_storage_json(src).get("features", []) or []
    except Exception as exc:  # noqa: BLE001 — degradation, reported below
        log.warning("Could not extract cities from %s: %s", pbf_path, exc)

    if not features:
        log.warning(
            "No cities extracted from %s — anchors will be high-degree graph nodes and "
            "bypass/ring detection will be empty. This is a WEAKER run, not a failed one.",
            pbf_path,
        )

    ensure_dir(cities_path)
    with open_output(cities_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    log.info("Cities file: %d place(s) at %s", len(features), cities_path)
    return len(features)


def _export_results(
    graph: RoadGraph,
    assignments: dict[int, int],
    scores: dict[int, dict[int, float]],
    sbs_by_zoom: dict[int, dict[int, float]],
    bypass_flags: dict[int, str],
    ring_flags: dict[int, bool],
    anchors_by_zoom: dict[int, list[int]],
    total_route_count: int,
    output_dir: str,
    city_count: int,
    backbone_by_zoom: dict[int, set[int]],
    elapsed_seconds: float,
) -> tuple[dict, dict]:
    """Export all pipeline results to files."""
    out = Path(output_dir)

    # Edges the backbone repair added at ANY zoom. Hardcoded 0 until
    # 2026-09-23, alongside a per-edge "backbone" flag hardcoded False — so
    # this one of the three advertised flags had never been emitted.
    backbone_edges: set[int] = set()
    for _z, eids in (backbone_by_zoom or {}).items():
        backbone_edges |= eids

    # CSV export: segment_scores.csv
    csv_path = str(out / "segment_scores.csv")
    _export_csv(
        graph, assignments, scores, sbs_by_zoom, bypass_flags, ring_flags,
        backbone_edges, csv_path,
    )

    # JSONL export: edge_importance.jsonl
    jsonl_path = str(out / "edge_importance.jsonl")
    _export_jsonl(
        graph, assignments, scores, sbs_by_zoom, bypass_flags, ring_flags,
        backbone_edges, jsonl_path,
    )

    # Per-zoom GeoJSON (cumulative)
    for z in range(2, 8):
        geojson_path = str(out / f"roads_z{z}.geojson")
        _export_zoom_geojson(graph, assignments, z, geojson_path)

    # Compute statistics
    zoom_dist: dict[int, int] = defaultdict(int)
    for _eid, z in assignments.items():
        zoom_dist[z] += 1

    total_selected = len(assignments)
    backbone_count = len(backbone_edges)
    bypass_count = len([f for f in bypass_flags.values() if f == "bypass"])
    ring_count = len(ring_flags)

    pair_count = 0
    anchor_counts: dict[int, int] = {}
    pair_counts: dict[int, int] = {}
    for z in range(2, 8):
        ac = len(anchors_by_zoom.get(z, []))
        anchor_counts[z] = ac
        pc = ac * (ac - 1) // 2
        pair_counts[z] = pc
        pair_count += pc

    zoom_dist_str = ",".join(f"z{z}={zoom_dist.get(z, 0)}" for z in range(2, 8))

    # Metrics JSON
    metrics = {
        "total_logical_edges": len(graph.edges),
        "selected_edges": total_selected,
        "pruned_edges": len(graph.edges) - total_selected,
        "backbone_edges": backbone_count,
        "bypass_edges": bypass_count,
        "ring_edges": ring_count,
        # Cities actually loaded from the cities file — NOT the z2 anchor count,
        # which this used to report. That conflated "50 cities" with "50
        # high-degree nodes standing in for cities" in every run made before
        # the extraction above existed. The anchor counts remain below.
        "city_count": city_count,
        "cities_source": "extracted" if city_count else "none (topology-only anchors)",
        "anchor_counts": json.dumps(anchor_counts),
        "pair_counts": json.dumps(pair_counts),
        "route_count": total_route_count,
        "zoom_distribution": zoom_dist_str,
        "budget_utilization": "",
        "processing_seconds": round(elapsed_seconds, 1),
    }

    metrics_path = str(out / "metrics.json")
    ensure_dir(metrics_path)
    with open_output(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    # Result
    result = {
        "output_dir": output_dir,
        "total_logical_edges": len(graph.edges),
        "selected_edges": total_selected,
        "zoom_distribution": zoom_dist_str,
        "city_count": metrics["city_count"],
        "pair_count": pair_count,
        "route_count": total_route_count,
        "csv_path": csv_path,
        "metrics_path": metrics_path,
        "format": "CSV+GeoJSON+JSONL",
        "extraction_date": datetime.now(UTC).isoformat(),
    }

    return result, metrics


def _export_csv(
    graph: RoadGraph,
    assignments: dict[int, int],
    scores: dict[int, dict[int, float]],
    sbs_by_zoom: dict[int, dict[int, float]],
    bypass_flags: dict[int, str],
    ring_flags: dict[int, bool],
    backbone_edges: set[int],
    path: str,
) -> None:
    """Export per-edge data to CSV."""
    fieldnames = [
        "edgeId",
        "fromNode",
        "toNode",
        "osmWayIds",
        "lengthM",
        "fc",
        "minZoom",
    ]
    for z in range(2, 8):
        fieldnames.append(f"score_z{z}")
    for z in range(2, 8):
        fieldnames.append(f"sb_z{z}")
    fieldnames.extend(
        [
            "backbone",
            "isBypass",
            "isRing",
            "isLegacyThruTown",
            "isBridgeOrTunnel",
        ]
    )

    ensure_dir(path)
    with open_output(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for edge in graph.edges:
            eid = edge.edge_id
            if eid not in assignments:
                continue

            row = {
                "edgeId": eid,
                "fromNode": edge.from_node,
                "toNode": edge.to_node,
                "osmWayIds": ",".join(str(w) for w in edge.osm_way_ids),
                "lengthM": round(edge.length_m, 1),
                "fc": edge.fc,
                "minZoom": assignments[eid],
                "backbone": eid in backbone_edges,
                "isBypass": bypass_flags.get(eid) == "bypass",
                "isRing": ring_flags.get(eid, False),
                "isLegacyThruTown": bypass_flags.get(eid) == "thru_town",
                "isBridgeOrTunnel": edge.bridge or edge.tunnel,
            }

            for z in range(2, 8):
                row[f"score_z{z}"] = round(scores.get(z, {}).get(eid, 0.0), 4)
                row[f"sb_z{z}"] = round(sbs_by_zoom.get(z, {}).get(eid, 0.0), 4)

            writer.writerow(row)

    log.info("Exported CSV: %s", path)


def _export_jsonl(
    graph: RoadGraph,
    assignments: dict[int, int],
    scores: dict[int, dict[int, float]],
    sbs_by_zoom: dict[int, dict[int, float]],
    bypass_flags: dict[int, str],
    ring_flags: dict[int, bool],
    backbone_edges: set[int],
    path: str,
) -> None:
    """Export per-edge data to JSON Lines."""
    ensure_dir(path)
    with open_output(path, "w") as f:
        for edge in graph.edges:
            eid = edge.edge_id
            if eid not in assignments:
                continue

            record = {
                "edgeId": eid,
                "fromNode": edge.from_node,
                "toNode": edge.to_node,
                "osmWayIds": edge.osm_way_ids,
                "lengthM": round(edge.length_m, 1),
                "fc": edge.fc,
                "minZoom": assignments[eid],
                "scores": {z: round(scores.get(z, {}).get(eid, 0.0), 4) for z in range(2, 8)},
                "sbs": {z: round(sbs_by_zoom.get(z, {}).get(eid, 0.0), 4) for z in range(2, 8)},
                "flags": {
                    "backbone": eid in backbone_edges,
                    "isBypass": bypass_flags.get(eid) == "bypass",
                    "isRing": ring_flags.get(eid, False),
                    "isLegacyThruTown": bypass_flags.get(eid) == "thru_town",
                    "isBridgeOrTunnel": edge.bridge or edge.tunnel,
                },
            }
            f.write(json.dumps(record) + "\n")

    log.info("Exported JSONL: %s", path)


def _export_zoom_geojson(
    graph: RoadGraph,
    assignments: dict[int, int],
    zoom: int,
    path: str,
) -> None:
    """Export cumulative GeoJSON for a zoom level (includes all z <= zoom)."""
    features = []
    for edge in graph.edges:
        eid = edge.edge_id
        min_z = assignments.get(eid)
        if min_z is None or min_z > zoom:
            continue

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "edge_id": eid,
                    "fc": edge.fc,
                    "min_zoom": min_z,
                    "ref": edge.ref,
                    "name": edge.name,
                    "length_m": round(edge.length_m, 1),
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": list(edge.coords),
                },
            }
        )

    geojson = {"type": "FeatureCollection", "features": features}
    ensure_dir(path)
    with open_output(path, "w") as f:
        json.dump(geojson, f)

    log.info("Exported z%d GeoJSON: %d features → %s", zoom, len(features), path)


def _empty_result(output_dir: str) -> dict:
    """Return empty result dict."""
    return {
        "output_dir": output_dir,
        "total_logical_edges": 0,
        "selected_edges": 0,
        "zoom_distribution": "",
        "city_count": 0,
        "pair_count": 0,
        "route_count": 0,
        "csv_path": "",
        "metrics_path": "",
        "format": "CSV+GeoJSON+JSONL",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _empty_metrics() -> dict:
    """Return empty metrics dict."""
    return {
        "total_logical_edges": 0,
        "selected_edges": 0,
        "pruned_edges": 0,
        "backbone_edges": 0,
        "bypass_edges": 0,
        "ring_edges": 0,
        "city_count": 0,
        "anchor_counts": "{}",
        "pair_counts": "{}",
        "route_count": 0,
        "zoom_distribution": "",
        "budget_utilization": "",
        "processing_seconds": 0.0,
    }
