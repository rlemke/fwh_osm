"""Handlers for ``osm.query`` — ad-hoc tag queries against the local extracts.

The point of this facet is what it *replaces*. Every question outside
``pbf_extract``'s 25 fixed categories has had to go to Overpass, and Overpass is
where the fleet keeps meeting somebody else's rate limit: the tag-quality maps
are documented "cache-first, do NOT fan out (egress rate-limit)", the ALPR map
is one query because a fan-out would be throttled. The data is already on disk —
this hands it to any workflow with an arbitrary filter.

Thin wrapper over ``tools/_osm_tools/tag_query``, so the CLI and the FFL share
one implementation. Blocking disk I/O (a continent is minutes, the planet under
an hour) — registered with ``timeout_ms=0`` like the other scan handlers.
"""
from __future__ import annotations

import os
from typing import Any

from ...tools._osm_tools.tag_query import query_region

NAMESPACE = "osm.query"


def _log(params: dict[str, Any]):
    sl = params.get("_step_log")
    return (lambda m: sl(m, level="info")) if sl else (lambda m: None)


def handle_tag_query(params: dict[str, Any]) -> dict[str, Any]:
    region = (params.get("region") or "").strip()
    expression = (params.get("filter") or "").strip()
    log = _log(params)
    log(f"local tag query on {region}: {expression}")
    res = query_region(
        region,
        expression,
        force=bool(params.get("force")),
        osmium_bin=os.environ.get("FW_OSMIUM_BIN", "osmium"),
    )
    log(
        f"{res.feature_count} feature(s) "
        + (f"from cache ({res.digest})" if res.was_cached
           else f"in {res.duration_seconds:.1f}s ({res.digest})")
    )
    return {
        "path": res.path,
        "feature_count": res.feature_count,
        "digest": res.digest,
        "expression": res.expression,
        "size_bytes": res.size_bytes,
        "duration_seconds": res.duration_seconds,
        "was_cached": res.was_cached,
    }


_DISPATCH = {
    f"{NAMESPACE}.TagQuery": handle_tag_query,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner. A planet-wide scan runs for tens of
    minutes, so timeout_ms=0 (rely on the runner's global execution timeout)."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_query_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
