"""Pairwise routing event facet handlers.

Handles the ComputePairwiseRoutes event facet defined in osmcityrouting.afl
under osm.Routing namespace. Computes all-pairs shortest-path driving
routes between cities using pgRouting (preferred) or GraphHopper (legacy).
"""

import json
import logging
import os
from itertools import combinations
from typing import Any

from facetwork.config import get_output_base

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing"

# GraphHopper API endpoint (local instance) — legacy fallback
GRAPHHOPPER_API_URL = os.environ.get("GRAPHHOPPER_API_URL", "http://localhost:8989")

try:
    import psycopg2

    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    psycopg2 = None


def _load_cities_geojson(cities_path: str) -> list[dict[str, Any]]:
    """Load city features from a GeoJSON file.

    Returns a list of feature dicts with name, population, and coordinates.
    """
    with open(cities_path) as f:
        data = json.load(f)

    cities = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [0, 0])

        name = props.get("name", "Unknown")
        population = props.get("population", 0)
        if isinstance(population, str):
            try:
                population = int(population)
            except ValueError:
                population = 0

        cities.append(
            {
                "name": name,
                "population": population,
                "lon": coords[0],
                "lat": coords[1],
            }
        )

    return cities


def _query_graphhopper_route(
    from_city: dict,
    to_city: dict,
    graph_dir: str,
    profile: str = "car",
) -> dict[str, Any] | None:
    """Query GraphHopper for a route between two cities.

    Tries the GraphHopper HTTP API first (for running instances),
    falls back to a simple great-circle estimate if unavailable.

    Returns dict with distance_km, duration_min, and geometry coordinates.
    """
    try:
        import requests

        url = f"{GRAPHHOPPER_API_URL}/route"
        params = {
            "point": [
                f"{from_city['lat']},{from_city['lon']}",
                f"{to_city['lat']},{to_city['lon']}",
            ],
            "profile": profile,
            "type": "json",
            "points_encoded": "false",
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            path = data.get("paths", [{}])[0]
            coords = path.get("points", {}).get("coordinates", [])
            return {
                "distance_km": round(path.get("distance", 0) / 1000, 1),
                "duration_min": round(path.get("time", 0) / 60000, 1),
                "coordinates": coords,
            }
    except Exception as exc:
        log.debug("GraphHopper API unavailable, falling back to estimate: %s", exc)

    # Fallback: great-circle distance estimate (no actual routing)
    return _estimate_route(from_city, to_city)


def _estimate_route(from_city: dict, to_city: dict) -> dict[str, Any]:
    """Estimate route distance and duration using great-circle distance."""
    import math

    lat1 = math.radians(from_city["lat"])
    lat2 = math.radians(to_city["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(to_city["lon"] - from_city["lon"])

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    distance_km = 6371 * c

    # Estimate driving time at 80 km/h average with 1.3 detour factor
    road_distance = distance_km * 1.3
    duration_min = road_distance / 80 * 60

    return {
        "distance_km": round(road_distance, 1),
        "duration_min": round(duration_min, 1),
        "coordinates": [
            [from_city["lon"], from_city["lat"]],
            [to_city["lon"], to_city["lat"]],
        ],
    }


def _query_pgrouting_route(
    from_city: dict,
    to_city: dict,
    postgis_url: str,
    prefix: str,
    profile: str = "car",
) -> dict[str, Any] | None:
    """Query pgRouting for a route between two cities.

    Finds the nearest vertices in the routing topology, runs pgr_dijkstra,
    and reconstructs the path geometry from the matched edges.

    Returns dict with distance_km, duration_min, and geometry coordinates.
    """
    if not HAS_PSYCOPG2:
        return None

    # Speed assumptions by profile (km/h) for duration estimation
    profile_speeds = {"car": 80, "bike": 20, "foot": 5}
    avg_speed = profile_speeds.get(profile, 80)

    try:
        conn = psycopg2.connect(postgis_url, gssencmode="disable")
        try:
            with conn.cursor() as cur:
                # Find nearest source vertex
                cur.execute(
                    f"SELECT id FROM {prefix}_ways_vertices_pgr "
                    "ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) "
                    "LIMIT 1",
                    (from_city["lon"], from_city["lat"]),
                )
                src_row = cur.fetchone()

                # Find nearest target vertex
                cur.execute(
                    f"SELECT id FROM {prefix}_ways_vertices_pgr "
                    "ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) "
                    "LIMIT 1",
                    (to_city["lon"], to_city["lat"]),
                )
                tgt_row = cur.fetchone()

                if not src_row or not tgt_row:
                    return None

                src_id, tgt_id = src_row[0], tgt_row[0]

                # Run pgr_dijkstra and reconstruct path geometry
                cur.execute(
                    f"SELECT "
                    f"  ST_AsGeoJSON(ST_LineMerge(ST_Union(w.the_geom))) AS geom, "
                    f"  SUM(w.length_m) AS total_length_m "
                    f"FROM pgr_dijkstra("
                    f"  'SELECT gid AS id, source, target, length_m AS cost, "
                    f"   length_m AS reverse_cost FROM {prefix}_ways', "
                    f"  %s, %s, directed := false"
                    f") d "
                    f"JOIN {prefix}_ways w ON d.edge = w.gid",
                    (src_id, tgt_id),
                )
                row = cur.fetchone()

                if not row or not row[0]:
                    return None

                geom_json = json.loads(row[0])
                total_m = row[1] or 0
                distance_km = round(total_m / 1000, 1)
                duration_min = round(distance_km / avg_speed * 60, 1)

                coords = geom_json.get("coordinates", [])
                return {
                    "distance_km": distance_km,
                    "duration_min": duration_min,
                    "coordinates": coords,
                }
        finally:
            conn.close()
    except Exception as exc:
        log.exception("pgRouting query failed: %s -> %s", from_city["name"], to_city["name"])
        raise


def compute_pairwise_routes(payload: dict) -> dict:
    """Compute all-pairs driving routes between cities in a GeoJSON file.

    Reads cities from the GeoJSON, iterates all unique pairs, queries
    pgRouting (preferred) or GraphHopper (legacy) for each route, and
    writes a GeoJSON with LineString geometries.

    Params:
        cities_path: Path to GeoJSON file with city features
        topology: RoutingTopology dict (pgRouting) — preferred
        graph: GraphHopperCache dict (legacy fallback)

    Returns:
        result: PairwiseRoutingResult dict
    """
    cities_path = payload.get("cities_path", "")
    topology = payload.get("topology", {})
    graph = payload.get("graph", {})
    step_log = payload.get("_step_log")

    # Determine routing backend
    use_pgrouting = bool(topology.get("region"))
    if use_pgrouting:
        from ..downloads.postgis_importer import get_postgis_url

        postgis_url = topology.get("postgisUrl") or get_postgis_url()
        # Rewrite sanitized URL back to real URL for connection
        if "***" in postgis_url:
            postgis_url = get_postgis_url()
        region = topology.get("region", "")
        profile = topology.get("profile", "car")
        import re

        prefix = re.sub(r"[^a-z0-9]", "_", region.lower().strip())
    else:
        profile = graph.get("profile", "car")

    if step_log:
        backend = "pgRouting" if use_pgrouting else "GraphHopper"
        step_log(
            f"ComputePairwiseRoutes: computing routes from {cities_path} ({profile} profile, {backend})"
        )
    if not cities_path or not os.path.exists(cities_path):
        return {"result": _empty_result(profile)}

    cities = _load_cities_geojson(cities_path)
    if len(cities) < 2:
        return {"result": _empty_result(profile)}

    # Compute all-pairs routes
    features = []
    total_distance = 0.0
    total_duration = 0.0

    for city_a, city_b in combinations(cities, 2):
        if use_pgrouting:
            route = _query_pgrouting_route(city_a, city_b, postgis_url, prefix, profile)
        else:
            route = _query_graphhopper_route(city_a, city_b, graph.get("graphDir", ""), profile)

        # Fallback to great-circle estimate if routing engine unavailable
        if route is None:
            route = _estimate_route(city_a, city_b)

        total_distance += route["distance_km"]
        total_duration += route["duration_min"]

        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": route["coordinates"],
                },
                "properties": {
                    "from": city_a["name"],
                    "to": city_b["name"],
                    "distance_km": route["distance_km"],
                    "duration_min": route["duration_min"],
                    "profile": profile,
                },
            }
        )

    # Write output GeoJSON
    output_dir = os.path.join(get_output_base(), "osm", "routing")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(
        output_dir,
        f"pairwise-routes-{len(cities)}cities-{profile}.geojson",
    )

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    with open(output_path, "w") as f:
        json.dump(geojson, f)

    route_count = len(features)
    if step_log:
        step_log(
            f"ComputePairwiseRoutes: {route_count} routes between {len(cities)} cities ({round(total_distance)} km total)",
            level="success",
        )
    return {
        "result": {
            "output_path": output_path,
            "route_count": route_count,
            "city_count": len(cities),
            "total_distance_km": round(total_distance),
            "total_duration_min": round(total_duration),
            "avg_distance_km": round(total_distance / route_count) if route_count else 0,
            "avg_duration_min": round(total_duration / route_count) if route_count else 0,
            "profile": profile,
            "format": "GeoJSON",
        },
    }


def _empty_result(profile: str) -> dict:
    """Return an empty PairwiseRoutingResult."""
    return {
        "output_path": "",
        "route_count": 0,
        "city_count": 0,
        "total_distance_km": 0,
        "total_duration_min": 0,
        "avg_distance_km": 0,
        "avg_duration_min": 0,
        "profile": profile,
        "format": "GeoJSON",
    }


# RegistryRunner dispatch adapter
_DISPATCH = {
    f"{NAMESPACE}.ComputePairwiseRoutes": compute_pairwise_routes,
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


def register_routing_handlers(poller) -> None:
    """Register pairwise routing handlers with the poller."""
    poller.register(
        f"{NAMESPACE}.ComputePairwiseRoutes",
        compute_pairwise_routes,
    )
    log.debug("Registered routing handler: %s.ComputePairwiseRoutes", NAMESPACE)
