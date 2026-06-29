"""GeoJSON map rendering with OSM backgrounds.

Generates interactive HTML maps using Folium (Leaflet.js) with OpenStreetMap tiles,
or static PNG images using contextily + matplotlib.
"""

import json
import logging
import os
import posixpath
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from facetwork.config import get_temp_dir
from facetwork.runtime.storage import get_storage_backend, localize

from ..shared._output import (
    finalize_output_file,
    resolve_local_output_dir,
    resolve_output_dir,
    uri_stem,
)


def _save_map_html(folium_map, output_path: str) -> None:
    """Save a folium map to ``output_path`` (storage-aware).

    folium can only write to the local filesystem, so for remote outputs
    (``s3://``, ``hdfs://``) we save to a local temp then finalize onto the
    backend — the map output is shareable on the object store like any other
    artifact."""
    if not output_path.startswith(("s3://", "hdfs://")):
        folium_map.save(output_path)
        return
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    folium_map.save(tmp)
    finalize_output_file(tmp, output_path)

log = logging.getLogger(__name__)

# Check for folium availability
try:
    import folium
    from folium.plugins import Fullscreen, MeasureControl

    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# Check for static image generation (optional)
try:
    import contextily as ctx
    import geopandas as gpd
    import matplotlib.pyplot as plt

    HAS_STATIC = True
except ImportError:
    HAS_STATIC = False


@dataclass
class LayerStyle:
    """Style configuration for a GeoJSON layer."""

    color: str = "#3388ff"  # Stroke color
    fill_color: str | None = None  # Fill color (default: same as color)
    weight: int = 2  # Stroke width
    opacity: float = 1.0  # Stroke opacity
    fill_opacity: float = 0.4  # Fill opacity

    def to_folium_style(self) -> dict:
        """Convert to Folium style function arguments."""
        return {
            "color": self.color,
            "fillColor": self.fill_color or self.color,
            "weight": self.weight,
            "opacity": self.opacity,
            "fillOpacity": self.fill_opacity,
        }


@dataclass
class MapResult:
    """Result of a map rendering operation."""

    output_path: str
    format: str
    feature_count: int
    bounds: str  # "minLon,minLat,maxLon,maxLat"
    title: str
    extraction_date: str = ""


def calculate_bounds(geojson: dict) -> tuple[float, float, float, float] | None:
    """Calculate bounding box from GeoJSON features.

    Returns:
        Tuple of (min_lon, min_lat, max_lon, max_lat) or None if no features
    """
    features = geojson.get("features", [])
    if not features:
        return None

    min_lon, min_lat = float("inf"), float("inf")
    max_lon, max_lat = float("-inf"), float("-inf")

    def update_bounds(coords):
        nonlocal min_lon, min_lat, max_lon, max_lat
        if isinstance(coords[0], (int, float)):
            # Single coordinate pair [lon, lat]
            lon, lat = coords[0], coords[1]
            min_lon = min(min_lon, lon)
            max_lon = max(max_lon, lon)
            min_lat = min(min_lat, lat)
            max_lat = max(max_lat, lat)
        else:
            # Nested coordinates
            for coord in coords:
                update_bounds(coord)

    for feature in features:
        geometry = feature.get("geometry")
        if geometry and geometry.get("coordinates"):
            update_bounds(geometry["coordinates"])

    if min_lon == float("inf"):
        return None

    return (min_lon, min_lat, max_lon, max_lat)


def bounds_to_string(bounds: tuple[float, float, float, float] | None) -> str:
    """Convert bounds tuple to string format."""
    if bounds is None:
        return ""
    return f"{bounds[0]:.6f},{bounds[1]:.6f},{bounds[2]:.6f},{bounds[3]:.6f}"


def calculate_center(bounds: tuple[float, float, float, float] | None) -> tuple[float, float]:
    """Calculate center point from bounds.

    Returns:
        Tuple of (lat, lon) - note: lat/lon order for Folium
    """
    if bounds is None:
        # Default to center of US
        return (39.8283, -98.5795)

    min_lon, min_lat, max_lon, max_lat = bounds
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    return (center_lat, center_lon)


def calculate_zoom(bounds: tuple[float, float, float, float] | None) -> int:
    """Estimate appropriate zoom level from bounds."""
    if bounds is None:
        return 4

    min_lon, min_lat, max_lon, max_lat = bounds
    lat_diff = max_lat - min_lat
    lon_diff = max_lon - min_lon
    max_diff = max(lat_diff, lon_diff)

    # Rough zoom level estimation
    if max_diff > 100:
        return 2
    elif max_diff > 50:
        return 3
    elif max_diff > 20:
        return 4
    elif max_diff > 10:
        return 5
    elif max_diff > 5:
        return 6
    elif max_diff > 2:
        return 7
    elif max_diff > 1:
        return 8
    elif max_diff > 0.5:
        return 9
    elif max_diff > 0.2:
        return 10
    elif max_diff > 0.1:
        return 11
    elif max_diff > 0.05:
        return 12
    else:
        return 13


def render_map_html(
    geojson_path: str | Path,
    output_path: str | Path | None = None,
    title: str = "Map",
    style: LayerStyle | None = None,
    center: tuple[float, float] | None = None,
    zoom: int | None = None,
    width: int = 800,
    height: int = 600,
    run_id: str | None = None,
) -> MapResult:
    """Render a GeoJSON file as an interactive HTML map.

    Args:
        geojson_path: Path to input GeoJSON file
        output_path: Path to output HTML file (default: same name with .html)
        title: Map title
        style: Layer style configuration
        center: Map center as (lat, lon), or None to auto-calculate
        zoom: Zoom level, or None to auto-calculate
        width: Map width in pixels (for embedding)
        height: Map height in pixels (for embedding)

    Returns:
        MapResult with output path and metadata
    """
    if not HAS_FOLIUM:
        raise RuntimeError(
            "folium is required for HTML map rendering. Install with: pip install folium"
        )

    geojson_path_str = str(geojson_path)
    local_geojson = localize(geojson_path_str)
    if output_path is None:
        # The map name inherits the (already param-addressed) input GeoJSON stem,
        # so distinct queries don't collide. When FW_OUTPUT_PER_RUN is set and a
        # run_id is supplied, isolate the map under maps/runs/<run_id>/.
        from ..shared._output import _per_run_enabled

        parts = ["maps"]
        if run_id and _per_run_enabled():
            parts += ["runs", run_id]
        output_path = os.path.join(
            resolve_local_output_dir(*parts),
            uri_stem(geojson_path_str) + ".html",
        )
    output_path = str(output_path)

    if style is None:
        style = LayerStyle()

    # Load GeoJSON
    with open(local_geojson, encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    bounds = calculate_bounds(geojson)

    # Calculate center and zoom if not provided
    if center is None:
        center = calculate_center(bounds)
    if zoom is None:
        zoom = calculate_zoom(bounds)

    # Create map
    m = folium.Map(
        location=center,
        zoom_start=zoom,
        tiles="OpenStreetMap",
    )

    # Add GeoJSON layer with style
    def style_function(feature):
        return style.to_folium_style()

    def highlight_function(feature):
        return {
            "weight": style.weight + 2,
            "fillOpacity": min(style.fill_opacity + 0.2, 1.0),
        }

    # Create popup content from properties
    def popup_content(feature):
        props = feature.get("properties", {})
        if not props:
            return None
        lines = [f"<b>{k}:</b> {v}" for k, v in props.items() if v is not None]
        return "<br>".join(lines) if lines else None

    geojson_layer = folium.GeoJson(
        geojson,
        name=title,
        style_function=style_function,
        highlight_function=highlight_function,
        tooltip=folium.GeoJsonTooltip(
            fields=list(features[0].get("properties", {}).keys())[:5] if features else [],
            aliases=list(features[0].get("properties", {}).keys())[:5] if features else [],
            localize=True,
        )
        if features and features[0].get("properties")
        else None,
    )
    geojson_layer.add_to(m)

    # Add controls
    Fullscreen().add_to(m)
    MeasureControl(position="bottomleft").add_to(m)
    folium.LayerControl().add_to(m)

    # Fit bounds if available
    if bounds:
        m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

    # Add title
    title_html = f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index: 1000;
                background-color: white; padding: 10px; border-radius: 5px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.3); font-family: Arial, sans-serif;">
        <h3 style="margin: 0;">{title}</h3>
        <small>{len(features)} features</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    # Save map
    _save_map_html(m, str(output_path))

    return MapResult(
        output_path=str(output_path),
        format="html",
        feature_count=len(features),
        bounds=bounds_to_string(bounds),
        title=title,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def render_map_png(
    geojson_path: str | Path,
    output_path: str | Path | None = None,
    title: str = "Map",
    style: LayerStyle | None = None,
    width: int = 800,
    height: int = 600,
    dpi: int = 100,
) -> MapResult:
    """Render a GeoJSON file as a static PNG image.

    Args:
        geojson_path: Path to input GeoJSON file
        output_path: Path to output PNG file
        title: Map title
        style: Layer style configuration
        width: Image width in pixels
        height: Image height in pixels
        dpi: Image resolution

    Returns:
        MapResult with output path and metadata
    """
    if not HAS_STATIC:
        raise RuntimeError(
            "geopandas and contextily are required for PNG rendering. "
            "Install with: pip install geopandas contextily matplotlib"
        )

    geojson_path_str = str(geojson_path)
    local_geojson = localize(geojson_path_str)
    if output_path is None:
        output_path = os.path.join(
            resolve_local_output_dir("maps"),
            uri_stem(geojson_path_str) + ".png",
        )
    output_path = str(output_path)

    if style is None:
        style = LayerStyle()

    # Load GeoJSON with geopandas
    gdf = gpd.read_file(local_geojson)

    # Ensure WGS84 then convert to Web Mercator for contextily
    if gdf.crs is None:
        gdf.set_crs("EPSG:4326", inplace=True)
    gdf_mercator = gdf.to_crs("EPSG:3857")

    # Calculate bounds in original CRS
    bounds = tuple(gdf.total_bounds)  # minx, miny, maxx, maxy

    # Create figure
    fig, ax = plt.subplots(figsize=(width / dpi, height / dpi), dpi=dpi)

    # Plot GeoJSON
    gdf_mercator.plot(
        ax=ax,
        color=style.fill_color or style.color,
        edgecolor=style.color,
        linewidth=style.weight,
        alpha=style.fill_opacity,
    )

    # Add OSM basemap
    ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik)

    # Add title
    ax.set_title(title, fontsize=14, fontweight="bold")
    ax.set_axis_off()

    # Save
    plt.tight_layout()
    plt.savefig(output_path, dpi=dpi, bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)

    return MapResult(
        output_path=str(output_path),
        format="png",
        feature_count=len(gdf),
        bounds=bounds_to_string(bounds),
        title=title,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def render_map(
    geojson_path: str | Path,
    output_path: str | Path | None = None,
    title: str = "Map",
    format: str = "html",
    style: LayerStyle | None = None,
    **kwargs,
) -> MapResult:
    """Render a GeoJSON file as a map.

    Args:
        geojson_path: Path to input GeoJSON file
        output_path: Path to output file
        title: Map title
        format: Output format ("html" or "png")
        style: Layer style configuration
        **kwargs: Additional arguments for the specific renderer

    Returns:
        MapResult with output path and metadata
    """
    if format.lower() == "html":
        return render_map_html(geojson_path, output_path, title, style, **kwargs)
    elif format.lower() == "png":
        # render_map_png does not take run_id; drop it if present.
        kwargs.pop("run_id", None)
        return render_map_png(geojson_path, output_path, title, style, **kwargs)
    else:
        raise ValueError(f"Unknown format: {format}. Use 'html' or 'png'.")


def render_layers(
    layer_paths: list[str | Path],
    colors: list[str] | None = None,
    output_path: str | Path | None = None,
    title: str = "Map",
    format: str = "html",
) -> MapResult:
    """Render multiple GeoJSON files as layers on a single map.

    Args:
        layer_paths: List of paths to GeoJSON files
        colors: List of colors for each layer (default: auto-generated)
        output_path: Path to output file
        title: Map title
        format: Output format ("html" or "png")

    Returns:
        MapResult with output path and metadata
    """
    if not HAS_FOLIUM:
        raise RuntimeError("folium is required for map rendering")

    if not layer_paths:
        raise ValueError("At least one layer path is required")

    # Default colors
    default_colors = [
        "#3388ff",
        "#ff3333",
        "#33ff33",
        "#ffff33",
        "#ff33ff",
        "#33ffff",
        "#ff8800",
        "#8800ff",
    ]
    if colors is None:
        colors = default_colors
    while len(colors) < len(layer_paths):
        colors = colors + default_colors

    # Determine output path
    if output_path is None:
        output_path = os.path.join(
            resolve_local_output_dir("maps"),
            uri_stem(str(layer_paths[0])) + "_layers.html",
        )
    output_path = str(output_path)

    # Load all GeoJSON files and calculate combined bounds
    all_bounds = []
    total_features = 0
    layers_data = []

    for path in layer_paths:
        local_path = localize(str(path))
        with open(local_path, encoding="utf-8") as f:
            geojson = json.load(f)
        features = geojson.get("features", [])
        total_features += len(features)
        bounds = calculate_bounds(geojson)
        if bounds:
            all_bounds.append(bounds)
        layers_data.append((uri_stem(str(path)), geojson))

    # Calculate combined bounds
    if all_bounds:
        combined_bounds = (
            min(b[0] for b in all_bounds),
            min(b[1] for b in all_bounds),
            max(b[2] for b in all_bounds),
            max(b[3] for b in all_bounds),
        )
    else:
        combined_bounds = None

    center = calculate_center(combined_bounds)
    zoom = calculate_zoom(combined_bounds)

    # Create map
    m = folium.Map(location=center, zoom_start=zoom, tiles="OpenStreetMap")

    # Add each layer
    for i, (name, geojson) in enumerate(layers_data):
        color = colors[i]
        style = LayerStyle(color=color, fill_color=color)

        folium.GeoJson(
            geojson,
            name=name,
            style_function=lambda x, s=style: s.to_folium_style(),
        ).add_to(m)

    # Add controls
    Fullscreen().add_to(m)
    folium.LayerControl().add_to(m)

    # Fit bounds
    if combined_bounds:
        m.fit_bounds(
            [[combined_bounds[1], combined_bounds[0]], [combined_bounds[3], combined_bounds[2]]]
        )

    # Add title
    title_html = f"""
    <div style="position: fixed; top: 10px; left: 50px; z-index: 1000;
                background-color: white; padding: 10px; border-radius: 5px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.3); font-family: Arial, sans-serif;">
        <h3 style="margin: 0;">{title}</h3>
        <small>{len(layer_paths)} layers, {total_features} features</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    _save_map_html(m, str(output_path))

    return MapResult(
        output_path=str(output_path),
        format="html",
        feature_count=total_features,
        bounds=bounds_to_string(combined_bounds),
        title=title,
        extraction_date=datetime.now(UTC).isoformat(),
    )


_TILED_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>__TITLE__</title>
<link href='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css' rel='stylesheet'>
<script src='https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js' crossorigin='anonymous'></script>
<script src='https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js' crossorigin='anonymous'></script>
<style>html,body{margin:0;height:100%} #map{position:absolute;top:0;bottom:0;left:0;right:0}
#title{position:absolute;top:10px;left:50px;z-index:1;background:#fff;padding:8px 12px;border-radius:6px;
       box-shadow:0 2px 5px rgba(0,0,0,.3);font-family:Arial,sans-serif}
#title h3{margin:0;font-size:16px} #title small{color:#666}
#legend{position:absolute;bottom:14px;right:14px;z-index:1;background:rgba(20,20,22,.82);color:#eee;
        padding:8px 11px;border-radius:6px;box-shadow:0 2px 6px rgba(0,0,0,.4);font-family:Arial,sans-serif;
        font-size:12px;line-height:1.55}
#legend b{display:block;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#aaa;margin-bottom:4px}
#legend .row{display:flex;align-items:center;gap:7px}
#legend .sw{width:13px;height:13px;border-radius:3px;border:1px solid rgba(255,255,255,.35);flex:none}
#err{position:absolute;bottom:10px;left:10px;right:10px;z-index:2;background:#fee;color:#900;padding:8px;
     border:1px solid #c66;border-radius:4px;font-family:monospace;font-size:12px;display:none;white-space:pre-wrap}</style>
</head><body>
<div id='title'><h3>__TITLE__</h3><small>vector tiles · zoom-tiled (only the current viewport is fetched) · <a href='https://protomaps.com/' target='_blank'>PMTiles</a> + <a href='https://maplibre.org/' target='_blank'>MapLibre</a></small></div>
<div id='legend'>__LEGEND__</div>
<div id='map'></div>
<div id='err'></div>
<script>
const eb=document.getElementById('err');
function showErr(m){eb.style.display='block';eb.textContent+=m+"\\n"}
window.addEventListener('error',e=>showErr('JS error: '+(e.error&&e.error.stack||e.message)));
window.addEventListener('unhandledrejection',e=>showErr('promise: '+(e.reason&&e.reason.stack||e.reason)));
if(typeof maplibregl==='undefined') showErr('maplibregl global missing — CDN load failed');
if(typeof pmtiles==='undefined') showErr('pmtiles global missing — CDN load failed');
const here=location.origin+location.pathname.replace(/\\/[^/]*$/,'/');
const protocol=new pmtiles.Protocol(); maplibregl.addProtocol('pmtiles', protocol.tile);
__PRE_REGISTER__
const map=new maplibregl.Map({
  container:'map',
  style:{
    version:8,
    glyphs:'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf',
    sources:Object.assign({__BG_SOURCE__}, __SOURCES__),
    layers:[__BG_LAYER__].concat(__LAYERS__)
  },
  center:[__CENTER_LON__,__CENTER_LAT__], zoom:__ZOOM__
});
map.on('error',e=>showErr('map error: '+(e.error&&e.error.message||JSON.stringify(e))));
map.addControl(new maplibregl.NavigationControl());
map.addControl(new maplibregl.ScaleControl());
</script>
</body></html>
"""


def _infer_layer_kind(layer_name: str) -> str:
    """Guess Mapbox-style 'type' for a vector-tile layer name."""
    n = layer_name.lower()
    if any(k in n for k in ("point", "node", "city", "cities", "place", "marker", "stop", "amenit")):
        return "circle"
    return "line"


def _default_palette(i: int) -> str:
    palette = ["#1f78b4", "#e31a1c", "#33a02c", "#ff7f00", "#6a3d9a", "#b15928"]
    return palette[i % len(palette)]


# Basemap backdrops. Each entry → (theme, JS source fragment, JS layer fragment).
# `theme` drives foreground contrast (label/stroke/casing colours) so dots and
# routes read on whichever backdrop is chosen. "none" emits no tile source, just
# a flat dark background, so the map works offline. CARTO tiles round-robin over
# a,b,c,d subdomains — MapLibre GL does NOT expand a Leaflet-style `{s}`, so the
# subdomains are listed explicitly in the tiles array.
def _carto_tiles(style: str) -> str:
    urls = ",".join(
        f"'https://{s}.basemaps.cartocdn.com/{style}/{{z}}/{{x}}/{{y}}.png'"
        for s in "abcd"
    )
    return f"bg:{{type:'raster',tiles:[{urls}],tileSize:256,attribution:'© OpenStreetMap © CARTO'}}"


_BASEMAPS = {
    "dark": ("dark", _carto_tiles("dark_nolabels"), "{id:'bg',type:'raster',source:'bg'}"),
    "light": ("light", _carto_tiles("light_nolabels"), "{id:'bg',type:'raster',source:'bg'}"),
    "osm": ("light",
            "bg:{type:'raster',tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,attribution:'© OpenStreetMap'}",
            "{id:'bg',type:'raster',source:'bg'}"),
    "none": ("dark", "", "{id:'bg',type:'background',paint:{'background-color':'#0a0a0c'}}"),
}


def render_tiled_map(
    tile_paths: list[str | Path],
    layer_names: list[str] | None = None,
    colors: list[str] | None = None,
    title: str = "Tiled map",
    output_path: str | Path | None = None,
    center_lon: float = 0.0,
    center_lat: float = 20.0,
    zoom: float = 2.0,
    basemap: str = "dark",
) -> MapResult:
    """Render a MapLibre + PMTiles viewer page for a set of vector-tile layers.

    The companion to :func:`render_layers` (folium, single-HTML): produces a
    zoom-tiled viewer where the browser fetches *only the tiles intersecting
    the current viewport at the current zoom*, via HTTP Range requests against
    the PMTiles archives. As you zoom in you get more detail (deeper tiles)
    AND less data (smaller viewport = fewer tiles).

    The output is a **directory**: an ``index.html`` plus a relative reference
    (symlink, falling back to a copy) to each input PMTiles archive. **The
    directory must be served over HTTP with Range support** — PMTiles relies on
    HTTP Range requests, and stdlib ``python -m http.server`` does NOT honor
    them (it returns ``200 OK`` with the full file, silently breaking the
    viewer). Use ``scripts/serve-tiled-map`` shipped with this package, or any
    Range-capable static server (nginx, caddy, ``npx http-server``). ``file://``
    is also unreliable across browsers.

    Args:
        tile_paths: PMTiles archive paths (one per layer).
        layer_names: layer name *inside* each PMTiles (the ``-l`` value
            passed to tippecanoe in BuildVectorTiles). If empty/short, the
            file stem is used as a fallback.
        colors: per-layer color; defaults to a small palette.
        title: page title.
        center_lon, center_lat, zoom: initial view.
        basemap: backdrop — ``"dark"`` (default), ``"light"``, ``"osm"`` or
            ``"none"``. A muted/dark backdrop keeps the thematic dots and routes
            legible; the full-colour ``"osm"`` raster competes with them.
    """
    import shutil

    theme, bg_source, bg_layer = _BASEMAPS.get(basemap, _BASEMAPS["dark"])

    # PMTiles archives may live on the object store (s3://) / HDFS when storage is
    # remote — BuildVectorTiles writes there so any runner can read them. Localize
    # each to a real local file first: the viewer dir is assembled with local
    # symlinks/copies and the index.html is built against on-disk archives.
    tile_paths = [
        localize(str(p)) if str(p).startswith(("s3://", "hdfs://")) else str(p)
        for p in tile_paths
    ]
    tile_paths = [Path(p) for p in tile_paths]
    if not tile_paths:
        raise ValueError("render_tiled_map: no tile paths provided")
    for p in tile_paths:
        if not p.exists():
            raise FileNotFoundError(f"PMTiles missing: {p}")

    # Output dir: a per-render subfolder under the local maps dir, named for the
    # first layer's stem (deterministic + human-readable).
    if output_path is None:
        base = resolve_local_output_dir("maps", "tiled")
        out_dir = Path(base) / tile_paths[0].stem
    else:
        out_dir = Path(str(output_path))
    out_dir.mkdir(parents=True, exist_ok=True)

    # Place each PMTiles next to index.html (symlink, fall back to copy).
    sources = []
    for i, p in enumerate(tile_paths):
        dst = out_dir / p.name
        if dst.resolve() != p.resolve():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            try:
                dst.symlink_to(p.resolve())
            except OSError:
                shutil.copy2(p, dst)
        layer_name = (layer_names[i] if layer_names and i < len(layer_names) and layer_names[i] else p.stem)
        sources.append({
            "id": f"layer{i}",
            "file": p.name,
            "layer": layer_name,
            "color": (colors[i] if colors and i < len(colors) and colors[i] else _default_palette(i)),
            "kind": _infer_layer_kind(layer_name),
        })

    # Build the JS substitutions. Sources use absolute URLs assembled at runtime
    # from JS `here` (the page's own origin+path) — relative `./` URLs are
    # unreliable with pmtiles 3.x. Each PMTiles instance is pre-registered with
    # the protocol so MapLibre and the protocol share one instance.
    sources_obj = ", ".join(
        f"{s['id']}:{{type:'vector', url:'pmtiles://' + here + '{s['file']}'}}" for s in sources
    )
    pre_register = "\n".join(
        f"protocol.add(new pmtiles.PMTiles(here + '{s['file']}'));" for s in sources
    )

    # Foreground contrast adapts to the backdrop so dots/routes/labels read on
    # either dark or light. Dot strokes, line casings and label haloes flip; the
    # band colours themselves are kept as authored.
    if theme == "dark":
        dot_stroke, text_color, text_halo, casing = "#0d0d0f", "#f2f2f2", "#000", "#000"
        casing_opacity = 0.55
    else:
        dot_stroke, text_color, text_halo, casing = "#ffffff", "#1a1a1a", "#fff", "#fff"
        casing_opacity = 0.7

    def _layer_block(s: dict) -> str:
        if s["kind"] == "circle":
            # Radius grows with zoom so megacity dots stay prominent low and
            # smaller cities don't bloat when you zoom in.
            radius = "['interpolate',['linear'],['zoom'],3,3.5,6,5,10,7.5]"
            return (f"{{id:'{s['id']}-dot',type:'circle',source:'{s['id']}','source-layer':'{s['layer']}',"
                    f"paint:{{'circle-color':'{s['color']}','circle-radius':{radius},"
                    f"'circle-stroke-color':'{dot_stroke}','circle-stroke-width':1.4}}}},"
                    # Label sits immediately right of the dot, anchored on the dot's
                    # vertical centre. No minzoom — the tile already drops features
                    # outside their per-layer zoom range (set in BuildVectorTiles).
                    f"{{id:'{s['id']}-lbl',type:'symbol',source:'{s['id']}','source-layer':'{s['layer']}',"
                    f"layout:{{'text-field':['get','name'],'text-font':['Noto Sans Regular'],'text-size':11,'text-offset':[0.7,0],'text-anchor':'left','text-allow-overlap':false,'text-ignore-placement':false}},"
                    f"paint:{{'text-color':'{text_color}','text-halo-color':'{text_halo}','text-halo-width':1.3}}}}")
        # Lines get a wider casing underneath (separation from the backdrop and
        # from crossing routes) plus a zoom-interpolated main stroke.
        width = "['interpolate',['linear'],['zoom'],3,1.2,6,2,10,3.2]"
        casing_w = "['interpolate',['linear'],['zoom'],3,2.6,6,3.8,10,5.4]"
        return (f"{{id:'{s['id']}-casing',type:'line',source:'{s['id']}','source-layer':'{s['layer']}',"
                f"layout:{{'line-cap':'round','line-join':'round'}},"
                f"paint:{{'line-color':'{casing}','line-width':{casing_w},'line-opacity':{casing_opacity}}}}},"
                f"{{id:'{s['id']}',type:'line',source:'{s['id']}','source-layer':'{s['layer']}',"
                f"layout:{{'line-cap':'round','line-join':'round'}},"
                f"paint:{{'line-color':'{s['color']}','line-width':{width},'line-opacity':0.9}}}}")
    # Casings/dots for ALL line layers must sit beneath ALL city dots, so emit
    # every line block first, then every circle block.
    line_blocks = [_layer_block(s) for s in sources if s["kind"] == "line"]
    dot_blocks = [_layer_block(s) for s in sources if s["kind"] == "circle"]
    layers_arr = "[" + ",".join(line_blocks + dot_blocks) + "]"

    # Legend: one row per layer (swatch + human label), so it's obvious what the
    # dots and routes are. Layer names like `cities_5M` / `routes_5M` are
    # prettified for display.
    def _pretty(name: str) -> str:
        return name.replace("_", " ").replace("cities", "cities").strip()
    legend_rows = "".join(
        f"<div class='row'><span class='sw' style='background:{s['color']}'></span>{_pretty(s['layer'])}</div>"
        for s in sources
    )
    legend = f"<b>Layers</b>{legend_rows}" if sources else ""

    html = (_TILED_HTML_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__PRE_REGISTER__", pre_register)
            .replace("__BG_SOURCE__", bg_source)
            .replace("__BG_LAYER__", bg_layer)
            .replace("__LEGEND__", legend)
            .replace("__SOURCES__", "{" + sources_obj + "}")
            .replace("__LAYERS__", layers_arr)
            .replace("__CENTER_LON__", repr(float(center_lon)))
            .replace("__CENTER_LAT__", repr(float(center_lat)))
            .replace("__ZOOM__", repr(float(zoom))))

    index = out_dir / "index.html"
    index.write_text(html)

    output_path = str(index)

    # When durable storage is remote (s3://, hdfs://), publish the whole rendered
    # viewer dir — index.html plus every PMTiles archive — to the object store so
    # downstream steps and the publish workflow (which may run on a different
    # runner) can read it. The HTML references each archive by basename relative
    # to its own location, so co-locating them in one remote dir keeps the viewer
    # working when served from the object store or GitHub Pages.
    durable_base = resolve_output_dir("maps")
    if durable_base.startswith(("s3://", "hdfs://")):
        remote_dir = f"{durable_base.rstrip('/')}/tiled/{out_dir.name}"
        backend = get_storage_backend(remote_dir)
        backend.makedirs(remote_dir)
        for f in sorted(out_dir.iterdir()):
            if not f.is_file():
                continue
            dst = f"{remote_dir}/{f.name}"
            with open(f, "rb") as src, backend.open(dst, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        output_path = f"{remote_dir}/index.html"

    return MapResult(
        output_path=output_path,
        format="html-tiled",
        feature_count=0,
        bounds="",
        title=title,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def preview_map(geojson_path: str | Path) -> MapResult:
    """Render a GeoJSON file and open it in the default browser.

    Args:
        geojson_path: Path to input GeoJSON file

    Returns:
        MapResult with output path and metadata
    """
    # Create temp file for preview
    geojson_path_str = str(geojson_path)
    stem = uri_stem(geojson_path_str)
    output_path = os.path.join(get_temp_dir(), f"preview_{stem}.html")

    result = render_map_html(
        geojson_path_str, output_path, title=f"Preview: {posixpath.basename(geojson_path_str)}"
    )

    # Open in browser
    webbrowser.open(f"file://{output_path}")

    return result
