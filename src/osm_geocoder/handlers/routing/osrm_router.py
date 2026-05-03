"""OSRM local routing adapter — routes via a self-hosted OSRM instance.

Requires a running OSRM server with a pre-processed region. OSRM processes
a single country PBF in minutes to hours (vs months for full planet PostGIS
import), making it practical for regional routing.

Setup (one-time per region):
    osrm-extract -p profiles/car.lua region.osm.pbf
    osrm-partition region.osrm
    osrm-customize region.osrm
    osrm-routed --algorithm mld region.osrm

Or via Docker:
    docker run -t -v data:/data ghcr.io/project-osrm/osrm-backend \\
        osrm-extract -p /opt/car.lua /data/region.osm.pbf
    docker run -t -v data:/data ghcr.io/project-osrm/osrm-backend \\
        osrm-partition /data/region.osrm
    docker run -t -v data:/data ghcr.io/project-osrm/osrm-backend \\
        osrm-customize /data/region.osrm
    docker run -t -v data:/data -p 5000:5000 ghcr.io/project-osrm/osrm-backend \\
        osrm-routed --algorithm mld /data/region.osrm

Environment:
    AFL_OSRM_URL: OSRM server URL (default: http://localhost:5000)
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime

from ..shared._output import resolve_output_dir

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing.OSRM"

OSRM_URL = os.environ.get("AFL_OSRM_URL", "http://localhost:5000")

# OSRM profile mapping
_OSRM_PROFILES = {
    "car": "driving",
    "driving": "driving",
    "bike": "cycling",
    "bicycle": "cycling",
    "cycling": "cycling",
    "foot": "foot",
    "walking": "foot",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _output_dir() -> str:
    d = resolve_output_dir("routing")
    os.makedirs(d, exist_ok=True)
    return d


def _osrm_request(endpoint: str, coords: list[tuple[float, float]], profile: str, **params) -> dict | None:
    """Make a request to the local OSRM server."""
    import requests

    osrm_profile = _OSRM_PROFILES.get(profile, "driving")
    coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = f"{OSRM_URL}/{endpoint}/v1/{osrm_profile}/{coord_str}"

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            log.warning("OSRM %s error: %s", endpoint, data.get("message", data.get("code")))
            return None
        return data
    except Exception:
        log.exception("OSRM %s request to %s failed", endpoint, OSRM_URL)
        return None


def _estimate_route(from_lon: float, from_lat: float, to_lon: float, to_lat: float) -> dict:
    """Haversine great-circle fallback."""
    lat1, lat2 = math.radians(from_lat), math.radians(to_lat)
    dlat = lat2 - lat1
    dlon = math.radians(to_lon - from_lon)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance_km = 6371 * c * 1.3
    duration_min = distance_km / 80 * 60
    return {
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min, 1),
        "coordinates": [[from_lon, from_lat], [to_lon, to_lat]],
    }


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


def _handle_route(payload: dict) -> dict:
    """Handle osm.Routing.OSRM.Route — point-to-point routing via local OSRM."""
    from_lat = payload.get("from_lat", 0)
    from_lon = payload.get("from_lon", 0)
    from_name = payload.get("from_name", "")
    to_lat = payload.get("to_lat", 0)
    to_lon = payload.get("to_lon", 0)
    to_name = payload.get("to_name", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"OSRM.Route: {from_name or 'origin'} -> {to_name or 'dest'} ({profile})")

    coords = [(from_lon, from_lat), (to_lon, to_lat)]
    data = _osrm_request("route", coords, profile, overview="full", geometries="geojson", steps="false")

    if data and data.get("routes"):
        r = data["routes"][0]
        distance_km = round(r.get("distance", 0) / 1000, 2)
        duration_min = round(r.get("duration", 0) / 60, 1)
        route_coords = r.get("geometry", {}).get("coordinates", [list(c) for c in coords])
        backend = "osrm-local"
    else:
        est = _estimate_route(from_lon, from_lat, to_lon, to_lat)
        distance_km = est["distance_km"]
        duration_min = est["duration_min"]
        route_coords = est["coordinates"]
        backend = "estimate"

    slug = f"{from_name or 'origin'}-{to_name or 'dest'}-{profile}".replace(" ", "_")
    output_path = os.path.join(_output_dir(), f"osrm-route-{slug}.geojson")

    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": route_coords},
            "properties": {
                "from": from_name,
                "to": to_name,
                "distance_km": distance_km,
                "duration_min": duration_min,
                "profile": profile,
            },
        }],
    }
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    if step_log:
        step_log(f"OSRM.Route: {distance_km} km, {duration_min} min ({backend})", level="success")

    return {
        "result": {
            "route": {
                "from_name": from_name,
                "to_name": to_name,
                "distance_km": distance_km,
                "duration_min": duration_min,
                "output_path": output_path,
                "profile": profile,
                "backend": backend,
                "format": "GeoJSON",
            },
            "waypoint_count": 2,
        }
    }


# ---------------------------------------------------------------------------
# Multi-stop handler
# ---------------------------------------------------------------------------


def _handle_multi_stop(payload: dict) -> dict:
    """Handle osm.Routing.OSRM.MultiStopRoute — ordered waypoint routing."""
    waypoints_raw = payload.get("waypoints", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if isinstance(waypoints_raw, str):
        try:
            waypoints = json.loads(waypoints_raw)
        except json.JSONDecodeError:
            waypoints = []
            for pair in waypoints_raw.split(";"):
                parts = pair.strip().split(",")
                if len(parts) >= 2:
                    waypoints.append({"lon": float(parts[0]), "lat": float(parts[1]), "name": ""})
    elif isinstance(waypoints_raw, list):
        waypoints = waypoints_raw
    else:
        waypoints = []

    if len(waypoints) < 2:
        return {"result": _empty_multi_stop(profile)}

    if step_log:
        step_log(f"OSRM.MultiStopRoute: {len(waypoints)} stops ({profile})")

    coords = [(w.get("lon", 0), w.get("lat", 0)) for w in waypoints]
    data = _osrm_request("route", coords, profile, overview="full", geometries="geojson", steps="false")

    features = []
    total_distance = 0.0
    total_duration = 0.0

    if data and data.get("routes"):
        r = data["routes"][0]
        total_distance = round(r.get("distance", 0) / 1000, 2)
        total_duration = round(r.get("duration", 0) / 60, 1)
        route_coords = r.get("geometry", {}).get("coordinates", [])
        for i, leg in enumerate(r.get("legs", [])):
            leg_dist = round(leg.get("distance", 0) / 1000, 2)
            leg_dur = round(leg.get("duration", 0) / 60, 1)
            fn = waypoints[i].get("name", f"stop-{i}")
            tn = waypoints[i + 1].get("name", f"stop-{i + 1}")
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": route_coords},
                "properties": {
                    "leg": i + 1,
                    "from": fn,
                    "to": tn,
                    "distance_km": leg_dist,
                    "duration_min": leg_dur,
                },
            })
        backend = "osrm-local"
    else:
        for i in range(len(coords) - 1):
            est = _estimate_route(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            total_distance += est["distance_km"]
            total_duration += est["duration_min"]
            fn = waypoints[i].get("name", f"stop-{i}")
            tn = waypoints[i + 1].get("name", f"stop-{i + 1}")
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": est["coordinates"]},
                "properties": {
                    "leg": i + 1,
                    "from": fn,
                    "to": tn,
                    "distance_km": est["distance_km"],
                    "duration_min": est["duration_min"],
                },
            })
        backend = "estimate"

    output_path = os.path.join(_output_dir(), f"osrm-multi-{len(waypoints)}pts-{profile}.geojson")
    geojson = {"type": "FeatureCollection", "features": features}
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    if step_log:
        step_log(f"OSRM.MultiStopRoute: {len(waypoints) - 1} legs, {total_distance} km ({backend})", level="success")

    return {
        "result": {
            "output_path": output_path,
            "leg_count": len(waypoints) - 1,
            "total_distance_km": total_distance,
            "total_duration_min": total_duration,
            "profile": profile,
            "backend": backend,
            "format": "GeoJSON",
        }
    }


def _empty_multi_stop(profile: str) -> dict:
    return {
        "output_path": "",
        "leg_count": 0,
        "total_distance_km": 0.0,
        "total_duration_min": 0.0,
        "profile": profile,
        "backend": "none",
        "format": "GeoJSON",
    }


# ---------------------------------------------------------------------------
# Isochrone handler
# ---------------------------------------------------------------------------


def _handle_isochrone(payload: dict) -> dict:
    """Handle osm.Routing.OSRM.Isochrone — approximate via OSRM table endpoint."""
    center_lat = payload.get("center_lat", 0)
    center_lon = payload.get("center_lon", 0)
    center_name = payload.get("center_name", "")
    time_minutes = payload.get("time_minutes", 15)
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"OSRM.Isochrone: {center_name or 'center'}, {time_minutes}min ({profile})")

    # OSRM doesn't have a native isochrone endpoint.
    # We sample points in a grid, use the OSRM table endpoint to get travel
    # times, then build a polygon from reachable points.
    speed_kmh = {"car": 80, "driving": 80, "bike": 18, "foot": 5}.get(profile, 80)
    max_radius_km = speed_kmh * (time_minutes / 60) * 1.2  # slightly overshoot

    # Generate sample points in concentric rings
    sample_points = [(center_lon, center_lat)]
    for ring in range(1, 5):
        radius_km = max_radius_km * ring / 4
        n_points = 16 * ring
        for i in range(n_points):
            angle = math.radians(i * (360 / n_points))
            dlat = radius_km / 111.32 * math.cos(angle)
            dlon = radius_km / (111.32 * math.cos(math.radians(center_lat))) * math.sin(angle)
            sample_points.append((center_lon + dlon, center_lat + dlat))

    # Query OSRM table for travel times from center to all sample points
    data = _osrm_request(
        "table", sample_points, profile,
        sources="0",
        annotations="duration",
    )

    reachable_points = []
    if data and data.get("durations"):
        durations = data["durations"][0]  # from center to all destinations
        threshold_seconds = time_minutes * 60
        for i, dur in enumerate(durations):
            if dur is not None and dur <= threshold_seconds:
                reachable_points.append(sample_points[i])
        backend = "osrm-local"
    else:
        backend = "estimate"

    if len(reachable_points) < 3:
        # Fallback: circular estimate
        _write_isochrone_circle(center_lon, center_lat, time_minutes, profile, backend)
        reachable_points = _circle_points(center_lon, center_lat, max_radius_km * 0.8)
        backend = "estimate"

    # Build convex hull polygon from reachable points
    hull = _convex_hull(reachable_points)
    hull.append(hull[0])  # close the ring

    output_path = os.path.join(
        _output_dir(),
        f"osrm-isochrone-{center_name or 'center'}-{time_minutes}min-{profile}.geojson".replace(" ", "_"),
    )
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [hull]},
            "properties": {
                "center": [center_lon, center_lat],
                "center_name": center_name,
                "time_minutes": time_minutes,
                "sample_points": len(sample_points),
                "reachable_points": len(reachable_points),
                "profile": profile,
            },
        }],
    }
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    if step_log:
        step_log(f"OSRM.Isochrone: {time_minutes}min polygon ({backend})", level="success")

    return {
        "result": {
            "output_path": output_path,
            "center_name": center_name,
            "time_minutes": time_minutes,
            "profile": profile,
            "backend": backend,
            "format": "GeoJSON",
        }
    }


def _circle_points(lon: float, lat: float, radius_km: float) -> list[list[float]]:
    """Generate circle polygon points."""
    points = []
    for i in range(32):
        angle = math.radians(i * (360 / 32))
        dlat = radius_km / 111.32 * math.cos(angle)
        dlon = radius_km / (111.32 * math.cos(math.radians(lat))) * math.sin(angle)
        points.append([lon + dlon, lat + dlat])
    return points


def _write_isochrone_circle(lon: float, lat: float, time_minutes: int, profile: str, backend: str) -> None:
    """Unused — kept as reference for circular fallback."""
    pass


def _convex_hull(points: list[tuple[float, float] | list[float]]) -> list[list[float]]:
    """Compute convex hull using Graham scan. Returns list of [lon, lat]."""
    pts = sorted([list(p) for p in points])
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

OSRM_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.Route": _handle_route,
    f"{NAMESPACE}.MultiStopRoute": _handle_multi_stop,
    f"{NAMESPACE}.Isochrone": _handle_isochrone,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = OSRM_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown OSRM routing facet: {facet_name}")
    return handler(payload)
