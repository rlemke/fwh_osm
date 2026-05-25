"""Event-facet handler for osm.Tiles.BuildVectorTiles.

Wires the facet to the path-based ``build_from_geojson`` in
``_osm_tools.vector_tiles_build`` (tippecanoe → MBTiles, → PMTiles when the
pmtiles CLI is present). The blocking tippecanoe run is pumped with a heartbeat
so the task lease survives a large tiling job; output is PMTiles if pmtiles is
on PATH, else MBTiles.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading

from ..shared._output import derive_output_path, uri_stem
from ..shared.output_cache import cached_result, save_result_meta
from ..shared.pbf_convert import vector_tiles

log = logging.getLogger(__name__)

NAMESPACE = "osm.Tiles"
_HEARTBEAT_INTERVAL = 30.0


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _run_with_heartbeat(payload: dict, fn):
    hb = payload.get("_task_heartbeat")
    if not callable(hb):
        return fn()
    stop = threading.Event()

    def _pump():
        while not stop.wait(_HEARTBEAT_INTERVAL):
            try:
                hb()
            except Exception:
                pass

    t = threading.Thread(target=_pump, name="tiles-heartbeat", daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()


def _make_build_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        geojson_path = payload.get("geojson_path", "")
        layer_name = payload.get("layer_name", "features") or "features"
        min_zoom = int(payload.get("min_zoom", 0))
        max_zoom = int(payload.get("max_zoom", 14))
        step_log = payload.get("_step_log")

        input_cache = {"path": geojson_path, "size": _file_size(geojson_path)}
        cp = {"layer_name": layer_name, "min_zoom": min_zoom, "max_zoom": max_zoom}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if not geojson_path:
            return {
                "result": {
                    "output_path": "", "format": "", "size_bytes": 0,
                    "min_zoom": min_zoom, "max_zoom": max_zoom, "layer": layer_name,
                }
            }

        # PMTiles when the CLI is available (smaller, single-file, HTTP-range
        # servable); else the universal MBTiles.
        ext = "pmtiles" if shutil.which("pmtiles") else "mbtiles"
        output_path = derive_output_path(
            "vector-tiles", uri_stem(geojson_path), "tiles",
            layer_name, f"z{min_zoom}-{max_zoom}", ext=ext,
            run_id=payload.get("_workflow_id", "") or None,
        )

        if step_log:
            step_log(f"{facet_name}: tiling {geojson_path} -> {ext} (z{min_zoom}-{max_zoom})")

        from facetwork.runtime.storage import localize
        local_input = localize(geojson_path)

        result = _run_with_heartbeat(
            payload,
            lambda: vector_tiles.build_from_geojson(
                local_input, output_path,
                min_zoom=min_zoom, max_zoom=max_zoom, layer_name=layer_name,
            ),
        )

        if step_log:
            step_log(
                f"{facet_name}: built {result.format} {result.size_bytes / 1e6:.2f} MB "
                f"(z{result.min_zoom}-{result.max_zoom})",
                level="success",
            )
        rv = {
            "result": {
                "output_path": result.output_path,
                "format": result.format,
                "size_bytes": result.size_bytes,
                "min_zoom": result.min_zoom,
                "max_zoom": result.max_zoom,
                "layer": result.layer,
            }
        }
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


TILE_FACETS = [("BuildVectorTiles", _make_build_handler)]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in TILE_FACETS:
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
    """Register osm.Tiles facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_tile_handlers(poller) -> None:
    """Register osm.Tiles event facet handlers with an AgentPoller."""
    for facet_name, factory in TILE_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered tile handler: %s", qualified_name)
