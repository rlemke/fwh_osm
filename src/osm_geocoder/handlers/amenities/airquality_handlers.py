"""Air quality event facet handlers.

Handles the FetchAirQuality, CorrelateSchoolAirQuality, and ExposureStatistics
event facets defined in osmairquality.afl under osm.AirQuality namespace.

Fetches air quality readings from the OpenAQ v3 API, correlates schools with
their nearest sensor using haversine distance, and classifies exposure using
WHO PM2.5 thresholds.
"""

import json
import logging
import math
import os
from typing import Any

from facetwork.config import get_output_base

from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

NAMESPACE = "osm.AirQuality"

# OpenAQ API configuration
OPENAQ_API_URL = os.environ.get("OPENAQ_API_URL", "https://api.openaq.org/v3")
OPENAQ_API_KEY = os.environ.get("OPENAQ_API_KEY", "")

# WHO PM2.5 thresholds (ug/m3)
PM25_HIGH = 35.0
PM25_MEDIUM = 15.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in km between two points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _classify_exposure(pm25: float) -> str:
    """Classify PM2.5 exposure level using WHO thresholds."""
    if pm25 >= PM25_HIGH:
        return "high"
    elif pm25 >= PM25_MEDIUM:
        return "medium"
    else:
        return "low"


def handle_fetch_air_quality(payload: dict) -> dict:
    """Fetch air quality stations from OpenAQ within a bounding box.

    Params:
        bbox: Bounding box or region identifier string
        parameter: Air quality parameter (default "pm25")
        radius_m: Search radius in meters (default 25000)

    Returns:
        result: AirQualityResult dict
    """
    bbox = payload.get("bbox", "")
    parameter = payload.get("parameter", "pm25")
    radius_m = payload.get("radius_m", 25000)
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"FetchAirQuality: fetching {parameter} data for bbox {bbox}")

    # Cache check using bbox as the path key
    cache = {"path": bbox, "size": radius_m}
    cache_params = {"parameter": parameter, "radius_m": radius_m}
    hit = cached_result(f"{NAMESPACE}.FetchAirQuality", cache, cache_params, step_log)
    if hit is not None:
        return hit

    if not HAS_REQUESTS or not OPENAQ_API_KEY:
        log.warning("OpenAQ API key not set or requests not available; returning empty result")
        return {"result": _empty_air_quality_result(parameter)}

    try:
        # Parse bbox as "lat,lon,lat,lon" or use center coordinates
        headers = {"X-API-Key": OPENAQ_API_KEY}
        url = f"{OPENAQ_API_URL}/locations"
        params: dict[str, Any] = {
            "parameter": parameter,
            "radius": radius_m,
            "limit": 100,
        }

        # If bbox contains coordinates, use center point
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) == 4:
            try:
                lat = (float(parts[0]) + float(parts[2])) / 2
                lon = (float(parts[1]) + float(parts[3])) / 2
                params["coordinates"] = f"{lat},{lon}"
            except ValueError:
                pass

        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code != 200:
            log.error("OpenAQ API returned %d: %s", resp.status_code, resp.text[:200])
            return {"result": _empty_air_quality_result(parameter)}

        data = resp.json()
        results = data.get("results", [])

        # Convert to GeoJSON
        features = []
        values = []
        for loc in results:
            coords = loc.get("coordinates", {})
            lat = coords.get("latitude", 0)
            lon = coords.get("longitude", 0)

            # Find the matching parameter's latest value
            pm_value = None
            for p in loc.get("parameters", []):
                if p.get("parameter") == parameter or p.get("name") == parameter:
                    pm_value = p.get("lastValue", p.get("average"))
                    break

            if pm_value is not None:
                values.append(pm_value)
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                        "properties": {
                            "name": loc.get("name", "Unknown"),
                            "parameter": parameter,
                            "value": pm_value,
                            "unit": "µg/m³",
                            "location_id": loc.get("id"),
                        },
                    }
                )

        # Write GeoJSON
        output_dir = os.path.join(get_output_base(), "osm", "airquality")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"airquality-{parameter}.geojson")

        geojson = {"type": "FeatureCollection", "features": features}
        with open(output_path, "w") as f:
            json.dump(geojson, f)

        avg_value = sum(values) / len(values) if values else 0.0

        if step_log:
            step_log(
                f"FetchAirQuality: fetched {len(features)} {parameter} stations (avg {avg_value:.1f} µg/m³)",
                level="success",
            )
        rv = {
            "result": {
                "output_path": output_path,
                "station_count": len(features),
                "parameter": parameter,
                "avg_value": round(avg_value, 2),
                "unit": "µg/m³",
                "format": "GeoJSON",
            }
        }
        save_result_meta(f"{NAMESPACE}.FetchAirQuality", cache, cache_params, rv)
        return rv

    except Exception as exc:
        log.error("Failed to fetch air quality data: %s", exc)
        if step_log:
            step_log(f"FetchAirQuality: FAILED to fetch air quality data: {exc}", level="error")
        raise


def handle_correlate(payload: dict) -> dict:
    """Correlate schools with nearest air quality sensor.

    For each school, finds the nearest air quality station within
    max_distance_km and classifies the exposure level using WHO
    PM2.5 thresholds.

    Params:
        schools_path: Path to schools GeoJSON
        air_quality_path: Path to air quality stations GeoJSON
        max_distance_km: Maximum matching distance in km

    Returns:
        result: ExposureCorrelationResult dict
    """
    schools_path = payload.get("schools_path", "")
    air_quality_path = payload.get("air_quality_path", "")
    max_distance_km = payload.get("max_distance_km", 10.0)
    step_log = payload.get("_step_log")

    if step_log:
        step_log(
            f"CorrelateSchoolAirQuality: correlating schools with air quality (max {max_distance_km} km)"
        )

    if not schools_path or not air_quality_path:
        return {"result": _empty_correlation_result()}

    cache = {"path": schools_path, "size": _file_size(schools_path)}
    cache_params = {"air_quality_path": air_quality_path, "max_distance_km": max_distance_km}
    hit = cached_result(f"{NAMESPACE}.CorrelateSchoolAirQuality", cache, cache_params, step_log)
    if hit is not None:
        return hit

    try:
        with open(schools_path) as f:
            schools_data = json.load(f)
        with open(air_quality_path) as f:
            stations_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("Failed to load input files: %s", e)
        return {"result": _empty_correlation_result()}

    schools = schools_data.get("features", [])
    stations = stations_data.get("features", [])

    if not schools or not stations:
        return {"result": _empty_correlation_result()}

    # Correlate each school with nearest station
    correlated_features = []
    high = medium = low = 0
    pm25_values = []

    for school in schools:
        school_coords = school.get("geometry", {}).get("coordinates", [0, 0])
        school_lon, school_lat = school_coords[0], school_coords[1]
        school_props = school.get("properties", {})

        best_station = None
        best_distance = float("inf")
        best_value = None

        for station in stations:
            st_coords = station.get("geometry", {}).get("coordinates", [0, 0])
            st_lon, st_lat = st_coords[0], st_coords[1]
            dist = _haversine_km(school_lat, school_lon, st_lat, st_lon)

            if dist < best_distance and dist <= max_distance_km:
                best_distance = dist
                best_station = station.get("properties", {}).get("name", "Unknown")
                best_value = station.get("properties", {}).get("value")

        if best_value is not None:
            exposure = _classify_exposure(best_value)
            if exposure == "high":
                high += 1
            elif exposure == "medium":
                medium += 1
            else:
                low += 1
            pm25_values.append(best_value)

            correlated_features.append(
                {
                    "type": "Feature",
                    "geometry": school["geometry"],
                    "properties": {
                        **school_props,
                        "nearest_station": best_station,
                        "distance_km": round(best_distance, 2),
                        "pm25": best_value,
                        "exposure": exposure,
                    },
                }
            )

    # Write correlated GeoJSON
    output_dir = os.path.join(get_output_base(), "osm", "airquality")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "school-exposure.geojson")

    geojson = {"type": "FeatureCollection", "features": correlated_features}
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    matched = len(correlated_features)
    avg_pm25 = sum(pm25_values) / len(pm25_values) if pm25_values else 0.0

    if step_log:
        step_log(
            f"CorrelateSchoolAirQuality: {matched}/{len(schools)} schools matched (high={high}, medium={medium}, low={low})",
            level="success",
        )
    rv = {
        "result": {
            "output_path": output_path,
            "school_count": len(schools),
            "matched_count": matched,
            "high_exposure": high,
            "medium_exposure": medium,
            "low_exposure": low,
            "avg_pm25": round(avg_pm25, 2),
            "format": "GeoJSON",
        }
    }
    save_result_meta(f"{NAMESPACE}.CorrelateSchoolAirQuality", cache, cache_params, rv)
    return rv


def handle_exposure_stats(payload: dict) -> dict:
    """Compute aggregate exposure statistics from correlated GeoJSON.

    Params:
        input_path: Path to correlated school-exposure GeoJSON

    Returns:
        stats: ExposureStats dict
    """
    input_path = payload.get("input_path", "")
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"ExposureStatistics: computing exposure stats from {input_path}")

    if not input_path:
        return {"stats": _empty_stats()}

    cache = {"path": input_path, "size": _file_size(input_path)}
    hit = cached_result(f"{NAMESPACE}.ExposureStatistics", cache, {}, step_log)
    if hit is not None:
        return hit

    try:
        with open(input_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("Failed to load exposure data: %s", e)
        return {"stats": _empty_stats()}

    features = data.get("features", [])
    if not features:
        return {"stats": _empty_stats()}

    high = medium = low = 0
    pm25_values = []

    for feat in features:
        props = feat.get("properties", {})
        exposure = props.get("exposure", "")
        pm25 = props.get("pm25")

        if exposure == "high":
            high += 1
        elif exposure == "medium":
            medium += 1
        elif exposure == "low":
            low += 1

        if pm25 is not None:
            pm25_values.append(pm25)

    total = high + medium + low
    matched = len(pm25_values)

    if step_log:
        avg_pm = sum(pm25_values) / matched if matched else 0.0
        step_log(
            f"ExposureStatistics: {total} schools (high={high}, medium={medium}, low={low}, avg PM2.5={avg_pm:.1f})",
            level="success",
        )
    rv = {
        "stats": {
            "total_schools": total,
            "matched_schools": matched,
            "high_count": high,
            "medium_count": medium,
            "low_count": low,
            "high_pct": round(high / total * 100, 1) if total else 0.0,
            "medium_pct": round(medium / total * 100, 1) if total else 0.0,
            "low_pct": round(low / total * 100, 1) if total else 0.0,
            "avg_pm25": round(sum(pm25_values) / matched, 2) if matched else 0.0,
            "max_pm25": max(pm25_values) if pm25_values else 0.0,
            "min_pm25": min(pm25_values) if pm25_values else 0.0,
        }
    }
    save_result_meta(f"{NAMESPACE}.ExposureStatistics", cache, {}, rv)
    return rv


def _file_size(path: str) -> int:
    """Return file size or 0 if unavailable."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _empty_air_quality_result(parameter: str) -> dict:
    """Return an empty AirQualityResult."""
    return {
        "output_path": "",
        "station_count": 0,
        "parameter": parameter,
        "avg_value": 0.0,
        "unit": "µg/m³",
        "format": "GeoJSON",
    }


def _empty_correlation_result() -> dict:
    """Return an empty ExposureCorrelationResult."""
    return {
        "output_path": "",
        "school_count": 0,
        "matched_count": 0,
        "high_exposure": 0,
        "medium_exposure": 0,
        "low_exposure": 0,
        "avg_pm25": 0.0,
        "format": "GeoJSON",
    }


def _empty_stats() -> dict:
    """Return empty ExposureStats."""
    return {
        "total_schools": 0,
        "matched_schools": 0,
        "high_count": 0,
        "medium_count": 0,
        "low_count": 0,
        "high_pct": 0.0,
        "medium_pct": 0.0,
        "low_pct": 0.0,
        "avg_pm25": 0.0,
        "max_pm25": 0.0,
        "min_pm25": 0.0,
    }


# RegistryRunner dispatch adapter
_DISPATCH = {
    f"{NAMESPACE}.FetchAirQuality": handle_fetch_air_quality,
    f"{NAMESPACE}.CorrelateSchoolAirQuality": handle_correlate,
    f"{NAMESPACE}.ExposureStatistics": handle_exposure_stats,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_airquality_handlers(poller) -> None:
    """Register air quality handlers with the poller."""
    poller.register(
        f"{NAMESPACE}.FetchAirQuality",
        handle_fetch_air_quality,
    )
    poller.register(
        f"{NAMESPACE}.CorrelateSchoolAirQuality",
        handle_correlate,
    )
    poller.register(
        f"{NAMESPACE}.ExposureStatistics",
        handle_exposure_stats,
    )
    log.debug("Registered air quality handlers: %s.*", NAMESPACE)
