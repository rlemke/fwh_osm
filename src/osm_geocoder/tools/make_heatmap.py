#!/usr/bin/env python3
"""make-heatmap — render a point GeoJSON as an interactive heat-map HTML page.

Shares the exact implementation with the FFL ``osm.viz.RenderHeatmap`` handler:
both call ``_osm_tools.heatmap.render_heatmap``. Input/output may be local paths
or ``s3://``/``hdfs://`` URIs. No GIS engine or API key required.

Examples:
  make-heatmap tesla.geojson tesla_heat.html --title "Tesla Superchargers"
  make-heatmap tesla.geojson tesla_grid.html --style grid --cell-km 50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.heatmap import HeatmapError, render_heatmap  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Input point GeoJSON path/URI")
    p.add_argument("output", help="Output HTML path/URI")
    p.add_argument("--title", default="Heat map")
    p.add_argument("--style", choices=["kernel", "grid"], default="kernel",
                   help="kernel = MapLibre heatmap layer; grid = cell_km square-bin density")
    p.add_argument("--weight-prop", default=None, help="(kernel) numeric property to weight points by")
    p.add_argument("--cell-km", type=float, default=25.0, help="(grid) cell size in km")
    a = p.parse_args()
    try:
        r = render_heatmap(a.input, a.output, title=a.title, style=a.style,
                           weight_prop=a.weight_prop, cell_km=a.cell_km)
    except HeatmapError as exc:
        print(f"heatmap error: {exc}", file=sys.stderr)
        return 2
    print(f"{r.point_count} points -> {r.style} heat map at {r.html_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
