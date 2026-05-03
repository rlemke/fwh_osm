"""Visualization event facet handlers for GeoJSON map rendering.

Handles visualization events defined in osmvisualization.afl under osm.viz.
"""

import logging
import os
from datetime import UTC, datetime

from ..shared.output_cache import cached_result, save_result_meta
from .map_renderer import (
    HAS_FOLIUM,
    HAS_STATIC,
    LayerStyle,
    MapResult,
    preview_map,
    render_layers,
    render_map,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.viz"


def _cache_dict_from_path(path: str) -> dict:
    """Build a cache identity dict from a file path."""
    if not path:
        return {"path": "", "size": 0}
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"path": path, "size": size}


def _cache_dict_from_paths(paths: list[str]) -> dict:
    """Build a cache identity dict from multiple file paths."""
    if not paths:
        return {"path": "", "size": 0}
    combined = ";".join(sorted(paths))
    total_size = 0
    for p in paths:
        try:
            total_size += os.path.getsize(p)
        except OSError:
            pass
    return {"path": combined, "size": total_size}


def _make_render_map_handler(facet_name: str):
    """Create handler for RenderMap event facet."""

    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        geojson_path = payload.get("geojson_path", "")
        title = payload.get("title", "Map")
        format = payload.get("format", "html")
        width = payload.get("width", 800)
        height = payload.get("height", 600)
        color = payload.get("color", "#3388ff")
        fill_opacity = payload.get("fill_opacity", 0.4)
        step_log = payload.get("_step_log")

        cache = _cache_dict_from_path(geojson_path)
        cache_params = {
            "format": format,
            "width": width,
            "height": height,
            "color": color,
            "fill_opacity": fill_opacity,
        }
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: rendering {geojson_path} as {format}")
        log.info("%s rendering %s as %s", facet_name, geojson_path, format)

        if not geojson_path:
            return {"result": _empty_result(title, format)}

        if format == "html" and not HAS_FOLIUM:
            log.error("folium not installed, cannot render HTML map")
            return {"result": _empty_result(title, format)}

        if format == "png" and not HAS_STATIC:
            log.error("geopandas/contextily not installed, cannot render PNG")
            return {"result": _empty_result(title, format)}

        try:
            style = LayerStyle(color=color, fill_opacity=fill_opacity)
            result = render_map(
                geojson_path,
                title=title,
                format=format,
                style=style,
                width=width,
                height=height,
            )
            if step_log:
                step_log(
                    f"{facet_name}: rendered {result.feature_count} features as {format}",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to render map: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to render map: {exc}", level="error")
            raise

    return handler


def _make_render_map_at_handler(facet_name: str):
    """Create handler for RenderMapAt event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        geojson_path = payload.get("geojson_path", "")
        lat = payload.get("lat", 0.0)
        lon = payload.get("lon", 0.0)
        zoom = payload.get("zoom", 10)
        title = payload.get("title", "Map")
        step_log = payload.get("_step_log")

        cache = _cache_dict_from_path(geojson_path)
        cache_params = {"lat": lat, "lon": lon, "zoom": zoom}
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(
                f"{facet_name}: rendering {geojson_path} at ({lat:.4f}, {lon:.4f}) zoom {zoom}"
            )
        log.info(
            "%s rendering %s at (%.4f, %.4f) zoom %d", facet_name, geojson_path, lat, lon, zoom
        )

        if not geojson_path:
            return {"result": _empty_result(title, "html")}

        if not HAS_FOLIUM:
            log.error("folium not installed")
            return {"result": _empty_result(title, "html")}

        try:
            result = render_map(
                geojson_path,
                title=title,
                format="html",
                center=(lat, lon),
                zoom=zoom,
            )
            if step_log:
                step_log(
                    f"{facet_name}: rendered {result.feature_count} features at ({lat:.4f}, {lon:.4f})",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to render map: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to render map: {exc}", level="error")
            raise

    return handler


def _make_render_layers_handler(facet_name: str):
    """Create handler for RenderLayers event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        layers = payload.get("layers", [])
        colors = payload.get("colors", [])
        title = payload.get("title", "Map")
        format = payload.get("format", "html")
        step_log = payload.get("_step_log")

        # Normalize layers to list
        if isinstance(layers, str):
            layers = [layer.strip() for layer in layers.split(",") if layer.strip()]
        if isinstance(colors, str):
            colors = [c.strip() for c in colors.split(",") if c.strip()]

        cache = _cache_dict_from_paths(layers)
        cache_params = {"colors": sorted(colors) if colors else [], "format": format}
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: rendering {len(layers)} layers")
        log.info("%s rendering %d layers", facet_name, len(layers))

        if not layers:
            return {"result": _empty_result(title, format)}

        if not HAS_FOLIUM:
            log.error("folium not installed")
            return {"result": _empty_result(title, format)}

        try:
            result = render_layers(
                layers,
                colors=colors if colors else None,
                title=title,
                format=format,
            )
            if step_log:
                step_log(
                    f"{facet_name}: rendered {result.feature_count} features across {len(layers)} layers",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to render layers: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to render layers: {exc}", level="error")
            raise

    return handler


def _make_render_styled_map_handler(facet_name: str):
    """Create handler for RenderStyledMap event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        geojson_path = payload.get("geojson_path", "")
        style_dict = payload.get("style", {})
        title = payload.get("title", "Map")
        step_log = payload.get("_step_log")

        cache = _cache_dict_from_path(geojson_path)
        cache_params = {
            "color": style_dict.get("color", "#3388ff"),
            "fill_color": style_dict.get("fill_color"),
            "weight": style_dict.get("weight", 2),
            "opacity": style_dict.get("opacity", 1.0),
            "fill_opacity": style_dict.get("fill_opacity", 0.4),
        }
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: rendering {geojson_path} with custom style")
        log.info("%s rendering %s with custom style", facet_name, geojson_path)

        if not geojson_path:
            return {"result": _empty_result(title, "html")}

        if not HAS_FOLIUM:
            log.error("folium not installed")
            return {"result": _empty_result(title, "html")}

        try:
            style = LayerStyle(
                color=cache_params["color"],
                fill_color=cache_params["fill_color"],
                weight=cache_params["weight"],
                opacity=cache_params["opacity"],
                fill_opacity=cache_params["fill_opacity"],
            )
            result = render_map(
                geojson_path,
                title=title,
                format="html",
                style=style,
            )
            if step_log:
                step_log(
                    f"{facet_name}: rendered {result.feature_count} features with custom style",
                    level="success",
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to render styled map: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to render styled map: {exc}", level="error")
            raise

    return handler


def _make_preview_map_handler(facet_name: str):
    """Create handler for PreviewMap event facet."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        geojson_path = payload.get("geojson_path", "")
        step_log = payload.get("_step_log")

        cache = _cache_dict_from_path(geojson_path)
        cache_params: dict = {}
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: previewing {geojson_path}")
        log.info("%s previewing %s", facet_name, geojson_path)

        if not geojson_path:
            return {"result": _empty_result("Preview", "html")}

        if not HAS_FOLIUM:
            log.error("folium not installed")
            return {"result": _empty_result("Preview", "html")}

        try:
            result = preview_map(geojson_path)
            if step_log:
                step_log(
                    f"{facet_name}: previewed {result.feature_count} features", level="success"
                )
            rv = {"result": _result_to_dict(result)}
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to preview map: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to preview map: {exc}", level="error")
            raise

    return handler


def _result_to_dict(result: MapResult) -> dict:
    """Convert a MapResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "format": result.format,
        "feature_count": result.feature_count,
        "bounds": result.bounds,
        "title": result.title,
        "extraction_date": result.extraction_date,
    }


def _empty_result(title: str, format: str) -> dict:
    """Return an empty result dict."""
    return {
        "output_path": "",
        "format": format,
        "feature_count": 0,
        "bounds": "",
        "title": title,
        "extraction_date": datetime.now(UTC).isoformat(),
    }


# Event facet definitions for handler registration
VISUALIZATION_FACETS = [
    ("RenderMap", _make_render_map_handler),
    ("RenderMapAt", _make_render_map_at_handler),
    ("RenderLayers", _make_render_layers_handler),
    ("RenderStyledMap", _make_render_styled_map_handler),
    ("PreviewMap", _make_preview_map_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in VISUALIZATION_FACETS:
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


def register_visualization_handlers(poller) -> None:
    """Register all visualization event facet handlers with the poller."""
    for facet_name, handler_factory in VISUALIZATION_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered visualization handler: %s", qualified_name)
