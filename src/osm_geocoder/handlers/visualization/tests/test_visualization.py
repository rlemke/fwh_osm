#!/usr/bin/env python3
"""Unit tests for the GeoJSON visualization handlers.

Run from the repo root:
    pytest examples/osm-geocoder/tests/mocked/py/test_visualization.py -v
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from osm_geocoder.handlers.visualization.map_renderer import (
    HAS_FOLIUM,
    LayerStyle,
    bounds_to_string,
    calculate_bounds,
    calculate_center,
    calculate_zoom,
)
from osm_geocoder.handlers.visualization.visualization_handlers import (
    NAMESPACE,
    VISUALIZATION_FACETS,
    register_visualization_handlers,
)

# Skip marker for tests requiring folium
requires_folium = pytest.mark.skipif(not HAS_FOLIUM, reason="folium not installed")


class TestCalculateBounds:
    """Tests for calculate_bounds()."""

    def test_point_geometry(self):
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10, 20]}}
            ],
        }
        bounds = calculate_bounds(geojson)
        assert bounds == (10, 20, 10, 20)

    def test_linestring_geometry(self):
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [10, 10], [20, 5]]},
                }
            ],
        }
        bounds = calculate_bounds(geojson)
        assert bounds == (0, 0, 20, 10)

    def test_polygon_geometry(self):
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
                    },
                }
            ],
        }
        bounds = calculate_bounds(geojson)
        assert bounds == (0, 0, 10, 10)

    def test_multiple_features(self):
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-10, -20]}},
                {"type": "Feature", "geometry": {"type": "Point", "coordinates": [30, 40]}},
            ],
        }
        bounds = calculate_bounds(geojson)
        assert bounds == (-10, -20, 30, 40)

    def test_empty_features(self):
        geojson = {"type": "FeatureCollection", "features": []}
        bounds = calculate_bounds(geojson)
        assert bounds is None

    def test_no_geometry(self):
        geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None}]}
        bounds = calculate_bounds(geojson)
        assert bounds is None


class TestBoundsToString:
    """Tests for bounds_to_string()."""

    def test_valid_bounds(self):
        bounds = (-122.5, 37.5, -122.0, 38.0)
        result = bounds_to_string(bounds)
        assert result == "-122.500000,37.500000,-122.000000,38.000000"

    def test_none_bounds(self):
        assert bounds_to_string(None) == ""


class TestCalculateCenter:
    """Tests for calculate_center()."""

    def test_valid_bounds(self):
        bounds = (0, 0, 10, 10)
        center = calculate_center(bounds)
        assert center == (5, 5)  # (lat, lon)

    def test_none_bounds_returns_default(self):
        center = calculate_center(None)
        assert center == (39.8283, -98.5795)  # Center of US


class TestCalculateZoom:
    """Tests for calculate_zoom()."""

    def test_large_area(self):
        bounds = (-180, -90, 180, 90)  # World
        zoom = calculate_zoom(bounds)
        assert zoom <= 3

    def test_medium_area(self):
        bounds = (-10, -5, 10, 5)  # ~20 degrees
        zoom = calculate_zoom(bounds)
        assert 4 <= zoom <= 6

    def test_small_area(self):
        bounds = (0, 0, 0.1, 0.1)  # Small area
        zoom = calculate_zoom(bounds)
        assert zoom >= 10

    def test_none_bounds(self):
        zoom = calculate_zoom(None)
        assert zoom == 4  # Default


class TestLayerStyle:
    """Tests for LayerStyle dataclass."""

    def test_default_style(self):
        style = LayerStyle()
        assert style.color == "#3388ff"
        assert style.weight == 2
        assert style.fill_opacity == 0.4

    def test_custom_style(self):
        style = LayerStyle(color="#ff0000", weight=5, fill_opacity=0.8)
        assert style.color == "#ff0000"
        assert style.weight == 5
        assert style.fill_opacity == 0.8

    def test_to_folium_style(self):
        style = LayerStyle(color="#ff0000", fill_color="#00ff00")
        folium_style = style.to_folium_style()
        assert folium_style["color"] == "#ff0000"
        assert folium_style["fillColor"] == "#00ff00"

    def test_fill_color_defaults_to_color(self):
        style = LayerStyle(color="#ff0000")
        folium_style = style.to_folium_style()
        assert folium_style["fillColor"] == "#ff0000"


@requires_folium
class TestRenderMapHtml:
    """Tests for render_map_html()."""

    @pytest.fixture
    def sample_geojson(self, tmp_path):
        """Create a sample GeoJSON file."""
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Test Polygon"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-122, 37], [-121, 37], [-121, 38], [-122, 38], [-122, 37]]
                        ],
                    },
                }
            ],
        }
        path = tmp_path / "test.geojson"
        with open(path, "w") as f:
            json.dump(geojson, f)
        return path

    def test_render_creates_html(self, sample_geojson, tmp_path):
        from osm_geocoder.handlers.visualization.map_renderer import render_map_html

        output_path = tmp_path / "map.html"
        result = render_map_html(sample_geojson, output_path)

        assert result.format == "html"
        assert result.feature_count == 1
        assert Path(result.output_path).exists()

        # Verify it's valid HTML
        with open(result.output_path) as f:
            content = f.read()
        assert "<html>" in content or "<!DOCTYPE html>" in content
        assert "leaflet" in content.lower()

    def test_render_default_output_path(self, sample_geojson, tmp_path, monkeypatch):
        from osm_geocoder.handlers.visualization.map_renderer import render_map_html

        # ⚠️ BOTH env vars, not just FW_LOCAL_OUTPUT_DIR. tests/mocked/py/conftest.py
        # sets FW_OUTPUT_BASE from a SESSION-scoped monkeypatch, which stays in
        # force for the rest of the session — including this file — and
        # FW_OUTPUT_BASE outranks FW_LOCAL_OUTPUT_DIR. So this test passed on its
        # own and failed in a full-suite run, which is what CI does.
        monkeypatch.setenv("FW_LOCAL_OUTPUT_DIR", str(tmp_path))
        monkeypatch.setenv("FW_OUTPUT_BASE", str(tmp_path))
        # Reload the cached module-level variable
        import osm_geocoder.handlers.shared._output as _output_mod

        monkeypatch.setattr(_output_mod, "_OUTPUT_BASE", "")

        result = render_map_html(sample_geojson)

        expected_path = os.path.join(str(tmp_path), "maps", "test.html")
        assert result.output_path == expected_path
        assert Path(result.output_path).exists()

    def test_render_with_custom_style(self, sample_geojson, tmp_path):
        from osm_geocoder.handlers.visualization.map_renderer import render_map_html

        output_path = tmp_path / "styled.html"
        style = LayerStyle(color="#ff0000", fill_opacity=0.8)
        result = render_map_html(sample_geojson, output_path, style=style)

        assert result.feature_count == 1
        assert Path(result.output_path).exists()

    def test_render_with_title(self, sample_geojson, tmp_path):
        from osm_geocoder.handlers.visualization.map_renderer import render_map_html

        output_path = tmp_path / "titled.html"
        result = render_map_html(sample_geojson, output_path, title="My Custom Map")

        assert result.title == "My Custom Map"

        with open(result.output_path) as f:
            content = f.read()
        assert "My Custom Map" in content


@requires_folium
class TestRenderLayers:
    """Tests for render_layers()."""

    @pytest.fixture
    def two_geojson_files(self, tmp_path):
        """Create two sample GeoJSON files."""
        geojson1 = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Layer 1"},
                    "geometry": {"type": "Point", "coordinates": [0, 0]},
                }
            ],
        }
        geojson2 = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Layer 2"},
                    "geometry": {"type": "Point", "coordinates": [10, 10]},
                }
            ],
        }

        path1 = tmp_path / "layer1.geojson"
        path2 = tmp_path / "layer2.geojson"

        with open(path1, "w") as f:
            json.dump(geojson1, f)
        with open(path2, "w") as f:
            json.dump(geojson2, f)

        return [str(path1), str(path2)]

    def test_render_multiple_layers(self, two_geojson_files, tmp_path):
        from osm_geocoder.handlers.visualization.map_renderer import render_layers

        output_path = tmp_path / "layers.html"
        result = render_layers(two_geojson_files, output_path=output_path)

        assert result.format == "html"
        assert result.feature_count == 2
        assert Path(result.output_path).exists()

    def test_render_with_custom_colors(self, two_geojson_files, tmp_path):
        from osm_geocoder.handlers.visualization.map_renderer import render_layers

        output_path = tmp_path / "colored.html"
        colors = ["#ff0000", "#00ff00"]
        result = render_layers(two_geojson_files, colors=colors, output_path=output_path)

        assert result.feature_count == 2


class TestVisualizationHandlers:
    """Tests for visualization event handlers."""

    def test_render_map_handler_no_folium(self):
        """Missing folium must REFUSE, not return an empty result.

        The handler was hardened to raise; these tests still asserted the old
        silent-empty behaviour and nothing caught it, because the repo had no CI.
        """
        from osm_geocoder.handlers.visualization.visualization_handlers import _make_render_map_handler

        handler = _make_render_map_handler("RenderMap")

        with patch("osm_geocoder.handlers.visualization.visualization_handlers.HAS_FOLIUM", False):
            with pytest.raises(RuntimeError, match="folium"):
                    handler(
                    {
                        "geojson_path": "/some/file.geojson",
                        "title": "Test Map",
                        "format": "html",
                    }
                )


    def test_render_map_handler_empty_path(self):
        """Test handler with empty input path."""
        from osm_geocoder.handlers.visualization.visualization_handlers import _make_render_map_handler

        handler = _make_render_map_handler("RenderMap")

        result = handler(
            {
                "geojson_path": "",
                "title": "Test",
            }
        )

        assert result["result"]["feature_count"] == 0

    def test_render_layers_handler_empty_layers(self):
        """Test layers handler with empty layer list."""
        from osm_geocoder.handlers.visualization.visualization_handlers import _make_render_layers_handler

        handler = _make_render_layers_handler("RenderLayers")

        result = handler(
            {
                "layers": [],
                "title": "Test",
            }
        )

        assert result["result"]["feature_count"] == 0

    def test_preview_handler_no_folium(self):
        """Test preview handler without folium."""
        from osm_geocoder.handlers.visualization.visualization_handlers import _make_preview_map_handler

        handler = _make_preview_map_handler("PreviewMap")

        with patch("osm_geocoder.handlers.visualization.visualization_handlers.HAS_FOLIUM", False):
            with pytest.raises(RuntimeError, match="folium"):
                    handler({"geojson_path": "/some/file.geojson"})



class TestHandlerRegistration:
    """Tests for handler registration."""

    def test_register_visualization_handlers(self):
        """Test that all visualization handlers are registered."""
        mock_poller = MagicMock()
        register_visualization_handlers(mock_poller)

        registered_names = [call[0][0] for call in mock_poller.register.call_args_list]

        assert f"{NAMESPACE}.RenderMap" in registered_names
        assert f"{NAMESPACE}.RenderMapAt" in registered_names
        assert f"{NAMESPACE}.RenderLayers" in registered_names
        assert f"{NAMESPACE}.RenderStyledMap" in registered_names
        assert f"{NAMESPACE}.PreviewMap" in registered_names

    def test_facet_count(self):
        """Verify expected number of visualization facets."""
        assert len(VISUALIZATION_FACETS) == 7  # RenderMap/RenderMapAt/RenderStyledMap/PreviewMap/RenderTiledMap (+ heatmap helpers)


class TestRenderTiledMap:
    """The MapLibre+PMTiles viewer styling (legibility of dots/routes)."""

    def _render(self, tmp_path, basemap="dark", layers=("routes_5M", "cities_5M")):
        from osm_geocoder.handlers.visualization.map_renderer import render_tiled_map

        tiles = []
        for name in layers:
            p = tmp_path / f"{name}.pmtiles"
            p.write_bytes(b"x")
            tiles.append(p)
        out = tmp_path / f"viewer_{basemap}"
        render_tiled_map(
            tiles, layer_names=list(layers), title="T", output_path=out, basemap=basemap
        )
        return (out / "index.html").read_text()

    def test_default_basemap_is_dark_and_keyless(self, tmp_path):
        """⚠️ This used to assert CARTO subdomains — pinning a basemap that
        CARTO began enforcing API keys on in Aug 2026, watermarking every
        unauthenticated tile "API KEY REQUIRED". The published gallery was
        migrated off CARTO on 2026-09-04; this renderer was not, and kept
        producing watermarked maps until a user reported one on 2026-09-24.

        "dark" is now keyless OSM raster darkened with MapLibre's own paint
        properties, so the look survives without a key that can be revoked.
        """
        html = self._render(tmp_path)
        assert "cartocdn" not in html, "no keyed basemap may come back"
        assert "tile.openstreetmap.org" in html
        # darkened in the renderer rather than by fetching a dark tileset
        assert "raster-brightness-max" in html and "raster-saturation" in html

    def test_no_basemap_option_uses_a_keyed_host(self, tmp_path):
        for basemap in ("dark", "light", "osm", "none"):
            assert "cartocdn" not in self._render(tmp_path, basemap=basemap), basemap

    def test_routes_drawn_under_dots_with_casing(self, tmp_path):
        html = self._render(tmp_path)
        import re

        ids = re.findall(r"\{id:'([^']+)'", html)
        assert "layer0-casing" in ids  # line gets a casing for separation
        last_line = max(i for i, x in enumerate(ids) if x == "layer0" or x.endswith("-casing"))
        first_dot = min(i for i, x in enumerate(ids) if x.endswith("-dot"))
        assert last_line < first_dot  # every route sits beneath every city dot

    def test_zoom_interpolated_sizing(self, tmp_path):
        html = self._render(tmp_path)
        assert "'interpolate'" in html  # radii/widths scale with zoom

    def test_legend_lists_layers(self, tmp_path):
        html = self._render(tmp_path)
        assert "id='legend'" in html
        assert "routes 5M" in html and "cities 5M" in html

    def test_basemap_osm_and_none(self, tmp_path):
        osm = self._render(tmp_path, basemap="osm")
        assert "tile.openstreetmap.org" in osm
        none = self._render(tmp_path, basemap="none")
        assert "background-color" in none
        # ⚠️ Assert on the TILE URL, not the word. basemap="none" must drop the
        # basemap tiles, but the page still carries the ODbL DATA attribution
        # linking to openstreetmap.org — which is required, so a bare
        # "openstreetmap" not in ... would be satisfied only by removing a
        # legally necessary credit.
        assert "cartocdn" not in none
        assert "tile.openstreetmap.org" not in none


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestBasemapFingerprint:
    """`basemap="dark"` names an option, not its content.

    When CARTO began watermarking and "dark" was repointed at keyless OSM
    raster, every cached map stayed on the old tiles because the PARAMETER had
    not changed — a re-render returned the watermarked page from cache
    (measured 2026-09-24). The render cache must key on what the option points
    at, not only on its name.
    """

    def test_fingerprint_is_stable(self):
        from osm_geocoder.handlers.visualization.map_renderer import basemap_fingerprint
        assert basemap_fingerprint() == basemap_fingerprint()

    def test_repointing_a_basemap_changes_the_fingerprint(self, monkeypatch):
        from osm_geocoder.handlers.visualization import map_renderer as mr
        before = mr.basemap_fingerprint()
        monkeypatch.setitem(mr._BASEMAPS, "dark", ("dark", "bg:{type:'raster'}", "{id:'bg'}"))
        assert mr.basemap_fingerprint() != before

    def test_the_render_handler_includes_it_in_its_cache_key(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "visualization_handlers.py").read_text()
        assert '"basemap_def": basemap_fingerprint()' in src
