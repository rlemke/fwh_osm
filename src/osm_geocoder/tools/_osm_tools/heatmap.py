"""Render a point GeoJSON as an interactive **heat map** HTML page.

Shared-library implementation: the FFL ``osm.Visualization.RenderHeatmap``
handler and the ``make-heatmap`` CLI both call :func:`render_heatmap`, so there
is one heat-map implementation. Object-store-native (localizes an ``s3://`` /
``hdfs://`` input, finalizes the HTML back) via the ``Storage`` abstraction.

Two styles, both dependency-light (no GIS engine, no API key):

* ``"kernel"`` (default) — embeds the points and a MapLibre GL ``heatmap`` layer,
  which does smooth Gaussian kernel density in the browser. Ideal for sparse
  point sets like "Tesla superchargers". ``weight_prop`` weights each point
  (default 1 per point).
* ``"grid"`` — pure-Python square-bin aggregation: counts points per ``cell_km``
  cell and renders the cells as a graduated-colour circle layer (a binned
  density), with the count in each cell's popup. No external deps.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _osm_tools.storage import Storage, get_storage, local_staging_subdir

_MAPLIBRE_VER = "3.6.2"


@dataclass
class HeatmapResult:
    """Outcome of a :func:`render_heatmap` call."""

    html_path: str
    relative_path: str
    point_count: int
    style: str


class HeatmapError(RuntimeError):
    """Raised when the heat map cannot be rendered."""


def _points(features: list[dict]) -> list[tuple[float, float, float]]:
    """Return (lon, lat, weight) for every Point feature."""
    out: list[tuple[float, float, float]] = []
    for f in features:
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            # Use a representative coordinate for non-points (first vertex).
            coords = g.get("coordinates")
            while isinstance(coords, list) and coords and isinstance(coords[0], list):
                coords = coords[0]
            if not (isinstance(coords, list) and len(coords) >= 2):
                continue
            lon, lat = coords[0], coords[1]
        else:
            c = g.get("coordinates") or []
            if len(c) < 2:
                continue
            lon, lat = c[0], c[1]
        out.append((float(lon), float(lat), 1.0))
    return out


def _bounds(pts: list[tuple[float, float, float]]) -> tuple[float, float, float, float]:
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def _grid_aggregate(
    pts: list[tuple[float, float, float]], cell_km: float
) -> list[tuple[float, float, int]]:
    """Bin points into ~``cell_km`` square cells; return (lon, lat, count) at
    each non-empty cell centroid. Latitude-corrected longitude spacing."""
    if not pts:
        return []
    deg_lat = cell_km / 111.0
    bins: dict[tuple[int, int], list[float]] = {}
    for lon, lat, _w in pts:
        deg_lon = cell_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
        key = (int(math.floor(lon / deg_lon)), int(math.floor(lat / deg_lat)))
        bins.setdefault(key, [lon, lat, 0.0, 0.0])
        b = bins[key]
        b[2] += lon
        b[3] += lat
        # reuse list[0:2] as count tracker via a 5th slot
    out: list[tuple[float, float, int]] = []
    # Recompute counts cleanly.
    counts: dict[tuple[int, int], list[float]] = {}
    for lon, lat, _w in pts:
        deg_lon = cell_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
        key = (int(math.floor(lon / deg_lon)), int(math.floor(lat / deg_lat)))
        c = counts.setdefault(key, [0.0, 0.0, 0])
        c[0] += lon
        c[1] += lat
        c[2] += 1
    for (_kx, _ky), (slon, slat, n) in counts.items():
        out.append((slon / n, slat / n, int(n)))
    return out


def _html(title: str, payload: dict, center: tuple[float, float], style: str) -> str:
    cx, cy = center
    data_json = json.dumps(payload)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<link href="https://unpkg.com/maplibre-gl@{_MAPLIBRE_VER}/dist/maplibre-gl.css" rel="stylesheet"/>
<script src="https://unpkg.com/maplibre-gl@{_MAPLIBRE_VER}/dist/maplibre-gl.js"></script>
<style>
  html,body,#map{{height:100%;margin:0}}
  .hdr{{position:absolute;top:10px;left:10px;z-index:1;background:rgba(0,0,0,.7);
        color:#fff;padding:8px 12px;border-radius:6px;font:14px/1.3 system-ui}}
</style>
</head>
<body>
<div id="map"></div>
<div class="hdr"><b>{title}</b><br/><span id="n"></span></div>
<script>
const DATA = {data_json};
const STYLE = {json.dumps(style)};
const map = new maplibregl.Map({{
  container:'map',
  style:{{version:8,sources:{{osm:{{type:'raster',
    tiles:['https://a.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png'],
    tileSize:256,attribution:'© OpenStreetMap contributors'}}}},
    layers:[{{id:'osm',type:'raster',source:'osm'}}]}},
  center:[{cx},{cy}], zoom:3
}});
map.addControl(new maplibregl.NavigationControl());
map.on('load',()=>{{
  map.addSource('pts',{{type:'geojson',data:DATA}});
  document.getElementById('n').textContent =
    (DATA.features?DATA.features.length:0)+' '+(STYLE==='grid'?'cells':'points');
  if(STYLE==='grid'){{
    map.addLayer({{id:'cells',type:'circle',source:'pts',paint:{{
      'circle-radius':['interpolate',['linear'],['get','count'],1,4,50,18,500,34],
      'circle-color':['interpolate',['linear'],['get','count'],
        1,'#2b83ba',10,'#abdda4',50,'#fdae61',200,'#d7191c'],
      'circle-opacity':0.75,'circle-stroke-width':0.5,'circle-stroke-color':'#222'}}}});
    map.on('click','cells',e=>{{new maplibregl.Popup().setLngLat(e.lngLat)
      .setHTML('count: <b>'+e.features[0].properties.count+'</b>').addTo(map);}});
  }} else {{
    map.addLayer({{id:'heat',type:'heatmap',source:'pts',paint:{{
      'heatmap-weight':['coalesce',['get','weight'],1],
      'heatmap-intensity':['interpolate',['linear'],['zoom'],0,1,9,3],
      'heatmap-radius':['interpolate',['linear'],['zoom'],0,15,9,40],
      'heatmap-opacity':0.85,
      'heatmap-color':['interpolate',['linear'],['heatmap-density'],
        0,'rgba(0,0,255,0)',0.2,'royalblue',0.4,'cyan',0.6,'lime',0.8,'yellow',1,'red']}}}});
    map.addLayer({{id:'dots',type:'circle',source:'pts',
      minzoom:8,paint:{{'circle-radius':3,'circle-color':'#d7191c','circle-opacity':0.7}}}});
  }}
  try{{
    const xs=DATA.features.map(f=>f.geometry.coordinates[0]);
    const ys=DATA.features.map(f=>f.geometry.coordinates[1]);
    if(xs.length) map.fitBounds([[Math.min(...xs),Math.min(...ys)],
      [Math.max(...xs),Math.max(...ys)]],{{padding:40,maxZoom:9}});
  }}catch(e){{}}
}});
</script>
</body>
</html>
"""


def render_heatmap(
    points_path: str,
    out_path: str,
    *,
    title: str = "Heat map",
    style: str = "kernel",
    weight_prop: str | None = None,
    cell_km: float = 25.0,
    storage: Any = None,
) -> HeatmapResult:
    """Render the point GeoJSON at ``points_path`` to a heat-map HTML page at
    ``out_path`` and return a :class:`HeatmapResult`.

    ``style`` is ``"kernel"`` (MapLibre heatmap layer) or ``"grid"`` (pure-Python
    ``cell_km`` square-bin density). ``weight_prop`` (kernel style) names a
    numeric feature property to weight each point by (default 1). Storage-aware:
    input/output may be local or object-store URIs.
    """
    from _osm_tools.geojson_filter import iter_features

    s = storage or get_storage()
    local_in = Path(s.localize(str(points_path)))
    # Tolerant streaming read (handles the extractors' large / possibly-truncated
    # FeatureCollection output the same way the filter does).
    features = list(iter_features(str(local_in)))
    pts = _points(features)
    if not pts:
        raise HeatmapError(f"no point features in {points_path}")

    if style == "grid":
        cells = _grid_aggregate(pts, cell_km)
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"count": n},
                }
                for lon, lat, n in cells
            ],
        }
        n_out = len(cells)
    else:  # kernel
        feats = []
        for f in features:
            g = f.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            w = 1.0
            if weight_prop:
                try:
                    w = float((f.get("properties") or {}).get(weight_prop, 1) or 1)
                except (TypeError, ValueError):
                    w = 1.0
            feats.append(
                {"type": "Feature", "geometry": g, "properties": {"weight": w}}
            )
        payload = {"type": "FeatureCollection", "features": feats}
        n_out = len(feats)

    minx, miny, maxx, maxy = _bounds(pts)
    center = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
    html = _html(title, payload, center, "grid" if style == "grid" else "kernel")

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(out_path)).name)
    staging = Path(local_staging_subdir("facetwork-heatmap")) / f"{safe}.tmp"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(html, encoding="utf-8")
    s.mkdir_p(Storage.dirname(str(out_path)))
    s.finalize_from_local(str(staging), str(out_path))

    return HeatmapResult(
        html_path=str(out_path),
        relative_path=Path(str(out_path)).name,
        point_count=n_out,
        style="grid" if style == "grid" else "kernel",
    )
