"""
Elevation enrichment and filtering handlers for OSM routes.

Uses Open-Elevation API (SRTM data) by default for elevation lookups.
"""

import json
import os
import time
from datetime import datetime
from typing import Any

import requests

# Conversion constants
METERS_TO_FEET = 3.28084
FEET_TO_METERS = 0.3048


def _get_elevation_batch(
    coordinates: list[tuple[float, float]], dem_source: str = "srtm"
) -> list[float]:
    """
    Get elevation for a batch of coordinates.

    Args:
        coordinates: List of (lat, lon) tuples
        dem_source: Elevation data source ("srtm" uses Open-Elevation API)

    Returns:
        List of elevations in meters
    """
    if not coordinates:
        return []

    if dem_source == "srtm":
        # Use Open-Elevation API (free, uses SRTM data)
        url = "https://api.open-elevation.com/api/v1/lookup"

        # API accepts batches, but limit to 100 at a time
        elevations = []
        batch_size = 100

        for i in range(0, len(coordinates), batch_size):
            batch = coordinates[i : i + batch_size]
            payload = {"locations": [{"latitude": lat, "longitude": lon} for lat, lon in batch]}

            try:
                response = requests.post(url, json=payload, timeout=30)
                response.raise_for_status()
                data = response.json()

                for result in data.get("results", []):
                    elevations.append(result.get("elevation", 0))

                # Rate limiting
                if i + batch_size < len(coordinates):
                    time.sleep(0.5)

            except requests.RequestException:
                # On error, fill with zeros
                elevations.extend([0] * len(batch))

        return elevations

    else:
        raise ValueError(f"Unknown DEM source: {dem_source}")


def _extract_coordinates_from_geometry(geometry: dict) -> list[tuple[float, float]]:
    """
    Extract (lat, lon) coordinates from a GeoJSON geometry.

    GeoJSON uses [lon, lat] order, we convert to (lat, lon) for elevation APIs.
    """
    coords = []
    geom_type = geometry.get("type", "")

    if geom_type == "Point":
        lon, lat = geometry["coordinates"][:2]
        coords.append((lat, lon))

    elif geom_type == "LineString":
        for point in geometry["coordinates"]:
            lon, lat = point[:2]
            coords.append((lat, lon))

    elif geom_type == "MultiLineString":
        for line in geometry["coordinates"]:
            for point in line:
                lon, lat = point[:2]
                coords.append((lat, lon))

    elif geom_type == "Polygon":
        for ring in geometry["coordinates"]:
            for point in ring:
                lon, lat = point[:2]
                coords.append((lat, lon))

    elif geom_type == "MultiPolygon":
        for polygon in geometry["coordinates"]:
            for ring in polygon:
                for point in ring:
                    lon, lat = point[:2]
                    coords.append((lat, lon))

    return coords


def _compute_elevation_stats(elevations_ft: list[float]) -> dict:
    """Compute elevation statistics from a list of elevations in feet."""
    if not elevations_ft:
        return {
            "min_elevation_ft": 0,
            "max_elevation_ft": 0,
            "elevation_gain_ft": 0,
            "elevation_loss_ft": 0,
            "avg_elevation_ft": 0,
            "points_sampled": 0,
        }

    min_elev = min(elevations_ft)
    max_elev = max(elevations_ft)
    avg_elev = sum(elevations_ft) / len(elevations_ft)

    # Compute gain and loss
    gain = 0
    loss = 0
    for i in range(1, len(elevations_ft)):
        diff = elevations_ft[i] - elevations_ft[i - 1]
        if diff > 0:
            gain += diff
        else:
            loss += abs(diff)

    return {
        "min_elevation_ft": round(min_elev, 1),
        "max_elevation_ft": round(max_elev, 1),
        "elevation_gain_ft": round(gain, 1),
        "elevation_loss_ft": round(loss, 1),
        "avg_elevation_ft": round(avg_elev, 1),
        "points_sampled": len(elevations_ft),
    }


def _enrich_feature_with_elevation(feature: dict, dem_source: str) -> dict:
    """Enrich a single GeoJSON feature with elevation data."""
    geometry = feature.get("geometry", {})
    properties = feature.get("properties", {})

    # Extract coordinates
    coords = _extract_coordinates_from_geometry(geometry)

    if not coords:
        return None

    # Get elevations
    elevations_m = _get_elevation_batch(coords, dem_source)
    elevations_ft = [e * METERS_TO_FEET for e in elevations_m]

    # Compute stats
    stats = _compute_elevation_stats(elevations_ft)

    # Build enriched route
    route_id = properties.get("@id", properties.get("id", str(hash(json.dumps(coords[:3])))))
    name = properties.get("name", "Unnamed")
    route_type = properties.get("route", properties.get("highway", "unknown"))
    network = properties.get("network", "*")

    return {
        "route_id": str(route_id),
        "name": name,
        "route_type": route_type,
        "network": network,
        "geometry": geometry,
        "elevation_profile": elevations_ft,
        "stats": stats,
        "properties": properties,  # Keep original properties
    }


def handle_enrich_with_elevation(params: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich GeoJSON routes with elevation data.

    Params:
        input_path: Path to GeoJSON file with routes
        dem_source: Elevation source ("srtm")
        sample_interval_m: Sampling interval in meters (not yet implemented)
    """
    input_path = params["input_path"]
    dem_source = params.get("dem_source", "srtm")
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"EnrichWithElevation: enriching {input_path} with {dem_source} elevation data")

    # Read input GeoJSON
    with open(input_path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    enriched_routes = []

    for feature in features:
        enriched = _enrich_feature_with_elevation(feature, dem_source)
        if enriched:
            enriched_routes.append(enriched)

    # Write output
    output_path = input_path.replace(".geojson", "_elevated.geojson")
    output_path = output_path.replace(".json", "_elevated.json")
    if output_path == input_path:
        output_path = input_path + "_elevated.geojson"

    # Create output GeoJSON with enriched properties
    output_features = []
    for route in enriched_routes:
        output_features.append(
            {
                "type": "Feature",
                "geometry": route["geometry"],
                "properties": {
                    **route["properties"],
                    "elevation_stats": route["stats"],
                    "elevation_profile": route["elevation_profile"],
                },
            }
        )

    output_geojson = {
        "type": "FeatureCollection",
        "features": output_features,
    }

    with open(output_path, "w") as f:
        json.dump(output_geojson, f)

    if step_log:
        step_log(
            f"EnrichWithElevation: enriched {len(enriched_routes)}/{len(features)} routes with elevation",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "routes": enriched_routes,
            "feature_count": len(features),
            "matched_count": len(enriched_routes),
            "filter_applied": "none",
            "elevation_source": dem_source,
            "extraction_date": datetime.now().isoformat(),
        }
    }


def handle_filter_by_max_elevation(params: dict[str, Any]) -> dict[str, Any]:
    """
    Filter routes where max elevation exceeds threshold.

    Params:
        input_path: Path to elevation-enriched GeoJSON
        min_max_elevation_ft: Minimum value for max elevation
    """
    input_path = params["input_path"]
    threshold_ft = params["min_max_elevation_ft"]
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"FilterByMaxElevation: filtering {input_path} above {threshold_ft} ft")

    with open(input_path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    matched = []

    for feature in features:
        props = feature.get("properties", {})
        stats = props.get("elevation_stats", {})
        max_elev = stats.get("max_elevation_ft", 0)

        if max_elev >= threshold_ft:
            matched.append(feature)

    # Write filtered output
    output_path = input_path.replace(".geojson", f"_above_{int(threshold_ft)}ft.geojson")
    output_path = output_path.replace(".json", f"_above_{int(threshold_ft)}ft.json")

    output_geojson = {
        "type": "FeatureCollection",
        "features": matched,
    }

    with open(output_path, "w") as f:
        json.dump(output_geojson, f)

    if step_log:
        step_log(
            f"FilterByMaxElevation: {len(matched)}/{len(features)} routes above {threshold_ft} ft",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "routes": matched,
            "feature_count": len(features),
            "matched_count": len(matched),
            "filter_applied": f"max_elevation >= {threshold_ft} ft",
            "elevation_source": "pre-enriched",
            "extraction_date": datetime.now().isoformat(),
        }
    }


def handle_filter_by_min_elevation(params: dict[str, Any]) -> dict[str, Any]:
    """
    Filter routes where min elevation is below threshold.

    Params:
        input_path: Path to elevation-enriched GeoJSON
        max_min_elevation_ft: Maximum value for min elevation
    """
    input_path = params["input_path"]
    threshold_ft = params["max_min_elevation_ft"]
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"FilterByMinElevation: filtering {input_path} below {threshold_ft} ft")

    with open(input_path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    matched = []

    for feature in features:
        props = feature.get("properties", {})
        stats = props.get("elevation_stats", {})
        min_elev = stats.get("min_elevation_ft", float("inf"))

        if min_elev <= threshold_ft:
            matched.append(feature)

    output_path = input_path.replace(".geojson", f"_below_{int(threshold_ft)}ft.geojson")
    output_path = output_path.replace(".json", f"_below_{int(threshold_ft)}ft.json")

    output_geojson = {
        "type": "FeatureCollection",
        "features": matched,
    }

    with open(output_path, "w") as f:
        json.dump(output_geojson, f)

    if step_log:
        step_log(
            f"FilterByMinElevation: {len(matched)}/{len(features)} routes below {threshold_ft} ft",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "routes": matched,
            "feature_count": len(features),
            "matched_count": len(matched),
            "filter_applied": f"min_elevation <= {threshold_ft} ft",
            "elevation_source": "pre-enriched",
            "extraction_date": datetime.now().isoformat(),
        }
    }


def handle_filter_by_elevation_gain(params: dict[str, Any]) -> dict[str, Any]:
    """
    Filter routes by total elevation gain.

    Params:
        input_path: Path to elevation-enriched GeoJSON
        min_gain_ft: Minimum elevation gain in feet
    """
    input_path = params["input_path"]
    min_gain_ft = params["min_gain_ft"]
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"FilterByElevationGain: filtering {input_path} for gain >= {min_gain_ft} ft")

    with open(input_path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    matched = []

    for feature in features:
        props = feature.get("properties", {})
        stats = props.get("elevation_stats", {})
        gain = stats.get("elevation_gain_ft", 0)

        if gain >= min_gain_ft:
            matched.append(feature)

    output_path = input_path.replace(".geojson", f"_gain_{int(min_gain_ft)}ft.geojson")
    output_path = output_path.replace(".json", f"_gain_{int(min_gain_ft)}ft.json")

    output_geojson = {
        "type": "FeatureCollection",
        "features": matched,
    }

    with open(output_path, "w") as f:
        json.dump(output_geojson, f)

    if step_log:
        step_log(
            f"FilterByElevationGain: {len(matched)}/{len(features)} routes with gain >= {min_gain_ft} ft",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "routes": matched,
            "feature_count": len(features),
            "matched_count": len(matched),
            "filter_applied": f"elevation_gain >= {min_gain_ft} ft",
            "elevation_source": "pre-enriched",
            "extraction_date": datetime.now().isoformat(),
        }
    }


def handle_filter_by_elevation_range(params: dict[str, Any]) -> dict[str, Any]:
    """
    Filter routes that pass through an elevation range.

    Params:
        input_path: Path to elevation-enriched GeoJSON
        min_elevation_ft: Lower bound of range
        max_elevation_ft: Upper bound of range
    """
    input_path = params["input_path"]
    min_elev = params["min_elevation_ft"]
    max_elev = params["max_elevation_ft"]
    step_log = params.get("_step_log")

    if step_log:
        step_log(
            f"FilterByElevationRange: filtering {input_path} for range {min_elev}-{max_elev} ft"
        )

    with open(input_path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    matched = []

    for feature in features:
        props = feature.get("properties", {})
        stats = props.get("elevation_stats", {})
        route_min = stats.get("min_elevation_ft", float("inf"))
        route_max = stats.get("max_elevation_ft", 0)

        # Route passes through range if it overlaps
        if route_min <= max_elev and route_max >= min_elev:
            matched.append(feature)

    output_path = input_path.replace(
        ".geojson", f"_range_{int(min_elev)}-{int(max_elev)}ft.geojson"
    )
    output_path = output_path.replace(".json", f"_range_{int(min_elev)}-{int(max_elev)}ft.json")

    output_geojson = {
        "type": "FeatureCollection",
        "features": matched,
    }

    with open(output_path, "w") as f:
        json.dump(output_geojson, f)

    if step_log:
        step_log(
            f"FilterByElevationRange: {len(matched)}/{len(features)} routes in range {min_elev}-{max_elev} ft",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "routes": matched,
            "feature_count": len(features),
            "matched_count": len(matched),
            "filter_applied": f"elevation in range [{min_elev}, {max_elev}] ft",
            "elevation_source": "pre-enriched",
            "extraction_date": datetime.now().isoformat(),
        }
    }


def handle_high_elevation_hiking_trails(params: dict[str, Any]) -> dict[str, Any]:
    """
    Combined: Extract hiking trails, enrich with elevation, filter by threshold.

    This is a convenience handler that chains:
    1. Extract hiking trails from OSM cache
    2. Enrich with elevation data
    3. Filter by max elevation threshold
    """
    _cache = params["cache"]
    min_elevation_ft = params.get("min_elevation_ft", 2000.0)
    _network = params.get("network", "*")
    dem_source = params.get("dem_source", "srtm")
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"HighElevationHikingTrails: filtering hiking trails above {min_elevation_ft} ft")

    # For now, we expect the cache to have a path to extracted data
    # In a full implementation, this would trigger the route extraction first

    # This handler expects pre-extracted route data
    # The workflow should chain: HikingTrails -> EnrichWithElevation -> FilterByMaxElevation

    return {
        "result": {
            "output_path": "",
            "routes": [],
            "feature_count": 0,
            "matched_count": 0,
            "filter_applied": f"hiking trails with max_elevation >= {min_elevation_ft} ft",
            "elevation_source": dem_source,
            "extraction_date": datetime.now().isoformat(),
            "note": "Use workflow chaining: HikingTrails -> EnrichWithElevation -> FilterByMaxElevation",
        }
    }


def handle_high_elevation_cycling_routes(params: dict[str, Any]) -> dict[str, Any]:
    """Combined: Extract cycling routes with elevation above threshold."""
    min_elevation_ft = params.get("min_elevation_ft", 2000.0)
    dem_source = params.get("dem_source", "srtm")
    step_log = params.get("_step_log")

    if step_log:
        step_log(
            f"HighElevationCyclingRoutes: filtering cycling routes above {min_elevation_ft} ft"
        )

    return {
        "result": {
            "output_path": "",
            "routes": [],
            "feature_count": 0,
            "matched_count": 0,
            "filter_applied": f"cycling routes with max_elevation >= {min_elevation_ft} ft",
            "elevation_source": dem_source,
            "extraction_date": datetime.now().isoformat(),
            "note": "Use workflow chaining: BicycleRoutes -> EnrichWithElevation -> FilterByMaxElevation",
        }
    }


def handle_high_elevation_routes(params: dict[str, Any]) -> dict[str, Any]:
    """Combined: Extract any route type with elevation above threshold."""
    route_type = params["route_type"]
    min_elevation_ft = params.get("min_elevation_ft", 2000.0)
    dem_source = params.get("dem_source", "srtm")
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"HighElevationRoutes: filtering {route_type} routes above {min_elevation_ft} ft")

    return {
        "result": {
            "output_path": "",
            "routes": [],
            "feature_count": 0,
            "matched_count": 0,
            "filter_applied": f"{route_type} routes with max_elevation >= {min_elevation_ft} ft",
            "elevation_source": dem_source,
            "extraction_date": datetime.now().isoformat(),
            "note": f"Use workflow chaining: ExtractRoutes(route_type={route_type}) -> EnrichWithElevation -> FilterByMaxElevation",
        }
    }


def handle_climbing_routes(params: dict[str, Any]) -> dict[str, Any]:
    """Combined: Extract routes with significant elevation gain."""
    route_type = params["route_type"]
    min_gain_ft = params.get("min_gain_ft", 1000.0)
    dem_source = params.get("dem_source", "srtm")
    step_log = params.get("_step_log")

    if step_log:
        step_log(f"ClimbingRoutes: filtering {route_type} routes with gain >= {min_gain_ft} ft")

    return {
        "result": {
            "output_path": "",
            "routes": [],
            "feature_count": 0,
            "matched_count": 0,
            "filter_applied": f"{route_type} routes with elevation_gain >= {min_gain_ft} ft",
            "elevation_source": dem_source,
            "extraction_date": datetime.now().isoformat(),
            "note": f"Use workflow chaining: ExtractRoutes(route_type={route_type}) -> EnrichWithElevation -> FilterByElevationGain",
        }
    }


# RegistryRunner dispatch adapter
_DISPATCH = {
    "osm.Elevation.EnrichWithElevation": handle_enrich_with_elevation,
    "osm.Elevation.FilterByMaxElevation": handle_filter_by_max_elevation,
    "osm.Elevation.FilterByMinElevation": handle_filter_by_min_elevation,
    "osm.Elevation.FilterByElevationGain": handle_filter_by_elevation_gain,
    "osm.Elevation.FilterByElevationRange": handle_filter_by_elevation_range,
    "osm.Elevation.HighElevationHikingTrails": handle_high_elevation_hiking_trails,
    "osm.Elevation.HighElevationCyclingRoutes": handle_high_elevation_cycling_routes,
    "osm.Elevation.HighElevationRoutes": handle_high_elevation_routes,
    "osm.Elevation.ClimbingRoutes": handle_climbing_routes,
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


def register_elevation_handlers(poller) -> None:
    """Register all elevation-related handlers with the poller."""
    handlers = {
        "osm.Elevation.EnrichWithElevation": handle_enrich_with_elevation,
        "osm.Elevation.FilterByMaxElevation": handle_filter_by_max_elevation,
        "osm.Elevation.FilterByMinElevation": handle_filter_by_min_elevation,
        "osm.Elevation.FilterByElevationGain": handle_filter_by_elevation_gain,
        "osm.Elevation.FilterByElevationRange": handle_filter_by_elevation_range,
        "osm.Elevation.HighElevationHikingTrails": handle_high_elevation_hiking_trails,
        "osm.Elevation.HighElevationCyclingRoutes": handle_high_elevation_cycling_routes,
        "osm.Elevation.HighElevationRoutes": handle_high_elevation_routes,
        "osm.Elevation.ClimbingRoutes": handle_climbing_routes,
    }

    for facet_name, handler in handlers.items():
        poller.register(facet_name, handler)
