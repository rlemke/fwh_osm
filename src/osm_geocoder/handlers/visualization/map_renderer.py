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
from facetwork.runtime.storage import localize

from ..shared._output import resolve_local_output_dir, uri_stem

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
        output_path = os.path.join(
            resolve_local_output_dir("maps"),
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
    m.save(str(output_path))

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

    m.save(str(output_path))

    return MapResult(
        output_path=str(output_path),
        format="html",
        feature_count=total_features,
        bounds=bounds_to_string(combined_bounds),
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
