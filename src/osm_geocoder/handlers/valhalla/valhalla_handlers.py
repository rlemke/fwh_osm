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

"""Valhalla routing tileset handlers.

Thin adapters over ``tools/_lib/valhalla_build.py``. The real build
logic (subprocess invocation, config JSON generation, manifest-based
cache, per-region locking, finalize-from-local staging) lives in the
library so the CLI (``build-valhalla-tiles``) and these handlers share
one code path and one cache layout at
``$AFL_DATA_ROOT/cache/osm/valhalla/<region>-latest/``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..shared.pbf_convert import valhalla

# Duplicated locally to avoid an import cycle with operations_handlers.
_GEOFABRIK_REGION_RE = re.compile(
    r"https?://download\.geofabrik\.de/(.+)-latest\.[^/]+$"
)


def _extract_region_path(url: str) -> str:
    m = _GEOFABRIK_REGION_RE.match(url)
    if m:
        return m.group(1)
    return url


def build_tiles_handler(payload: dict) -> dict:
    """BuildTiles: build or fetch a cached Valhalla tileset."""
    cache = payload.get("cache", {}) or {}
    recreate = bool(payload.get("recreate", False))
    step_log = payload.get("_step_log")

    region = _extract_region_path(cache.get("url", ""))
    if step_log:
        step_log(f"BuildTiles: Valhalla tileset for {region}")
    try:
        result = valhalla.build_tiles(region, force=recreate)
    except valhalla.BuildError as exc:
        raise RuntimeError(str(exc)) from exc
    out = valhalla.to_valhalla_cache(result)
    if step_log:
        status = "cache" if result.was_cached else "built"
        step_log(
            f"BuildTiles: {region} {status} "
            f"({out['tileCount']} tiles, {out['size']} bytes)",
            level="success",
        )
    return {"tiles": out}


def validate_tiles_handler(payload: dict) -> dict:
    """ValidateTiles: confirm a built tileset still has at least one .gph file."""
    tiles = payload.get("tiles", {}) or {}
    tile_dir = tiles.get("tileDir", "")
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"ValidateTiles: {tile_dir}")
    if not tile_dir:
        return {"valid": False, "tileCount": 0}
    count, _levels = valhalla._count_tiles(Path(tile_dir))
    valid = count > 0
    if step_log:
        step_log(
            f"ValidateTiles: valid={valid} ({count} tiles)",
            level="success",
        )
    return {"valid": valid, "tileCount": count}


def clean_tiles_handler(payload: dict) -> dict:
    """CleanTiles: delete a built tileset directory."""
    tiles = payload.get("tiles", {}) or {}
    tile_dir = tiles.get("tileDir", "")
    step_log = payload.get("_step_log")
    if step_log:
        step_log(f"CleanTiles: {tile_dir}")
    if not tile_dir:
        if step_log:
            step_log("CleanTiles: no tileDir supplied", level="success")
        return {"deleted": False}
    deleted = valhalla.clean_tiles(tile_dir)
    if step_log:
        step_log(
            f"CleanTiles: {'deleted' if deleted else 'no tileset found'} at {tile_dir}",
            level="success",
        )
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

VALHALLA_OPERATIONS_HANDLERS: dict[str, callable] = {
    "osm.ops.Valhalla.BuildTiles": build_tiles_handler,
    "osm.ops.Valhalla.BuildTilesBatch": build_tiles_handler,
    "osm.ops.Valhalla.ValidateTiles": validate_tiles_handler,
    "osm.ops.Valhalla.CleanTiles": clean_tiles_handler,
}


def register_valhalla_handlers(poller) -> int:
    """Register all Valhalla operation handlers with the poller."""
    count = 0
    for name, handler in VALHALLA_OPERATIONS_HANDLERS.items():
        poller.register(name, handler)
        count += 1
    return count


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for name, handler in VALHALLA_OPERATIONS_HANDLERS.items():
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
