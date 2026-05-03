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

"""HTML map-page render handlers (MapLibre + PMTiles).

Thin adapters over ``tools/_lib/html_render.py``. The real render
logic (style generation, per-region directory, master index rewrite,
manifest-based cache) lives in the library so the CLI
(``render-html-maps``) and these handlers share one code path and
one cache layout at ``$AFL_DATA_ROOT/cache/osm/html/``.
"""

from __future__ import annotations

import os
import re

from ..shared.pbf_convert import html_render


_GEOFABRIK_REGION_RE = re.compile(
    r"https?://download\.geofabrik\.de/(.+)-latest\.[^/]+$"
)


def _extract_region_path(url: str) -> str:
    m = _GEOFABRIK_REGION_RE.match(url)
    if m:
        return m.group(1)
    return url


def _to_html_map_cache(result: html_render.RenderResult) -> dict:
    """Map a ``RenderResult`` to the ``HTMLMapCache`` FFL schema dict.

    ``indexUrl`` is relative to the serving root (`html/<region>-latest/
    index.html`) — prepend whatever base URL your static server uses
    (typically ``http://localhost:8000/``).
    """
    return {
        "region": result.region,
        "htmlDir": result.html_dir,
        "indexUrl": f"html/{result.relative_path}index.html",
        "sources": list(result.sources),
        "size": int(result.total_size_bytes),
        "date": result.generated_at,
        "wasInCache": bool(result.was_cached),
        "styleVersion": int(result.style_version),
    }


def render_html_map_handler(payload: dict) -> dict:
    """RenderHtmlMap: generate the per-region HTML + style.json.

    Takes ``cache:OSMCache`` (any cache entry whose ``url`` is a
    Geofabrik URL — most commonly the PBF download's OSMCache — so we
    can derive the region). Returns ``htmlMap:HTMLMapCache``.

    After rendering, the master ``html/index.html`` is refreshed so it
    always reflects the latest set of rendered regions.
    """
    cache = payload.get("cache", {}) or {}
    step_log = payload.get("_step_log")
    region = _extract_region_path(cache.get("url", ""))
    if step_log:
        step_log(f"RenderHtmlMap: {region}")
    try:
        result = html_render.render_region(region)
    except html_render.RenderError as exc:
        raise RuntimeError(str(exc)) from exc
    # Keep the master index in sync after every render. Cheap: reads the
    # manifest and writes one small static HTML file.
    html_render.write_master_index()
    out = _to_html_map_cache(result)
    if step_log:
        status = "cache" if result.was_cached else "rendered"
        step_log(
            f"RenderHtmlMap: {region} {status} "
            f"({len(result.sources)} source layer(s), "
            f"{result.total_size_bytes} bytes)",
            level="success",
        )
    return {"htmlMap": out}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

HTML_MAP_HANDLERS: dict[str, callable] = {
    "osm.ops.RenderHtmlMap": render_html_map_handler,
    "osm.ops.RenderHtmlMapBatch": render_html_map_handler,
}


def register_html_map_handlers(poller) -> int:
    """Register HTML map handlers with the poller."""
    count = 0
    for name, handler in HTML_MAP_HANDLERS.items():
        poller.register(name, handler)
        count += 1
    return count


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for name, handler in HTML_MAP_HANDLERS.items():
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
