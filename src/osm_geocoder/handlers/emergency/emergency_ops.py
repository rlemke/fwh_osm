"""Compute core for ``osm.emergency`` (emergency-access atlas).

Pure functions over GeoJSON layers + route distances; storage-backend aware
(paths may be s3://). Scoring contract (design doc §7 — disclosed, not
hidden): a city's per-category component is ``max(0, 100 * (1 - nearest
network km / 50))`` (0 when the category has no facilities); the city score
is the weighted mean over categories (equal weights by default, ``weights``
JSON overrides, renormalized over categories present); the region score is
the population-weighted mean of its city scores. 10/25/50 km bucket counts
are straight-line (crow-flies) over ALL facilities; nearest/median are
network distances to the k routed candidates — both labeled in the output.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from html import escape

from facetwork.runtime import storage as fws

from ..shared._output import resolve_output_dir

DEFAULT_WEIGHTS = {"hospitals": 0.25, "fire": 0.25, "police": 0.25, "shelters": 0.25}
NEAREST_FULL_SCORE_KM = 0.0
NEAREST_ZERO_SCORE_KM = 50.0
BUCKET_RADII_KM = (10.0, 25.0, 50.0)


# ---------------------------------------------------------------------------
# IO helpers.
# ---------------------------------------------------------------------------


def _read_json(path: str):
    local = fws.localize(path) if "://" in (path or "") else path
    with open(local, encoding="utf-8") as f:
        return json.load(f)


def _write_text(path: str, text: str) -> None:
    if "://" in path:
        with fws.get_storage_backend(path).open(path, "wb") as f:
            f.write(text.encode("utf-8"))
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _centroid(geom: dict) -> tuple[float, float] | None:
    """(lon, lat) — exact for Points, bbox-average otherwise (good enough
    for candidate pairing; routing uses the true network)."""
    if not geom:
        return None
    if geom.get("type") == "Point":
        c = geom.get("coordinates") or []
        return (float(c[0]), float(c[1])) if len(c) >= 2 else None
    lo = [180.0, 90.0]
    hi = [-180.0, -90.0]

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            lo[0] = min(lo[0], c[0]); lo[1] = min(lo[1], c[1])
            hi[0] = max(hi[0], c[0]); hi[1] = max(hi[1], c[1])
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)

    walk(geom.get("coordinates") or [])
    if hi[0] < lo[0]:
        return None
    return ((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _prop(props: dict, key: str):
    if key in props:
        return props[key]
    tags = props.get("tags") or {}
    return tags.get(key)


# ---------------------------------------------------------------------------
# Facet cores.
# ---------------------------------------------------------------------------


def top_cities(input_path: str, max_cities: int, min_population: int) -> dict:
    fc = _read_json(input_path)
    tagged, untagged = [], 0
    for ft in fc.get("features") or []:
        props = ft.get("properties") or {}
        c = _centroid(ft.get("geometry"))
        if not c:
            continue
        raw_pop = _prop(props, "population")
        try:
            pop = int(str(raw_pop).replace(",", ""))
        except (TypeError, ValueError):
            untagged += 1
            continue
        if pop < min_population:
            continue
        name = _prop(props, "name") or "(unnamed)"
        tagged.append({"name": str(name), "lat": c[1], "lon": c[0], "population": pop})
    tagged.sort(key=lambda x: -x["population"])
    cities = tagged[:max_cities]
    return {"cities": cities, "city_count": len(cities), "untagged_count": untagged}


def build_category_set(hospitals_path: str, fire_path: str, police_path: str,
                       shelters_path: str) -> dict:
    cats = [
        {"name": "hospitals", "path": hospitals_path},
        {"name": "fire", "path": fire_path},
        {"name": "police", "path": police_path},
        {"name": "shelters", "path": shelters_path},
    ]
    present = []
    for c in cats:
        if not c["path"]:
            continue
        # Drop zero-feature layers too: a region with NO facilities of a
        # category would give every city an empty candidate set (a
        # zero-iteration route fan-out), and "no mapped facilities" is more
        # honestly expressed as the category being absent (weights
        # renormalize) than as an unroutable empty layer.
        try:
            n = len((_read_json(c["path"]) or {}).get("features") or [])
        except Exception:
            n = 0
        if n > 0:
            present.append(c)
    if not present:
        # Fail LOUD here: an all-empty category set means the upstream
        # extract/filter chain broke (e.g. an unknown scan category returning
        # a silent empty) — completing with [] would cascade into
        # empty-foreach reference errors three levels downstream.
        raise RuntimeError(
            "every facility layer path is empty — upstream extract/filter "
            "produced nothing; refusing to emit an empty category set"
        )
    return {"categories": present}


def nearest_candidates(facilities_path: str, from_lat: float, from_lon: float,
                       k: int) -> dict:
    fc = _read_json(facilities_path)
    dists = []
    for ft in fc.get("features") or []:
        c = _centroid(ft.get("geometry"))
        if c:
            dists.append((_haversine_km(from_lat, from_lon, c[1], c[0]), c[1], c[0]))
    dists.sort(key=lambda x: x[0])
    pairs = [
        {"from_lat": from_lat, "from_lon": from_lon, "to_lat": d[1], "to_lon": d[2]}
        for d in dists[:k]
    ]
    buckets = {
        str(int(r)): sum(1 for d in dists if d[0] <= r) for r in BUCKET_RADII_KM
    }
    return {"pairs": pairs, "facility_count": len(dists), "bucket_counts": buckets}


def category_metrics(category: str, network_distances: list, bucket_counts: dict,
                     facility_count: int, city: dict) -> dict:
    ds = sorted(float(d) for d in (network_distances or []) if d is not None)
    nearest = ds[0] if ds else None
    median = ds[len(ds) // 2] if ds else None
    pop = int(city.get("population") or 0)
    per_100k = round(100000.0 * facility_count / pop, 2) if pop > 0 else None
    return {"metrics_json": json.dumps({
        "category": category,
        "facility_count": int(facility_count),
        "nearest_network_km": round(nearest, 2) if nearest is not None else None,
        "median_network_km": round(median, 2) if median is not None else None,
        "crowflies_within_km": bucket_counts or {},
        "per_100k": per_100k,
    })}


def _component(nearest_km) -> float:
    if nearest_km is None:
        return 0.0
    span = NEAREST_ZERO_SCORE_KM - NEAREST_FULL_SCORE_KM
    return max(0.0, min(100.0, 100.0 * (1.0 - (float(nearest_km) - NEAREST_FULL_SCORE_KM) / span)))


def city_readiness(city: dict, category_metrics_list: list[str]) -> dict:
    cats = {}
    for blob in category_metrics_list or []:
        m = json.loads(blob)
        m["component"] = round(_component(m.get("nearest_network_km")), 1)
        cats[m["category"]] = m
    return {"metrics_json": json.dumps({"city": city, "categories": cats})}


def region_readiness(region: str, city_metrics: list[str], weights: str) -> dict:
    try:
        w = {**DEFAULT_WEIGHTS, **(json.loads(weights) if weights else {})}
    except (ValueError, TypeError):
        w = dict(DEFAULT_WEIGHTS)
    cities, feats = [], []
    for blob in city_metrics or []:
        cm = json.loads(blob)
        cats = cm.get("categories") or {}
        present = {k: v for k, v in cats.items() if k in w}
        wsum = sum(w[k] for k in present) or 1.0
        score = round(sum(w[k] * present[k]["component"] for k in present) / wsum, 1)
        city = cm.get("city") or {}
        cities.append({"city": city, "score": score, "categories": cats})
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [city.get("lon"), city.get("lat")]},
            "properties": {
                "name": city.get("name"), "region": region,
                "population": city.get("population"), "score": score,
                "breakdown": json.dumps(cats),
            },
        })
    pop_total = sum(int(c["city"].get("population") or 0) for c in cities)
    if pop_total > 0:
        score = round(sum(c["score"] * int(c["city"].get("population") or 0)
                          for c in cities) / pop_total, 1)
    else:
        score = round(sum(c["score"] for c in cities) / len(cities), 1) if cities else 0.0

    layer_path = f"{resolve_output_dir('emergency')}/regions/{region.lower().replace(' ', '-')}.geojson"
    _write_text(layer_path, json.dumps(
        {"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
    return {
        "score": score,
        "metrics_json": json.dumps({"region": region, "score": score,
                                    "weights": w, "cities": cities}),
        "layer_path": layer_path,
    }


def rank_regions(region_metrics: list[str]) -> dict:
    rows = []
    for blob in region_metrics or []:
        rm = json.loads(blob)
        best = max(rm.get("cities") or [], key=lambda c: c["score"], default=None)
        rows.append({
            "region": rm.get("region"), "score": rm.get("score"),
            "city_count": len(rm.get("cities") or []),
            "best_city": (best or {}).get("city", {}).get("name"),
        })
    rows.sort(key=lambda r: -(r["score"] or 0))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"rankings": rows, "region_count": len(rows)}


_ABOUT = (
    "Emergency-access readiness per city: for each category (hospitals, fire, "
    "police, mapped shelters) the component is 100 at 0 km falling to 0 at "
    "50 km of <b>network distance</b> to the nearest facility over the "
    "region's major-road (motorway&ndash;secondary) network; the city score "
    "is the weighted category mean and the region score is the "
    "population-weighted city mean &mdash; <b>weights are a disclosed value "
    "judgment</b>. 10/25/50 km counts are straight-line, not network. "
    "Facilities come from each region's OSM extract; OSM coverage varies and "
    "<b>shelters especially are thinly mapped</b> &mdash; a low shelter count "
    "can mean unmapped, not absent. Cities are OSM places with a population "
    "tag (untagged places are excluded). Routing is approximate (see "
    "docs/architecture/approximate-freeway-routing.md)."
)


def render_atlas(layer_path: str, rankings: list, title: str) -> dict:
    fc = _read_json(layer_path)
    fc_js = json.dumps(fc, separators=(",", ":"))
    rank_js = json.dumps(rankings or [])
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rows = "".join(
        f"<tr><td class='r'>{r.get('rank')}</td><td>{escape(str(r.get('region')))}</td>"
        f"<td class='r'>{r.get('score')}</td><td>{escape(str(r.get('best_city') or ''))}</td></tr>"
        for r in (rankings or [])
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
 html,body,#map{{margin:0;height:100%;width:100%;font-family:system-ui,sans-serif}}
 .panel{{position:absolute;z-index:1;background:rgba(255,255,255,.94);padding:10px 12px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);font-size:12px}}
 #ctl{{top:10px;left:10px;max-width:360px;max-height:82vh;overflow:auto}}
 #ctl h3{{margin:0 0 6px;font-size:14px}}
 table{{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}}
 th,td{{padding:3px 6px;border-bottom:1px solid #eee;text-align:left}} td.r,th.r{{text-align:right}}
 .maplibregl-popup-content{{max-width:320px;font-size:12px}}
 .maplibregl-popup-content h4{{margin:0 0 4px;font-size:13px}}
 .infobtn{{margin-top:8px;width:100%;padding:7px;font-size:13px;cursor:pointer;background:#fff8e1;border:1px solid #f6c343;border-radius:4px;color:#5d4b00}}
 .modal{{position:absolute;inset:0;z-index:5;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center}}
 .modalcard{{background:#fff;max-width:480px;margin:16px;padding:18px 20px;border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.4);position:relative;font:13px/1.5 system-ui,sans-serif;max-height:80vh;overflow:auto}}
 .modalclose{{position:absolute;top:6px;right:10px;border:none;background:none;font-size:24px;cursor:pointer;color:#999}}
 .attr{{position:fixed;bottom:10px;left:10px;z-index:9999;background:rgba(255,255,255,.92);border-radius:6px;padding:6px 10px;box-shadow:0 1px 4px rgba(0,0,0,.2);font:11px system-ui,sans-serif;color:#444}}
</style></head><body>
<div id="map"></div>
<div id="ctl" class="panel"><h3>{escape(title)}</h3>
<div style="color:#555">City readiness 0&ndash;100 (green = ready). Click a city for its category breakdown.</div>
<table><tr><th class="r">#</th><th>Region</th><th class="r">Score</th><th>Best city</th></tr>{rows}</table>
<button id="infobtn" class="infobtn">&#8505;&#65039; About this data</button></div>
<div id="infomodal" class="modal"><div class="modalcard"><button id="infoclose" class="modalclose">&times;</button><h2 style="font-size:16px;margin:0 0 8px">About this data</h2><div>{_ABOUT}</div></div></div>
<div class="attr">Generated by <code>osm.emergency.flows.ContinentalEmergencyAtlas</code> &middot; OSM &copy; contributors &middot; {ts}</div>
<script>
const FC={fc_js}, RANKS={rank_js};
const map=new maplibregl.Map({{container:'map',style:{{version:8,
 sources:{{bm:{{type:'raster',tiles:['https://a.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'],tileSize:256,attribution:'&copy; OSM &copy; CARTO'}}}},
 layers:[{{id:'bm',type:'raster',source:'bm'}}]}},center:[5.5,51.5],zoom:6}});
map.addControl(new maplibregl.NavigationControl());
map.on('load',()=>{{
 map.addSource('c',{{type:'geojson',data:FC}});
 map.addLayer({{id:'pts',type:'circle',source:'c',paint:{{
  'circle-radius':['interpolate',['linear'],['coalesce',['get','population'],0],10000,5,1000000,14],
  'circle-color':['interpolate',['linear'],['get','score'],0,'#b2182b',50,'#fee08b',100,'#1a9850'],
  'circle-stroke-color':'#fff','circle-stroke-width':1,'circle-opacity':0.88}}}});
 map.on('click','pts',e=>{{const p=e.features[0].properties||{{}};
  const cats=typeof p.breakdown==='string'?JSON.parse(p.breakdown):(p.breakdown||{{}});
  let rows='';Object.keys(cats).forEach(k=>{{const m=cats[k];
   rows+=`<tr><td>${{k}}</td><td class="r">${{m.nearest_network_km==null?'&mdash;':m.nearest_network_km+' km'}}</td><td class="r">${{m.facility_count}}</td><td class="r">${{m.component}}</td></tr>`;}});
  new maplibregl.Popup({{maxWidth:'330px'}}).setLngLat(e.lngLat).setHTML(
   `<h4>${{p.name}} (${{p.region}})</h4>score <b>${{p.score}}</b>${{p.population?` &middot; pop ${{(+p.population).toLocaleString()}}`:''}}
    <table><tr><td></td><td class="r"><b>nearest</b></td><td class="r"><b>count</b></td><td class="r"><b>comp</b></td></tr>${{rows}}</table>`).addTo(map);}});
 map.on('mouseenter','pts',()=>map.getCanvas().style.cursor='pointer');
 map.on('mouseleave','pts',()=>map.getCanvas().style.cursor='');
}});
const im=document.getElementById('infomodal');
document.getElementById('infobtn').onclick=()=>im.style.display='flex';
document.getElementById('infoclose').onclick=()=>im.style.display='none';
im.onclick=e=>{{if(e.target===im)im.style.display='none';}};
</script></body></html>"""
    html_path = f"{resolve_output_dir('emergency')}/atlas/index.html"
    _write_text(html_path, html)
    return {"html_path": html_path}
