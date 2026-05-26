"""Event-facet handlers for the ``osm.Network`` namespace.

Wires BuildNetwork / ApproxRoute / RouteMatrix (osmnetwork.ffl) to the pure
compute core in :mod:`network_ops`. Follows the transform/spatial handler
pattern: per-facet factories, output cache, RegistryRunner dispatch + poller
helper. The ops bodies raise NotImplementedError until Phase 1 (see the design
doc); these handlers are the stable contract around them.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .network_ops import approx_route, build_network, route_layer, route_matrix

log = logging.getLogger(__name__)

NAMESPACE = "osm.Network"


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _empty_network() -> dict:
    return {
        "result": {
            "network_path": "",
            "node_count": 0,
            "edge_count": 0,
            "connected_components": 0,
            "largest_component_frac": 0.0,
            "snap_tolerance_m": 0.0,
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _empty_route() -> dict:
    return {
        "result": {
            "route_path": "",
            "distance_km": 0.0,
            "reached_lat": 0.0,
            "reached_lon": 0.0,
            "gap_to_b_km": 0.0,
            "reached_b": False,
            "node_hops": 0,
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _empty_matrix() -> dict:
    return {
        "result": {
            "result_path": "",
            "pair_count": 0,
            "reachable_count": 0,
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _empty_routelayer() -> dict:
    return {
        "result": {
            "output_path": "",
            "route_count": 0,
            "reachable_count": 0,
            "point_count": 0,
            "extraction_date": datetime.now(UTC).isoformat(),
        }
    }


def _make_build_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        edges_path = payload.get("edges_path", "")
        snap_tolerance_m = float(payload.get("snap_tolerance_m", 25.0) or 25.0)
        ref_filter = payload.get("ref_filter", "") or ""
        step_log = payload.get("_step_log")

        input_cache = {"path": edges_path, "size": _file_size(edges_path)}
        cp = {"snap_tolerance_m": snap_tolerance_m, "ref_filter": ref_filter}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if not edges_path:
            return _empty_network()
        if step_log:
            step_log(f"{facet_name}: noding {edges_path} (tol={snap_tolerance_m}m"
                     f"{', ref=' + ref_filter if ref_filter else ''})")

        result = build_network(
            edges_path, snap_tolerance_m=snap_tolerance_m, ref_filter=ref_filter,
            heartbeat=payload.get("_task_heartbeat"), run_id=payload.get("_workflow_id", ""),
        )
        if step_log:
            step_log(f"{facet_name}: {result.node_count} nodes / {result.edge_count} edges, "
                     f"{result.connected_components} components", level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_route_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        network_path = payload.get("network_path", "")
        from_lat = float(payload.get("from_lat", 0.0) or 0.0)
        from_lon = float(payload.get("from_lon", 0.0) or 0.0)
        to_lat = float(payload.get("to_lat", 0.0) or 0.0)
        to_lon = float(payload.get("to_lon", 0.0) or 0.0)
        step_log = payload.get("_step_log")

        input_cache = {"path": network_path, "size": _file_size(network_path)}
        cp = {"from": [from_lon, from_lat], "to": [to_lon, to_lat]}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if not network_path:
            return _empty_route()
        if step_log:
            step_log(f"{facet_name}: route ({from_lat},{from_lon}) -> ({to_lat},{to_lon})")

        result = approx_route(
            network_path, from_lat, from_lon, to_lat, to_lon,
            heartbeat=payload.get("_task_heartbeat"), run_id=payload.get("_workflow_id", ""),
        )
        if step_log:
            reached = "reached B" if result.reached_b else f"gap {result.gap_to_b_km:.1f}km to B"
            step_log(f"{facet_name}: {result.distance_km:.1f}km ({reached})", level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_matrix_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        network_path = payload.get("network_path", "")
        points = payload.get("points", "") or ""
        step_log = payload.get("_step_log")

        input_cache = {"path": network_path, "size": _file_size(network_path)}
        cp = {"points": points}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if not network_path or not points:
            return _empty_matrix()
        if step_log:
            step_log(f"{facet_name}: all-pairs matrix over {network_path}")

        result = route_matrix(
            network_path, points,
            heartbeat=payload.get("_task_heartbeat"), run_id=payload.get("_workflow_id", ""),
        )
        if step_log:
            step_log(f"{facet_name}: {result.reachable_count}/{result.pair_count} pairs reachable",
                     level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_routelayer_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        network_path = payload.get("network_path", "")
        points = payload.get("points", "") or ""
        step_log = payload.get("_step_log")

        input_cache = {"path": network_path, "size": _file_size(network_path)}
        cp = {"points": points}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if not network_path or not points:
            return _empty_routelayer()
        if step_log:
            step_log(f"{facet_name}: drawing all-pairs routes over {network_path}")

        result = route_layer(
            network_path, points,
            heartbeat=payload.get("_task_heartbeat"), run_id=payload.get("_workflow_id", ""),
        )
        if step_log:
            step_log(f"{facet_name}: {result.route_count} routes "
                     f"({result.reachable_count} reachable) over {result.point_count} points",
                     level="success")
        rv = {"result": result.to_dict()}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


NETWORK_FACETS = [
    ("BuildNetwork", _make_build_handler),
    ("ApproxRoute", _make_route_handler),
    ("RouteMatrix", _make_matrix_handler),
    ("RouteLayer", _make_routelayer_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in NETWORK_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = factory(facet_name)


_build_dispatch()


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.Network facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_network_handlers(poller) -> None:
    """Register all osm.Network event facet handlers with an AgentPoller."""
    for facet_name, factory in NETWORK_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered network handler: %s", qualified_name)
