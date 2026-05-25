"""Event-facet handlers for the ``osm.Clip`` namespace.

Wires ClipByBBox / ClipByPolygon (osmclip.ffl) to the existing
``_osm_tools.pbf_clip`` tool (osmium extract + ``pbf-clips`` sidecar cache).
The handler's job is the facet boundary: derive the source region from the
incoming OSMCache, mint a flat, deterministic clip name, run the (blocking)
osmium extract with a heartbeat so the task lease survives a multi-minute
clip, and return a *new* OSMCache over the clipped PBF so downstream
ExtractCategory / Spatial steps compose on the smaller region.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading

from ..shared.output_cache import cached_result, save_result_meta
from ..shared.pbf_convert import clip as clip_tool

log = logging.getLogger(__name__)

NAMESPACE = "osm.Clip"

# Pump the task heartbeat this often (seconds) while osmium extract blocks, so
# the lease (default 5 min) does not expire mid-clip — the CombinedScan lesson
# applied to a blocking subprocess that cannot heartbeat from inside.
_HEARTBEAT_INTERVAL = 30.0


def _source_region(cache: dict) -> str:
    """The Geofabrik region key for the cached source PBF (clip_pbf's input).

    ``OSMCache.region`` is a typed Region whose ``geofabrik_path`` (e.g.
    ``north-america/us/california``) is the authoritative source key the
    ``pbf`` cache sidecar is filed under. Fails explicitly rather than guessing.
    """
    region = cache.get("region") or {}
    src = region.get("geofabrik_path") or region.get("canonical") or ""
    if not src:
        raise ValueError(
            "ClipBy*: OSMCache.region has no geofabrik_path/canonical — cannot "
            "resolve the source PBF to clip. Build the cache via osm.ops.CacheRegion."
        )
    return src


def _sanitize(text: str) -> str:
    """Flatten to a clip name clip_pbf accepts (no '/'; keep [-._] readable)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "region"


def _bbox_clip_name(source_region: str, bbox: tuple[float, float, float, float]) -> str:
    """Deterministic flat name for a bbox clip — idempotent across runs."""
    leaf = source_region.rsplit("/", 1)[-1] or "region"
    body = "_".join(f"{x:g}" for x in bbox)
    return _sanitize(f"{leaf}_bbox_{body}")


def _polygon_clip_name(source_region: str, polygon_path: str) -> str:
    """Deterministic flat name for a polygon clip (keyed by polygon content)."""
    leaf = source_region.rsplit("/", 1)[-1] or "region"
    try:
        with open(polygon_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:10]
    except OSError as exc:
        raise ValueError(f"ClipByPolygon: cannot read polygon {polygon_path!r}: {exc}") from exc
    return _sanitize(f"{leaf}_poly_{digest}")


def _run_with_heartbeat(payload: dict, fn):
    """Run blocking ``fn`` while pumping the task heartbeat in a daemon thread."""
    hb = payload.get("_task_heartbeat")
    if not callable(hb):
        return fn()
    stop = threading.Event()

    def _pump():
        while not stop.wait(_HEARTBEAT_INTERVAL):
            try:
                hb()
            except Exception:  # heartbeat best-effort; never kill the clip
                pass

    t = threading.Thread(target=_pump, name="clip-heartbeat", daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()


def _clipped_cache(source_cache: dict, result) -> dict:
    """Build a new OSMCache over the clipped PBF, preserving source provenance."""
    return {
        "region": source_cache.get("region") or {},
        "url": "",
        "path": result.path,
        "date": result.generated_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
    }


def _make_bbox_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache") or {}
        bbox = (
            float(payload.get("west", 0.0)),
            float(payload.get("south", 0.0)),
            float(payload.get("east", 0.0)),
            float(payload.get("north", 0.0)),
        )
        step_log = payload.get("_step_log")
        source_region = _source_region(cache)
        name = _bbox_clip_name(source_region, bbox)

        # Output-cache short-circuit: keyed on the source PBF + the bbox. (clip_pbf
        # also has its own sidecar cache; this avoids even spawning osmium on a hit.)
        input_cache = {"path": cache.get("path", ""), "size": cache.get("size", 0)}
        cp = {"source_region": source_region, "bbox": list(bbox)}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: clipping {source_region} to bbox {bbox} -> {name}")
        log.info("%s clipping %s to bbox %s (name=%s)", facet_name, source_region, bbox, name)

        result = _run_with_heartbeat(
            payload,
            lambda: clip_tool.clip_pbf(name, source_region, bbox=bbox),
        )

        if step_log:
            mb = result.size_bytes / 1e6
            step_log(
                f"{facet_name}: clip {'(cached) ' if result.was_cached else ''}"
                f"{name} = {mb:.1f} MB",
                level="success",
            )
        rv = {"cache": _clipped_cache(cache, result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


def _make_polygon_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache") or {}
        polygon_path = payload.get("polygon_path", "")
        step_log = payload.get("_step_log")
        source_region = _source_region(cache)
        if not polygon_path:
            raise ValueError("ClipByPolygon: polygon_path is required")
        name = _polygon_clip_name(source_region, polygon_path)

        input_cache = {"path": cache.get("path", ""), "size": cache.get("size", 0)}
        cp = {"source_region": source_region, "polygon": name}
        hit = cached_result(qualified, input_cache, cp, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: clipping {source_region} to polygon {polygon_path} -> {name}")
        log.info("%s clipping %s to polygon %s (name=%s)", facet_name, source_region, polygon_path, name)

        result = _run_with_heartbeat(
            payload,
            lambda: clip_tool.clip_pbf(name, source_region, polygon_path=polygon_path),
        )

        if step_log:
            mb = result.size_bytes / 1e6
            step_log(
                f"{facet_name}: clip {'(cached) ' if result.was_cached else ''}{name} = {mb:.1f} MB",
                level="success",
            )
        rv = {"cache": _clipped_cache(cache, result)}
        save_result_meta(qualified, input_cache, cp, rv)
        return rv

    return handler


CLIP_FACETS = [
    ("ClipByBBox", _make_bbox_handler),
    ("ClipByPolygon", _make_polygon_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, factory in CLIP_FACETS:
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
    """Register all osm.Clip facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_clip_handlers(poller) -> None:
    """Register all osm.Clip event facet handlers with an AgentPoller."""
    for facet_name, factory in CLIP_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, factory(facet_name))
        log.debug("Registered clip handler: %s", qualified_name)
