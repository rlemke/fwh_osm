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

from ...tools._osm_tools.cancellation import HandlerCancelled
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
from .zoom_graph import FERRY_FC, HAS_OSMIUM, RoadGraph, build_logical_graph
from .zoom_sbs import (
    DEFAULT_K_PAIRS,
    HAS_REQUESTS,
    SegmentIndex,
    SnapPool,
    build_anchors,
    load_sbs,
    min_pair_km,
    normalize_sbs,
    probe_router,
    region_scale,
    route_and_accumulate,
    sample_od_pairs,
    save_anchors,
    save_sbs,
    snap_workers_default,
)
from .zoom_selection import (
    build_cell_budgets,
    compute_scores,
    enforce_monotonic_reveal,
    prune_assignments,
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
        ANCHOR_MIN_COUNT,
        ANCHOR_POP_THRESHOLDS,
        ANCHOR_TARGETS,
        DEFAULT_K_PAIRS,
        MIN_PAIR_DISTANCE_KM,
        MIN_PAIR_FLOOR_KM,
        REGION_REF_KM,
        SETTLEMENT_PLACES,
    )
    from .zoom_selection import (
        BASE_KM,
        CORRIDOR_OVERDRAFT,
        CORRIDOR_SELECTION,
        MIN_COMPONENT_KM,
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
            "min_component_km": MIN_COMPONENT_KM,
            # ⚠️ Behaviour, not a constant: the prune moved from per-zoom
            # pre-union to one monotonic post-union pass. The fingerprint hashes
            # CONSTANTS, so a code-only change is invisible to it and every cached
            # layer would come back with the old behaviour.
            "prune_stage": "post-union-monotonic",
            # Behaviour: car ferries enter the edge graph and are admitted
            # where routed (step 7b). Cached layers built without them would
            # come back with every crossing missing.
            "ferries": "car-ferries-routed-only",
            # ⚠️ Behaviour again, and the same reason: the UNIT OF SELECTION
            # changed from the edge to the corridor, so a road is admitted whole
            # or deferred whole. Constants alone cannot see that, and every
            # cached layer would come back per-edge — fourth time this trap has
            # been paid for in a month.
            "selection_unit": (
                f"corridor/overdraft={CORRIDOR_OVERDRAFT}"
                if CORRIDOR_SELECTION
                else "edge"
            ),
            # Output shape, not selection: the zoom layers now carry `routed`,
            # which the viewer splits each band on. A cached layer from before
            # would come back without it and the split would show nothing.
            "layer_props": "routed",
            # Anchor RULES that are code, not constants: settlements only, and no
            # high-degree-junction top-up. Without these the cache would serve a
            # layer routed between state centroids and junctions.
            "anchor_places": sorted(SETTLEMENT_PLACES),
            "anchor_topup": "none",
            # Code, not a constant: z7 routes its own pairs instead of reusing z6.
            "sbs_zooms": "2-7",
            # Region scaling of the pair minimum + the spaced z2/z3 floor.
            "region_ref_km": REGION_REF_KM,
            "pair_floor_km": MIN_PAIR_FLOOR_KM,
            "anchor_min_count": ANCHOR_MIN_COUNT,
            "anchors_cumulative": True,
            # Code: routes are densified before snapping (zoom_sbs.SegmentIndex).
            "snap_densify_m": SegmentIndex.DENSIFY_STEP_M,
            # Code: numbered routes are bridged one class below the floor.
            "route_gap_bridging": "one-class",
            # Code: snapping follows the current edge before a grid search.
            "snap_follow_edge_m": SegmentIndex.STICKY_M,
            "anchor_pop": ANCHOR_POP_THRESHOLDS, "anchor_targets": ANCHOR_TARGETS,
            "k_pairs": DEFAULT_K_PAIRS, "min_pair_km": MIN_PAIR_DISTANCE_KM,
            "bypass": [BYPASS_TIME_RATIO, BYPASS_CORE_FRACTION_MAX, BYPASS_FC_ADVANTAGE],
            "settlement_radii": SETTLEMENT_RADII, "ring_min_pop": RING_MIN_POPULATION,
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(recipe.encode()).hexdigest()[:12]


def _sbs_checkpoint_key(z: int, pairs: list, graph, pbf_path: str) -> str:
    """Identity of one zoom's routing: recipe, input, graph size and the pairs."""
    h = hashlib.sha256()
    h.update(f"{recipe_fingerprint()}|{pbf_path}|{len(graph.edges)}|{z}|".encode())
    h.update(json.dumps(pairs, sort_keys=True, default=str).encode())
    return h.hexdigest()


def build_zoom_layers(
    cache: dict,
    graph_config: dict,
    min_population: int = 50_000,
    output_dir: str = "",
    max_concurrent: int = 16,
    heartbeat: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    route_max_zoom: int = 7,
    max_pairs: int = 0,
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
        route_max_zoom: Highest zoom that routes its city pairs (2-7). Zooms
            above it get no betweenness, so their layers are selected by class
            and name alone and carry ``routed = false``. A country-scale run on
            a major-roads graph routes z2-z4 only: z5-z7 pair hundreds of
            thousands of towns over continental distances, and the graph has
            none of the roads those tiers ride.
        max_pairs: Cap on the city pairs routed per zoom (0 = the per-zoom
            default). Matching a route onto the graph costs ~1 ms per km, so a
            country's z4 default of 50k pairs averaging >1,000 km is ~18 h; a
            random sample keeps the distance mix, only the votes get noisier.

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
    # The region's extent scales the pair minimum and the z2/z3 floor spacing
    # (see zoom_sbs.region_scale): continental distances route nothing in a state.
    scale = region_scale(road_graph)
    log.info(
        "Region scale %.3f: pair minimum z2..z7 = %s km",
        scale, ", ".join(f"{min_pair_km(z, scale):.0f}" for z in range(2, 8)),
    )
    # The cities actually anchored, each with the first zoom it anchored at, so
    # the map draws exactly what was routed instead of re-deriving the rule.
    tier_of: dict[str, dict] = {}
    carried: list[tuple] = []  # cumulative: each zoom keeps the lower zooms' cities
    for z in range(2, 8):
        # Per zoom: at country scale one zoom's anchors took 10-20 min before
        # snapping was indexed, and a single heartbeat for the whole step let
        # the stuck-task watchdog reclaim it.
        if check_cancel is not None:
            check_cancel()
        if heartbeat is not None:
            heartbeat(f"step 2: anchors z{z}")
        chosen: list[dict] = []
        anchors_by_zoom[z] = build_anchors(road_graph, cities_path, z, scale, chosen, carried)
        anchors_path = str(out / f"anchors_z{z}.json")
        save_anchors(anchors_by_zoom[z], anchors_path)
        for f in chosen:
            key = f"{f['geometry']['coordinates']}|{f['properties']['name']}"
            if key not in tier_of:
                f["properties"]["tier"] = z
                tier_of[key] = f
    ensure_dir(str(out / "anchor_cities.geojson"))
    with open_output(str(out / "anchor_cities.geojson"), "w") as fh:
        json.dump({"type": "FeatureCollection", "features": list(tier_of.values())}, fh)

    # 3. Compute SBS per zoom (the expensive step)
    log.info("Step 3: Computing SBS per zoom level")
    sbs_by_zoom: dict[int, dict[int, float]] = {}
    segment_index = SegmentIndex(road_graph)
    total_route_count = 0

    # Prove the router serves THIS region before spending hours on it. A server
    # holding another state rejects every pair as out of bounds and _route_pair
    # swallows that like any other miss, so the alternative is a run that
    # completes with zero betweenness and reports success.
    if HAS_REQUESTS and graph_dir:
        if heartbeat is not None:
            heartbeat("step 3: probing the routing server")
        # Take the sample from the first zoom that HAS one. Zoom 2 wants anchors
        # over 500k population at least 300 km apart, which Delaware, DC and
        # Rhode Island legitimately cannot supply — probing only z2 would skip
        # the check on exactly the small states, silently.
        probe_pairs: list = []
        for pz in range(2, route_max_zoom + 1):
            probe_pairs = sample_od_pairs(anchors_by_zoom[pz], pz, road_graph, scale=scale)
            if probe_pairs:
                log.info("Probing the router with zoom-%d pairs", pz)
                break
        probe_router(road_graph.node_coords, probe_pairs, profile, graph_dir=graph_dir)

    # One snap pool for every zoom: its workers map the index once and keep it
    # (see SnapPool for the measurement behind it).
    snap_workers = snap_workers_default() if (HAS_REQUESTS and graph_dir) else 0
    snapper = SnapPool(segment_index, snap_workers, str(out)) if snap_workers >= 2 else None
    try:
        # Every zoom routes its own cities, z7 included. z7 used to REUSE z6's SBS,
        # so its 5k+ cities (the z7 tier) were never routed between: on the map they
        # sat unconnected while every other tier's routes ran city to city.
        for z in range(2, 8):
            if z > route_max_zoom:
                log.info("  SBS for zoom %d skipped (route_max_zoom=%d)", z, route_max_zoom)
                sbs_by_zoom[z] = {}
                continue
            log.info("  SBS for zoom %d", z)
            if check_cancel is not None:
                check_cancel()
            if heartbeat is not None:
                heartbeat(f"step 3: SBS zoom {z}")
            k = DEFAULT_K_PAIRS.get(z, 5_000)
            if max_pairs > 0:
                k = min(k, max_pairs)
            pairs = sample_od_pairs(anchors_by_zoom[z], z, road_graph, k_pairs=k, scale=scale)

            # Resume: a reclaimed attempt on the same host finds the zoom it already
            # routed. Measured 2026-10-12: the US z2-z4 build was killed three times
            # AFTER routing, and each retry spent ~3 h rerouting identical pairs.
            sbs_path = out / f"sbs_z{z}.json"
            key_path = out / f"sbs_z{z}.key"
            sbs_key = _sbs_checkpoint_key(z, pairs, road_graph, pbf_path)
            if sbs_path.exists() and key_path.exists() and key_path.read_text() == sbs_key:
                sbs_by_zoom[z] = load_sbs(str(sbs_path))
                log.info("  SBS for zoom %d loaded from checkpoint (%d edges)", z, len(sbs_by_zoom[z]))
                continue

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
                    snapper=snapper,
                )
                total_route_count += routed
            else:
                bc = {}
                log.info("  Skipping routing (no requests lib or graphDir)")

            sbs_by_zoom[z] = normalize_sbs(bc)
            save_sbs(sbs_by_zoom[z], str(sbs_path))
            key_path.write_text(sbs_key)
    except BaseException:
        if snapper is not None:
            snapper.close(cancel=True)
            snapper = None
        raise
    finally:
        if snapper is not None:
            snapper.close()

    # 4. Detect bypasses and rings
    log.info("Step 4: Detecting bypasses and rings")
    if heartbeat is not None:
        heartbeat("step 4")
    if HAS_REQUESTS and graph_dir:
        # One index for steps 3 and 4; step 3's memo is useless here (other
        # routes) and is the bulk of its size.
        segment_index.clear_memo()
        bypass_flags = detect_bypasses(
            road_graph, cities_path, graph_dir, profile,
            heartbeat=heartbeat, check_cancel=check_cancel,
            segment_index=segment_index,
        )
        segment_index.clear_memo()
        ring_flags = detect_rings(
            road_graph, cities_path, graph_dir, profile,
            heartbeat=heartbeat, check_cancel=check_cancel,
            segment_index=segment_index,
        )
        segment_index.clear_memo()
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

    # 7b. Ferries a zoom's routes rode. Class selection never admits one (no
    # class, score 0 -- see zoom_graph.FERRY_FC), so a crossing appears exactly
    # where routes between this zoom's cities take it, and nowhere else.
    ferry_ids = {e.edge_id for e in road_graph.edges if getattr(e, "fc", None) == FERRY_FC}
    if ferry_ids:
        for z, sel in selected_by_zoom.items():
            rode = {eid for eid in ferry_ids if sbs_by_zoom.get(z, {}).get(eid, 0.0) > 0.0}
            if rode:
                sel |= rode
                log.info("Zoom %d: %d routed ferry edge(s) admitted", z, len(rode))

    # 8. Enforce monotonic reveal → assign minZoom
    log.info("Step 8: Enforcing monotonic reveal")
    if heartbeat is not None:
        heartbeat("step 8")
    assignments = enforce_monotonic_reveal(selected_by_zoom)

    # 8b. Generalisation: drop the orphan fragments per-edge selection leaves.
    # AFTER the union, because the union is what the map shows, and applied as one
    # set across every zoom, because the bar rises with zoom and pruning the layers
    # independently would make a road vanish as you zoom IN.
    assignments, pruned_n, pruned_km = prune_assignments(
        road_graph, assignments, backbone_by_zoom
    )
    log.info("Step 8b: pruned %d orphan edges (%.0f km) from every zoom", pruned_n, pruned_km)

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
    # An EMPTY file is a degraded run's leftover, never a result: the work dir
    # is deterministic so a reclaim can resume, and reusing an empty file there
    # routed nothing (US z2-z4, 2026-10-14). Only a non-empty file short-circuits.
    if Path(cities_path).exists():
        try:
            n = len(read_storage_json(cities_path).get("features", []))
        except (OSError, json.JSONDecodeError):
            n = 0
        if n:
            return n

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
    except HandlerCancelled:
        # A cancel is a STOP, not a failed extraction: writing the empty file
        # here is what a reclaimed task on the same host later reused.
        raise
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
        _export_zoom_geojson(graph, assignments, z, geojson_path, sbs_by_zoom)

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
    sbs_by_zoom: dict[int, dict[int, float]] | None = None,
) -> None:
    """Export cumulative GeoJSON for a zoom level (includes all z <= zoom).

    With ``sbs_by_zoom``, each edge also carries ``routed``: whether THIS
    band's routes -- the sampled pairs between this zoom's cities -- rode it.
    False means the band holds it on name/kind alone (class, the motorway/trunk
    skeleton, backbone repair, the sparse-cell top-up, or the rest of a routed
    corridor). The viewer splits each band on it, so "z5 routed" draws the
    roads the z5 city-to-city routes actually drove.

    ⚠️ Per BAND, not per reveal zoom. Keyed on the zoom an edge was revealed
    at, an Interstate admitted at z2 as skeleton (where nothing routes) read
    "name/kind" in every band -- so Montana's z5 routed view showed fragments
    instead of the I-90 that every z5 route between its cities drives.
    """
    features = []
    for edge in graph.edges:
        eid = edge.edge_id
        min_z = assignments.get(eid)
        if min_z is None or min_z > zoom:
            continue

        props = {
            "edge_id": eid,
            "fc": edge.fc,
            "min_zoom": min_z,
            "ref": edge.ref,
            "name": edge.name,
            "length_m": round(edge.length_m, 1),
        }
        if sbs_by_zoom is not None:
            props["routed"] = sbs_by_zoom.get(zoom, {}).get(eid, 0.0) > 0.0
        features.append(
            {
                "type": "Feature",
                "properties": props,
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
