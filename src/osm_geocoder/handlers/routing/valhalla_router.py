"""Valhalla routing adapter — routes via a self-hosted Valhalla server.

A source-adapter sibling of ``osrm_router`` / ``api_router``: same result schemas
(``osm.Routing.Types``), so downstream workflows are engine-agnostic — swap
``osm.Routing.OSRM.Route`` for ``osm.Routing.Valhalla.Route`` and nothing else
changes. Talks to Valhalla's HTTP API (POST + JSON body), with a graceful
great-circle fallback when the server is unreachable (the Route philosophy).

Setup (one-time per region; Valhalla is not on Homebrew — build from source or
use the official Docker image ghcr.io/valhalla/valhalla):
    valhalla_build_config --mjolnir-tile-dir ./tiles > valhalla.json
    valhalla_build_tiles -c valhalla.json region.osm.pbf
    valhalla_service valhalla.json   # serves on :8002

Environment:
    FW_VALHALLA_URL: Valhalla server URL (default: http://localhost:8002)
"""

from __future__ import annotations

import json
import logging
import math
import os

from ..shared._output import ensure_dir, open_output, resolve_output_dir

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing.Valhalla"

VALHALLA_URL = os.environ.get("FW_VALHALLA_URL", "http://localhost:8002")

# Facetwork profile -> Valhalla costing model.
_COSTING = {
    "car": "auto", "driving": "auto", "auto": "auto",
    "bike": "bicycle", "bicycle": "bicycle", "cycling": "bicycle",
    "foot": "pedestrian", "walking": "pedestrian", "pedestrian": "pedestrian",
}


def _output_dir() -> str:
    d = resolve_output_dir("routing")
    os.makedirs(d, exist_ok=True)
    return d


def _costing(profile: str) -> str:
    return _COSTING.get(profile, "auto")


def _valhalla_post(endpoint: str, body: dict, timeout: int = 30) -> dict | None:
    """POST a JSON body to a Valhalla endpoint; return parsed JSON or None."""
    import requests

    url = f"{VALHALLA_URL}/{endpoint}"
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("Valhalla %s request to %s failed", endpoint, VALHALLA_URL)
        return None


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = r2 - r1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _decode_polyline6(encoded: str) -> list[list[float]]:
    """Decode a Valhalla-encoded polyline (precision 6) into [[lon, lat], …]."""
    coords: list[list[float]] = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        for is_lon in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append([lon / 1e6, lat / 1e6])
    return coords


def _handle_route(payload: dict) -> dict:
    """osm.Routing.Valhalla.Route — point-to-point routing via Valhalla /route."""
    from_lat, from_lon = payload.get("from_lat", 0), payload.get("from_lon", 0)
    to_lat, to_lon = payload.get("to_lat", 0), payload.get("to_lon", 0)
    from_name, to_name = payload.get("from_name", ""), payload.get("to_name", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"Valhalla.Route: {from_name or 'origin'} -> {to_name or 'dest'} ({profile})")

    data = _valhalla_post("route", {
        "locations": [{"lat": from_lat, "lon": from_lon}, {"lat": to_lat, "lon": to_lon}],
        "costing": _costing(profile),
    })

    if data and data.get("trip", {}).get("summary"):
        summary = data["trip"]["summary"]
        distance_km = round(summary.get("length", 0), 2)
        duration_min = round(summary.get("time", 0) / 60, 1)
        coords = []
        for leg in data["trip"].get("legs", []):
            if leg.get("shape"):
                coords.extend(_decode_polyline6(leg["shape"]))
        if not coords:
            coords = [[from_lon, from_lat], [to_lon, to_lat]]
        backend = "valhalla"
    else:
        distance_km = round(_haversine_km(from_lon, from_lat, to_lon, to_lat) * 1.3, 2)
        duration_min = round(distance_km / 80 * 60, 1)
        coords = [[from_lon, from_lat], [to_lon, to_lat]]
        backend = "estimate"

    slug = f"{from_name or 'origin'}-{to_name or 'dest'}-{profile}".replace(" ", "_")
    output_path = os.path.join(_output_dir(), f"valhalla-route-{slug}.geojson")
    ensure_dir(output_path)
    with open_output(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"from": from_name, "to": to_name, "distance_km": distance_km,
                           "duration_min": duration_min, "profile": profile}}]}, f)

    if step_log:
        step_log(f"Valhalla.Route: {distance_km} km, {duration_min} min ({backend})", level="success")

    return {"result": {"route": {
        "from_name": from_name, "to_name": to_name, "distance_km": distance_km,
        "duration_min": duration_min, "output_path": output_path, "profile": profile,
        "backend": backend, "format": "GeoJSON"}, "waypoint_count": 2}}


def _parse_points(raw) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        pts = []
        for pair in raw.split(";"):
            parts = pair.strip().split(",")
            if len(parts) >= 2:
                pts.append({"lon": float(parts[0]), "lat": float(parts[1]), "name": ""})
        return pts
    return []


def _handle_matrix(payload: dict) -> dict:
    """osm.Routing.Valhalla.Matrix — NxN matrix via /sources_to_targets."""
    points = _parse_points(payload.get("points", ""))
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")
    if len(points) < 2:
        return {"result": {"output_path": "", "point_count": 0, "durations": "[]",
                           "distances": "[]", "profile": profile, "backend": "none", "format": "JSON"}}

    if step_log:
        step_log(f"Valhalla.Matrix: {len(points)}x{len(points)} ({profile})")

    locs = [{"lat": p.get("lat", 0), "lon": p.get("lon", 0)} for p in points]
    data = _valhalla_post("sources_to_targets",
                          {"sources": locs, "targets": locs, "costing": _costing(profile)})

    if data and data.get("sources_to_targets"):
        durations, distances = [], []
        for row in data["sources_to_targets"]:
            durations.append([round(c.get("time") or 0, 1) for c in row])
            distances.append([round((c.get("distance") or 0) * 1000, 1) for c in row])  # km -> m
        backend = "valhalla"
    else:
        durations, distances = [], []
        coords = [(p.get("lon", 0), p.get("lat", 0)) for p in points]
        for a in coords:
            dr, mr = [], []
            for b in coords:
                km = _haversine_km(a[0], a[1], b[0], b[1]) * 1.3
                dr.append(round(km / 80 * 3600, 1))
                mr.append(round(km * 1000, 1))
            durations.append(dr)
            distances.append(mr)
        backend = "estimate"

    output_path = os.path.join(_output_dir(), f"valhalla-matrix-{len(points)}pts-{profile}.json")
    ensure_dir(output_path)
    with open_output(output_path, "w") as f:
        json.dump({"durations": durations, "distances": distances,
                   "profile": profile, "backend": backend}, f)

    if step_log:
        step_log(f"Valhalla.Matrix: {len(points)}x{len(points)} ({backend})", level="success")

    return {"result": {"output_path": output_path, "point_count": len(points),
                       "durations": json.dumps(durations), "distances": json.dumps(distances),
                       "profile": profile, "backend": backend, "format": "JSON"}}


def _handle_isochrone(payload: dict) -> dict:
    """osm.Routing.Valhalla.Isochrone — reachability polygon via /isochrone."""
    center_lat, center_lon = payload.get("center_lat", 0), payload.get("center_lon", 0)
    center_name = payload.get("center_name", "")
    time_minutes = payload.get("time_minutes", 15)
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"Valhalla.Isochrone: {center_name or 'center'}, {time_minutes}min ({profile})")

    data = _valhalla_post("isochrone", {
        "locations": [{"lat": center_lat, "lon": center_lon}],
        "costing": _costing(profile),
        "contours": [{"time": time_minutes}],
        "polygons": True,
    })

    backend = "valhalla" if data and data.get("features") else "none"
    geojson = data if backend == "valhalla" else {"type": "FeatureCollection", "features": []}
    output_path = os.path.join(
        _output_dir(),
        f"valhalla-isochrone-{center_name or 'center'}-{time_minutes}min-{profile}.geojson".replace(" ", "_"),
    )
    ensure_dir(output_path)
    with open_output(output_path, "w") as f:
        json.dump(geojson, f)

    if step_log:
        step_log(f"Valhalla.Isochrone: {time_minutes}min polygon ({backend})", level="success")

    return {"result": {"output_path": output_path, "center_name": center_name,
                       "time_minutes": time_minutes, "profile": profile,
                       "backend": backend, "format": "GeoJSON"}}


VALHALLA_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.Route": _handle_route,
    f"{NAMESPACE}.Matrix": _handle_matrix,
    f"{NAMESPACE}.Isochrone": _handle_isochrone,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = VALHALLA_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown Valhalla routing facet: {facet_name}")
    return handler(payload)
