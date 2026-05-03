# GeoJSON Visualization

Render GeoJSON files as interactive HTML maps or static PNG images with OpenStreetMap backgrounds.

## Features

- **Interactive HTML maps** using Leaflet.js with OSM tiles
- **Static PNG images** for documents/reports
- **Multi-layer support** with custom colors
- **Auto-fit bounds** to data extent
- **Tooltips and popups** from feature properties
- **Fullscreen and measure controls**

## FFL Facets

All facets are defined in `osmvisualization.ffl` under `osm.viz`.

### RenderMap

Render a GeoJSON file as an interactive HTML map or static PNG.

```afl
event facet RenderMap(
    geojson_path: String,
    title: String = "Map",
    format: String = "html",        // "html" or "png"
    width: Long = 800,
    height: Long = 600,
    color: String = "#3388ff",      // Feature color
    fill_opacity: Double = 0.4
) => (result: MapResult)
```

### RenderMapAt

Render a map centered on specific coordinates.

```afl
event facet RenderMapAt(
    geojson_path: String,
    lat: Double,                    // Center latitude
    lon: Double,                    // Center longitude
    zoom: Long = 10,
    title: String = "Map"
) => (result: MapResult)
```

### RenderLayers

Render multiple GeoJSON files as layers with different colors.

```afl
event facet RenderLayers(
    layers: [String],               // List of GeoJSON file paths
    colors: [String],               // List of colors for each layer
    title: String = "Map",
    format: String = "html"
) => (result: MapResult)
```

### RenderStyledMap

Render with full style control.

```afl
event facet RenderStyledMap(
    geojson_path: String,
    style: LayerStyle,
    title: String = "Map"
) => (result: MapResult)
```

### PreviewMap

Quick preview - renders and opens in default browser.

```afl
event facet PreviewMap(
    geojson_path: String
) => (result: MapResult)
```

## Schemas

### MapResult

```afl
schema MapResult {
    output_path: String      // Path to generated file
    format: String           // "html" or "png"
    feature_count: Long      // Number of features rendered
    bounds: String           // "minLon,minLat,maxLon,maxLat"
    title: String
    extraction_date: String
}
```

### LayerStyle

```afl
schema LayerStyle {
    color: String            // Stroke color (default: "#3388ff")
    fill_color: String       // Fill color (default: same as color)
    weight: Long             // Stroke width (default: 2)
    opacity: Double          // Stroke opacity (default: 1.0)
    fill_opacity: Double     // Fill opacity (default: 0.4)
}
```

## Usage Examples

### Basic Map Rendering

```afl
workflow ShowBoundaries(geojson_path: String) => (result: MapResult) andThen {
    map = RenderMap(
        geojson_path = $.geojson_path,
        title = "Administrative Boundaries",
        color = "#ff6600"
    )
    yield ShowBoundaries(result = map.result)
}
```

### Compare Two Datasets

```afl
workflow CompareLayers(file1: String, file2: String) => (result: MapResult) andThen {
    map = RenderLayers(
        layers = [$.file1, $.file2],
        colors = ["#ff0000", "#0000ff"],
        title = "Comparison"
    )
    yield CompareLayers(result = map.result)
}
```

### Centered View

```afl
workflow ShowSanFrancisco(geojson_path: String) => (result: MapResult) andThen {
    map = RenderMapAt(
        geojson_path = $.geojson_path,
        lat = 37.7749,
        lon = -122.4194,
        zoom = 12,
        title = "San Francisco"
    )
    yield ShowSanFrancisco(result = map.result)
}
```

### Static Image for Reports

```afl
workflow ExportPNG(geojson_path: String) => (result: MapResult) andThen {
    map = RenderMap(
        geojson_path = $.geojson_path,
        format = "png",
        width = 1200,
        height = 800,
        title = "District Map"
    )
    yield ExportPNG(result = map.result)
}
```

## Python Usage

You can also use the renderer directly in Python:

```python
from handlers.map_renderer import render_map, render_layers, LayerStyle

# Basic rendering
result = render_map("boundaries.geojson", title="My Map")
print(f"Map saved to: {result.output_path}")

# Custom style
style = LayerStyle(color="#ff0000", fill_opacity=0.6, weight=3)
result = render_map("data.geojson", style=style)

# Multiple layers
result = render_layers(
    ["layer1.geojson", "layer2.geojson"],
    colors=["#ff0000", "#00ff00"],
    title="Comparison"
)

# Quick preview (opens browser)
from handlers.map_renderer import preview_map
preview_map("my_data.geojson")
```

## Map Controls

The generated HTML maps include:

| Control | Description |
|---------|-------------|
| **Zoom** | +/- buttons and scroll wheel |
| **Pan** | Click and drag |
| **Fullscreen** | Expand to full browser window |
| **Measure** | Measure distances and areas |
| **Layer Control** | Toggle layers on/off |
| **Tooltips** | Hover to see feature properties |

## Tile Providers

By default, maps use OpenStreetMap tiles. The tiles are loaded from:
```
https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png
```

## Output Formats

### HTML (Interactive)

- Self-contained HTML file
- No server required - works offline (tiles need internet)
- Interactive: zoom, pan, click, tooltips
- ~50KB base size + data

### PNG (Static)

- Raster image for embedding
- Requires `geopandas`, `contextily`, `matplotlib`
- Good for reports, presentations
- Configurable DPI for print quality

## Dependencies

**Required for HTML maps:**
```bash
pip install folium
```

**Required for PNG export:**
```bash
pip install geopandas contextily matplotlib
```

## Running Tests

```bash
# From repo root
pytest examples/osm-geocoder/test_visualization.py -v
```

## Troubleshooting

### "folium not installed"
```bash
pip install folium
```

### "geopandas/contextily not installed" (PNG only)
```bash
pip install geopandas contextily matplotlib
```

### Map is blank
- Check that the GeoJSON file exists and is valid JSON
- Verify features have valid geometry coordinates
- Check browser console for JavaScript errors

### Features not visible
- Check that coordinates are in WGS84 (lon/lat, not lat/lon)
- Verify features are within reasonable bounds (-180 to 180, -90 to 90)
- Try increasing `fill_opacity` or adjusting `color`
