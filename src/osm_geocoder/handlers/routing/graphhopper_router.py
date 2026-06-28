"""GraphHopper routing adapter — routes via a self-hosted GraphHopper server.

A source-adapter sibling of ``osrm_router`` / ``valhalla_router`` / ``api_router``:
same result schemas (``osm.Routing.Types``), so the backend is swappable by
namespace. Talks to the open-source GraphHopper Directions HTTP API (GET with
query params), with a graceful great-circle fallback when the server is
unreachable. Covers the verbs the OSS server reliably exposes — ``Route`` and
``Isochrone`` (Matrix / map-matching are separate/commercial GraphHopper modules).

Setup (one-time per region; needs Java + the graphhopper-web jar):
    java -jar graphhopper-web.jar import config.yml   # build the graph
    java -jar graphhopper-web.jar server config.yml   # serves on :8989

Environment:
    FW_GRAPHHOPPER_URL: GraphHopper server URL (default: http://localhost:8989)
"""

from __future__ import annotations

import json
import logging
import math
import os

from ..shared._output import ensure_dir, open_output, resolve_output_dir

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing.GraphHopper"

GRAPHHOPPER_URL = os.environ.get("FW_GRAPHHOPPER_URL", "http://localhost:8989")

# Facetwork profile -> GraphHopper profile name (depends on server config; these
# are the conventional defaults shipped with graphhopper-web).
_PROFILES = {
    "car": "car", "driving": "car", "auto": "car",
    "bike": "bike", "bicycle": "bike", "cycling": "bike",
    "foot": "foot", "walking": "foot", "pedestrian": "foot",
}


def _output_dir() -> str:
    d = resolve_output_dir("routing")
    os.makedirs(d, exist_ok=True)
    return d


def _profile(profile: str) -> str:
    return _PROFILES.get(profile, "car")


def _gh_get(endpoint: str, params, timeout: int = 30) -> dict | None:
    """GET a GraphHopper endpoint; return parsed JSON or None.

    ``params`` may repeat keys (e.g. multiple ``point=``), so it is a list of
    (key, value) pairs rather than a dict.
    """
    import requests

    url = f"{GRAPHHOPPER_URL}/{endpoint}"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("GraphHopper %s request to %s failed", endpoint, GRAPHHOPPER_URL)
        return None


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = r2 - r1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _handle_route(payload: dict) -> dict:
    """osm.Routing.GraphHopper.Route — point-to-point routing via /route."""
    from_lat, from_lon = payload.get("from_lat", 0), payload.get("from_lon", 0)
    to_lat, to_lon = payload.get("to_lat", 0), payload.get("to_lon", 0)
    from_name, to_name = payload.get("from_name", ""), payload.get("to_name", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"GraphHopper.Route: {from_name or 'origin'} -> {to_name or 'dest'} ({profile})")

    # GraphHopper takes point=lat,lon (lat first); points_encoded=false -> GeoJSON.
    data = _gh_get("route", [
        ("point", f"{from_lat},{from_lon}"), ("point", f"{to_lat},{to_lon}"),
        ("profile", _profile(profile)), ("points_encoded", "false"), ("calc_points", "true"),
    ])

    if data and data.get("paths"):
        path = data["paths"][0]
        distance_km = round(path.get("distance", 0) / 1000, 2)
        duration_min = round(path.get("time", 0) / 60000, 1)  # ms -> min
        coords = path.get("points", {}).get("coordinates", [[from_lon, from_lat], [to_lon, to_lat]])
        backend = "graphhopper"
    else:
        distance_km = round(_haversine_km(from_lon, from_lat, to_lon, to_lat) * 1.3, 2)
        duration_min = round(distance_km / 80 * 60, 1)
        coords = [[from_lon, from_lat], [to_lon, to_lat]]
        backend = "estimate"

    slug = f"{from_name or 'origin'}-{to_name or 'dest'}-{profile}".replace(" ", "_")
    output_path = os.path.join(_output_dir(), f"graphhopper-route-{slug}.geojson")
    ensure_dir(output_path)
    with open_output(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"from": from_name, "to": to_name, "distance_km": distance_km,
                           "duration_min": duration_min, "profile": profile}}]}, f)

    if step_log:
        step_log(f"GraphHopper.Route: {distance_km} km, {duration_min} min ({backend})", level="success")

    return {"result": {"route": {
        "from_name": from_name, "to_name": to_name, "distance_km": distance_km,
        "duration_min": duration_min, "output_path": output_path, "profile": profile,
        "backend": backend, "format": "GeoJSON"}, "waypoint_count": 2}}


def _handle_isochrone(payload: dict) -> dict:
    """osm.Routing.GraphHopper.Isochrone — reachability polygon via /isochrone."""
    center_lat, center_lon = payload.get("center_lat", 0), payload.get("center_lon", 0)
    center_name = payload.get("center_name", "")
    time_minutes = payload.get("time_minutes", 15)
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"GraphHopper.Isochrone: {center_name or 'center'}, {time_minutes}min ({profile})")

    data = _gh_get("isochrone", [
        ("point", f"{center_lat},{center_lon}"), ("time_limit", str(int(time_minutes) * 60)),
        ("profile", _profile(profile)), ("buckets", "1"),
    ])

    features = []
    backend = "none"
    if data and data.get("polygons"):
        for poly in data["polygons"]:
            geom = poly.get("geometry")
            if geom:
                features.append({"type": "Feature", "geometry": geom,
                                 "properties": {"center_name": center_name,
                                                "time_minutes": time_minutes, "profile": profile}})
        backend = "graphhopper" if features else "none"

    output_path = os.path.join(
        _output_dir(),
        f"graphhopper-isochrone-{center_name or 'center'}-{time_minutes}min-{profile}.geojson".replace(" ", "_"),
    )
    ensure_dir(output_path)
    with open_output(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)

    if step_log:
        step_log(f"GraphHopper.Isochrone: {time_minutes}min polygon ({backend})", level="success")

    return {"result": {"output_path": output_path, "center_name": center_name,
                       "time_minutes": time_minutes, "profile": profile,
                       "backend": backend, "format": "GeoJSON"}}


GRAPHHOPPER_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.Route": _handle_route,
    f"{NAMESPACE}.Isochrone": _handle_isochrone,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = GRAPHHOPPER_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown GraphHopper routing facet: {facet_name}")
    return handler(payload)
