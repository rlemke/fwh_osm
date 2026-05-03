"""API routing adapter — routes via public OSRM demo or OpenRouteService.

Zero-import routing: no local graph build needed. Uses the OSRM public
demo server by default. Set AFL_ORS_API_KEY to use OpenRouteService instead
(higher rate limits, isochrone support).

OSRM demo is rate-limited and for non-commercial use only. For production
batch workloads, use the OSRM local adapter or set up your own instance.

Environment:
    AFL_ROUTING_API_URL: OSRM-compatible routing endpoint
        (default: https://router.project-osrm.org)
    AFL_ORS_API_KEY: OpenRouteService API key (enables ORS backend)
    AFL_ORS_API_URL: ORS endpoint (default: https://api.openrouteservice.org)
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import UTC, datetime

from ..shared._output import resolve_output_dir

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing.API"

# OSRM demo (default — no key needed)
OSRM_API_URL = os.environ.get(
    "AFL_ROUTING_API_URL", "https://router.project-osrm.org"
)

# OpenRouteService (optional — needs API key)
ORS_API_KEY = os.environ.get("AFL_ORS_API_KEY", "")
ORS_API_URL = os.environ.get(
    "AFL_ORS_API_URL", "https://api.openrouteservice.org"
)

# Rate limiting: minimum seconds between API calls
_RATE_LIMIT_SECONDS = float(os.environ.get("AFL_ROUTING_RATE_LIMIT", "1.0"))
_last_request_time = 0.0

# OSRM profile mapping (OSRM uses specific profile names in URL path)
_OSRM_PROFILES = {
    "car": "driving",
    "driving": "driving",
    "bike": "cycling",
    "bicycle": "cycling",
    "cycling": "cycling",
    "foot": "foot",
    "walking": "foot",
}

# ORS profile mapping
_ORS_PROFILES = {
    "car": "driving-car",
    "driving": "driving-car",
    "bike": "cycling-regular",
    "bicycle": "cycling-regular",
    "cycling": "cycling-regular",
    "foot": "foot-walking",
    "walking": "foot-walking",
}


def _rate_limit() -> None:
    """Enforce rate limiting between API calls."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        time.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _output_dir() -> str:
    d = resolve_output_dir("routing")
    os.makedirs(d, exist_ok=True)
    return d


def _use_ors() -> bool:
    """Return True if ORS backend should be used."""
    return bool(ORS_API_KEY)


# ---------------------------------------------------------------------------
# OSRM API helpers
# ---------------------------------------------------------------------------


def _osrm_route(
    coords: list[tuple[float, float]], profile: str
) -> dict | None:
    """Query OSRM for a route. coords is list of (lon, lat) tuples."""
    import requests

    osrm_profile = _OSRM_PROFILES.get(profile, "driving")
    coord_str = ";".join(f"{lon},{lat}" for lon, lat in coords)
    url = f"{OSRM_API_URL}/route/v1/{osrm_profile}/{coord_str}"

    _rate_limit()
    try:
        resp = requests.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            log.warning("OSRM error: %s", data.get("message", data.get("code")))
            return None
        return data
    except Exception:
        log.exception("OSRM API request failed")
        return None


# ---------------------------------------------------------------------------
# OpenRouteService API helpers
# ---------------------------------------------------------------------------


def _ors_route(
    coords: list[tuple[float, float]], profile: str
) -> dict | None:
    """Query OpenRouteService for a route."""
    import requests

    ors_profile = _ORS_PROFILES.get(profile, "driving-car")
    url = f"{ORS_API_URL}/v2/directions/{ors_profile}/geojson"

    _rate_limit()
    try:
        resp = requests.post(
            url,
            json={"coordinates": [[lon, lat] for lon, lat in coords]},
            headers={
                "Authorization": ORS_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("ORS API request failed")
        return None


def _ors_isochrone(
    center: tuple[float, float], time_minutes: int, profile: str
) -> dict | None:
    """Query OpenRouteService for an isochrone polygon."""
    import requests

    ors_profile = _ORS_PROFILES.get(profile, "driving-car")
    url = f"{ORS_API_URL}/v2/isochrones/{ors_profile}"

    _rate_limit()
    try:
        resp = requests.post(
            url,
            json={
                "locations": [[center[0], center[1]]],
                "range": [time_minutes * 60],
                "range_type": "time",
            },
            headers={
                "Authorization": ORS_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        log.exception("ORS isochrone request failed")
        return None


# ---------------------------------------------------------------------------
# Great-circle fallback
# ---------------------------------------------------------------------------


def _estimate_route(
    from_lon: float, from_lat: float, to_lon: float, to_lat: float
) -> dict:
    """Haversine great-circle estimate when APIs are unavailable."""
    lat1, lat2 = math.radians(from_lat), math.radians(to_lat)
    dlat = lat2 - lat1
    dlon = math.radians(to_lon - from_lon)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance_km = 6371 * c * 1.3  # 1.3 road detour factor
    duration_min = distance_km / 80 * 60  # 80 km/h average
    return {
        "distance_km": round(distance_km, 2),
        "duration_min": round(duration_min, 1),
        "coordinates": [[from_lon, from_lat], [to_lon, to_lat]],
    }


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


def _write_route_geojson(
    coordinates: list, properties: dict, output_path: str
) -> None:
    """Write a route as a GeoJSON FeatureCollection."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "properties": properties,
            }
        ],
    }
    with open(output_path, "w") as f:
        json.dump(geojson, f)


def _handle_route(payload: dict) -> dict:
    """Handle osm.Routing.API.Route — point-to-point routing."""
    from_lat = payload.get("from_lat", 0)
    from_lon = payload.get("from_lon", 0)
    from_name = payload.get("from_name", "")
    to_lat = payload.get("to_lat", 0)
    to_lon = payload.get("to_lon", 0)
    to_name = payload.get("to_name", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        backend = "ORS" if _use_ors() else "OSRM"
        step_log(f"API.Route: {from_name or 'origin'} -> {to_name or 'dest'} ({profile}, {backend})")

    coords = [(from_lon, from_lat), (to_lon, to_lat)]
    distance_km = 0.0
    duration_min = 0.0
    route_coords = [list(c) for c in coords]

    if _use_ors():
        data = _ors_route(coords, profile)
        if data and data.get("features"):
            feat = data["features"][0]
            props = feat.get("properties", {}).get("summary", {})
            distance_km = round(props.get("distance", 0) / 1000, 2)
            duration_min = round(props.get("duration", 0) / 60, 1)
            route_coords = feat["geometry"]["coordinates"]
        else:
            est = _estimate_route(from_lon, from_lat, to_lon, to_lat)
            distance_km = est["distance_km"]
            duration_min = est["duration_min"]
            route_coords = est["coordinates"]
    else:
        data = _osrm_route(coords, profile)
        if data and data.get("routes"):
            r = data["routes"][0]
            distance_km = round(r.get("distance", 0) / 1000, 2)
            duration_min = round(r.get("duration", 0) / 60, 1)
            route_coords = r.get("geometry", {}).get("coordinates", route_coords)
        else:
            est = _estimate_route(from_lon, from_lat, to_lon, to_lat)
            distance_km = est["distance_km"]
            duration_min = est["duration_min"]
            route_coords = est["coordinates"]

    slug = f"{from_name or 'origin'}-{to_name or 'dest'}-{profile}".replace(" ", "_")
    output_path = os.path.join(_output_dir(), f"route-{slug}.geojson")

    _write_route_geojson(
        route_coords,
        {
            "from": from_name,
            "to": to_name,
            "distance_km": distance_km,
            "duration_min": duration_min,
            "profile": profile,
        },
        output_path,
    )

    backend = "openrouteservice" if _use_ors() else "osrm-api"
    if step_log:
        step_log(
            f"API.Route: {distance_km} km, {duration_min} min ({backend})",
            level="success",
        )

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
    """Handle osm.Routing.API.MultiStopRoute — ordered waypoint routing."""
    waypoints_raw = payload.get("waypoints", "")
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    # Parse waypoints: JSON array of {lon, lat, name} dicts or "lon,lat;lon,lat" string
    if isinstance(waypoints_raw, str):
        try:
            waypoints = json.loads(waypoints_raw)
        except json.JSONDecodeError:
            # Parse "lon,lat;lon,lat" format
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
        step_log(f"API.MultiStopRoute: {len(waypoints)} stops ({profile})")

    coords = [(w.get("lon", w.get("longitude", 0)), w.get("lat", w.get("latitude", 0))) for w in waypoints]

    all_features = []
    total_distance = 0.0
    total_duration = 0.0

    if _use_ors():
        data = _ors_route(coords, profile)
        if data and data.get("features"):
            feat = data["features"][0]
            props = feat.get("properties", {}).get("summary", {})
            total_distance = round(props.get("distance", 0) / 1000, 2)
            total_duration = round(props.get("duration", 0) / 60, 1)
            all_features.append(feat)
    else:
        data = _osrm_route(coords, profile)
        if data and data.get("routes"):
            r = data["routes"][0]
            total_distance = round(r.get("distance", 0) / 1000, 2)
            total_duration = round(r.get("duration", 0) / 60, 1)
            route_coords = r.get("geometry", {}).get("coordinates", [])
            # Split into legs
            for i, leg in enumerate(r.get("legs", [])):
                leg_dist = round(leg.get("distance", 0) / 1000, 2)
                leg_dur = round(leg.get("duration", 0) / 60, 1)
                fn = waypoints[i].get("name", f"stop-{i}")
                tn = waypoints[i + 1].get("name", f"stop-{i + 1}")
                all_features.append({
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

    # Fallback if no API data
    if not all_features:
        for i in range(len(coords) - 1):
            est = _estimate_route(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            total_distance += est["distance_km"]
            total_duration += est["duration_min"]
            fn = waypoints[i].get("name", f"stop-{i}")
            tn = waypoints[i + 1].get("name", f"stop-{i + 1}")
            all_features.append({
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

    output_path = os.path.join(_output_dir(), f"multi-stop-{len(waypoints)}pts-{profile}.geojson")
    geojson = {"type": "FeatureCollection", "features": all_features}
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    backend = "openrouteservice" if _use_ors() else "osrm-api"
    if step_log:
        step_log(
            f"API.MultiStopRoute: {len(waypoints) - 1} legs, {total_distance} km ({backend})",
            level="success",
        )

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
    """Handle osm.Routing.API.Isochrone — reachability polygon."""
    center_lat = payload.get("center_lat", 0)
    center_lon = payload.get("center_lon", 0)
    center_name = payload.get("center_name", "")
    time_minutes = payload.get("time_minutes", 15)
    profile = payload.get("profile", "car")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"API.Isochrone: {center_name or 'center'}, {time_minutes}min ({profile})")

    output_path = os.path.join(
        _output_dir(),
        f"isochrone-{center_name or 'center'}-{time_minutes}min-{profile}.geojson".replace(" ", "_"),
    )

    backend = "none"

    if _use_ors():
        data = _ors_isochrone((center_lon, center_lat), time_minutes, profile)
        if data and data.get("features"):
            backend = "openrouteservice"
            with open(output_path, "w") as f:
                json.dump(data, f)
        else:
            # Fallback: approximate circle
            backend = "estimate"
            _write_isochrone_estimate(center_lon, center_lat, time_minutes, profile, output_path)
    else:
        # OSRM demo doesn't support isochrones — generate estimate
        backend = "estimate"
        _write_isochrone_estimate(center_lon, center_lat, time_minutes, profile, output_path)

    if step_log:
        step_log(f"API.Isochrone: {time_minutes}min polygon ({backend})", level="success")

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


def _write_isochrone_estimate(
    lon: float, lat: float, time_minutes: int, profile: str, output_path: str
) -> None:
    """Write an approximate circular isochrone based on speed assumptions."""
    speed_kmh = {"car": 60, "driving": 60, "bike": 15, "foot": 5}.get(profile, 60)
    radius_km = speed_kmh * (time_minutes / 60)
    # Approximate circle as 32-point polygon
    points = []
    for i in range(33):
        angle = math.radians(i * (360 / 32))
        dlat = radius_km / 111.32 * math.cos(angle)
        dlon = radius_km / (111.32 * math.cos(math.radians(lat))) * math.sin(angle)
        points.append([lon + dlon, lat + dlat])

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [points]},
                "properties": {
                    "center": [lon, lat],
                    "time_minutes": time_minutes,
                    "radius_km": round(radius_km, 1),
                    "profile": profile,
                    "method": "estimate",
                },
            }
        ],
    }
    with open(output_path, "w") as f:
        json.dump(geojson, f)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

API_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.Route": _handle_route,
    f"{NAMESPACE}.MultiStopRoute": _handle_multi_stop,
    f"{NAMESPACE}.Isochrone": _handle_isochrone,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = API_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown API routing facet: {facet_name}")
    return handler(payload)
