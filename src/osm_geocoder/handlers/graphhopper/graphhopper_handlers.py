# Copyright 2025 Ralph Lemke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GraphHopper routing graph handlers.

Thin adapters over ``tools/_lib/graphhopper_build.py``. The real logic
(subprocess invocation, config YAML, manifest-based cache, per-
(region, profile) locking, finalize-from-local staging) lives in the
library so the CLI tool (``build-graphhopper-graph``) and these
handlers share one code path and one cache layout at
``$AFL_DATA_ROOT/cache/osm/graphhopper/<region>-latest/<profile>/``.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..shared.pbf_convert import graphhopper


# Reuse the handler-side Geofabrik-URL → region-path parser. Duplicated
# deliberately to avoid an import cycle with operations_handlers.
_GEOFABRIK_REGION_RE = re.compile(
    r"https?://download\.geofabrik\.de/(.+)-latest\.[^/]+$"
)


def _extract_region_path(url: str) -> str:
    """Strip a Geofabrik download URL to its region path.

    E.g. ``https://download.geofabrik.de/africa/algeria-latest.osm.pbf``
    returns ``africa/algeria``. Falls back to the raw URL if the pattern
    doesn't match — the library will surface a clear "no pbf manifest
    entry" error for that case.
    """
    m = _GEOFABRIK_REGION_RE.match(url)
    if m:
        return m.group(1)
    return url


def _region_and_profile(payload: dict) -> tuple[str, str]:
    cache = payload.get("cache", {}) or {}
    region = _extract_region_path(cache.get("url", ""))
    profile = payload.get("profile") or "car"
    return region, profile


def build_graph_handler(payload: dict) -> dict:
    """BuildGraph: build or fetch a cached GraphHopper routing graph."""
    region, profile = _region_and_profile(payload)
    recreate = bool(payload.get("recreate", False))
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"BuildGraph: {profile} graph for {region}")
    try:
        result = graphhopper.build_graph(region, profile, force=recreate)
    except graphhopper.BuildError as exc:
        raise RuntimeError(str(exc)) from exc
    cache_dict = graphhopper.to_graphhopper_cache(result)
    if step_log:
        status = "cache" if result.was_cached else "built"
        step_log(
            f"BuildGraph: {profile} graph for {region} {status} "
            f"({cache_dict['nodeCount']} nodes, {cache_dict['edgeCount']} edges)",
            level="success",
        )
    return {"graph": cache_dict}


def build_multi_profile_handler(payload: dict) -> dict:
    """BuildMultiProfile: build graphs for every profile in ``profiles``."""
    cache = payload.get("cache", {}) or {}
    profiles = payload.get("profiles") or ["car"]
    recreate = bool(payload.get("recreate", False))
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"BuildMultiProfile: profiles={profiles}")

    graphs: list[dict[str, Any]] = []
    for profile in profiles:
        one = build_graph_handler(
            {
                "cache": cache,
                "profile": profile,
                "recreate": recreate,
                "_step_log": step_log,
            }
        )
        graphs.append(one["graph"])
    if step_log:
        step_log(
            f"BuildMultiProfile: built {len(graphs)} profile graph(s)",
            level="success",
        )
    return {"graphs": graphs}


def import_graph_handler(payload: dict) -> dict:
    """ImportGraph: same behavior as BuildGraph (build if not found)."""
    return build_graph_handler(payload)


def validate_graph_handler(payload: dict) -> dict:
    """ValidateGraph: read node/edge counts from the built graph dir."""
    graph = payload.get("graph", {}) or {}
    graph_dir = graph.get("graphDir", "")
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"ValidateGraph: {graph_dir}")
    if not graph_dir:
        return {"valid": False, "nodeCount": 0, "edgeCount": 0}
    from pathlib import Path as _Path

    stats = graphhopper.read_graph_stats(_Path(graph_dir))
    if step_log:
        step_log(
            f"ValidateGraph: valid={stats.valid} "
            f"({stats.node_count} nodes, {stats.edge_count} edges)",
            level="success",
        )
    return {
        "valid": stats.valid,
        "nodeCount": stats.node_count,
        "edgeCount": stats.edge_count,
    }


def clean_graph_handler(payload: dict) -> dict:
    """CleanGraph: delete a built graph directory."""
    graph = payload.get("graph", {}) or {}
    graph_dir = graph.get("graphDir", "")
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"CleanGraph: {graph_dir}")
    if not graph_dir:
        if step_log:
            step_log("CleanGraph: no graphDir supplied", level="success")
        return {"deleted": False}
    deleted = graphhopper.clean_graph(graph_dir)
    if step_log:
        step_log(
            f"CleanGraph: {'deleted' if deleted else 'no graph found'} at {graph_dir}",
            level="success",
        )
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

GRAPHHOPPER_OPERATIONS_HANDLERS: dict[str, callable] = {
    "osm.ops.GraphHopper.BuildGraph": build_graph_handler,
    "osm.ops.GraphHopper.BuildMultiProfile": build_multi_profile_handler,
    "osm.ops.GraphHopper.BuildGraphBatch": build_graph_handler,
    "osm.ops.GraphHopper.ImportGraph": import_graph_handler,
    "osm.ops.GraphHopper.ValidateGraph": validate_graph_handler,
    "osm.ops.GraphHopper.CleanGraph": clean_graph_handler,
}


def register_graphhopper_handlers(poller) -> int:
    """Register all GraphHopper operation handlers with the poller.

    Returns the number of handlers registered.
    """
    count = 0
    for name, handler in GRAPHHOPPER_OPERATIONS_HANDLERS.items():
        poller.register(name, handler)
        count += 1
    return count


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for name, handler in GRAPHHOPPER_OPERATIONS_HANDLERS.items():
        _DISPATCH[name] = handler


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
