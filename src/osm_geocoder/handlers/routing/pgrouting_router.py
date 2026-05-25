"""pgRouting routing adapter — routes via PostGIS + the pgrouting extension.

The fourth swappable routing engine (alongside OSRM / Valhalla / GraphHopper),
reusing the same ``osm.Routing.Types`` schemas. Unlike the HTTP engines, pgRouting
runs SQL functions (``pgr_dijkstra`` / ``pgr_dijkstraCostMatrix`` /
``pgr_drivingDistance``) against a routable topology — the osm2pgrouting tables
``<prefix>ways`` (gid, source, target, length_m, the_geom) and
``<prefix>ways_vertices_pgr`` (id, the_geom). Points are snapped to the nearest
network vertex; routes optimize distance (``length_m``) with duration estimated
from a per-profile speed.

The DB work lives in mockable ``_pg_*`` helpers; every verb degrades to a
great-circle estimate when psycopg2 / the DB / the topology is unavailable (the
Route philosophy).

Setup (one-time per region): osm2pgrouting -f region.osm.pbf -d <db> ... then
``CREATE EXTENSION pgrouting;``. Environment:
    AFL_POSTGIS_URL: PostgreSQL DSN (default postgresql://afl:afl@afl-postgres:5432/afl_gis)
    AFL_PGROUTING_PREFIX: table-name prefix (default "" -> ways / ways_vertices_pgr)
"""

from __future__ import annotations

import json
import logging
import math
import os

from ..shared._output import resolve_output_dir

log = logging.getLogger(__name__)

NAMESPACE = "osm.Routing.PgRouting"

try:
    import psycopg2

    HAS_PSYCOPG2 = True
except ImportError:  # pragma: no cover
    psycopg2 = None
    HAS_PSYCOPG2 = False

POSTGIS_URL = os.environ.get("AFL_POSTGIS_URL", "postgresql://afl:afl@afl-postgres:5432/afl_gis")
_PREFIX = os.environ.get("AFL_PGROUTING_PREFIX", "")

# Profile -> assumed speed (km/h) for duration estimation over distance-optimal routes.
_SPEED_KMH = {"car": 80, "driving": 80, "auto": 80,
              "bike": 20, "bicycle": 20, "cycling": 20,
              "foot": 5, "walking": 5, "pedestrian": 5}


def _output_dir() -> str:
    d = resolve_output_dir("routing")
    os.makedirs(d, exist_ok=True)
    return d


def _speed(profile: str) -> float:
    return _SPEED_KMH.get(profile, 80)


def _ways(prefix: str) -> str:
    return f"{prefix}ways"


def _vertices(prefix: str) -> str:
    return f"{prefix}ways_vertices_pgr"


def _edges_sql(prefix: str) -> str:
    return (f"SELECT gid AS id, source, target, length_m AS cost, "
            f"length_m AS reverse_cost FROM {_ways(prefix)}")


def _haversine_km(lon1, lat1, lon2, lat2) -> float:
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = r2 - r1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _connect():
    return psycopg2.connect(POSTGIS_URL, gssencmode="disable",
                            options="-c default_transaction_read_only=on")


def _nearest_vertex(cur, prefix: str, lon: float, lat: float):
    cur.execute(
        f"SELECT id FROM {_vertices(prefix)} "
        "ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326) LIMIT 1",
        (lon, lat),
    )
    row = cur.fetchone()
    return row[0] if row else None


# --- DB query helpers (mockable; return parsed results or None) ----------------


def _pg_route(from_lon, from_lat, to_lon, to_lat, prefix) -> dict | None:
    """Shortest path via pgr_dijkstra -> {distance_km, coordinates} or None."""
    if not HAS_PSYCOPG2:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                src = _nearest_vertex(cur, prefix, from_lon, from_lat)
                tgt = _nearest_vertex(cur, prefix, to_lon, to_lat)
                if src is None or tgt is None:
                    return None
                cur.execute(
                    "SELECT ST_AsGeoJSON(ST_LineMerge(ST_Union(w.the_geom))), SUM(w.length_m) "
                    f"FROM pgr_dijkstra(%s, %s, %s, directed := false) d "
                    f"JOIN {_ways(prefix)} w ON d.edge = w.gid",
                    (_edges_sql(prefix), src, tgt),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                return {"distance_km": round((row[1] or 0) / 1000, 2),
                        "coordinates": json.loads(row[0]).get("coordinates", [])}
        finally:
            conn.close()
    except Exception:
        log.exception("pgRouting route query failed")
        return None


def _pg_matrix(points, prefix) -> tuple[list, list] | None:
    """NxN distance matrix (meters) via pgr_dijkstraCostMatrix; durations derived
    by the caller. Returns (vids_in_order, agg_cost_matrix_meters) or None."""
    if not HAS_PSYCOPG2:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                vids = []
                for p in points:
                    vid = _nearest_vertex(cur, prefix, p.get("lon", 0), p.get("lat", 0))
                    if vid is None:
                        return None
                    vids.append(vid)
                cur.execute(
                    "SELECT start_vid, end_vid, agg_cost FROM pgr_dijkstraCostMatrix("
                    "%s, %s, directed := false)",
                    (_edges_sql(prefix), vids),
                )
                idx = {v: i for i, v in enumerate(vids)}
                n = len(vids)
                mat = [[0.0] * n for _ in range(n)]
                for s, e, cost in cur.fetchall():
                    if s in idx and e in idx:
                        mat[idx[s]][idx[e]] = round(cost or 0, 1)  # meters
                return vids, mat
        finally:
            conn.close()
    except Exception:
        log.exception("pgRouting matrix query failed")
        return None


def _pg_isochrone(center_lon, center_lat, max_cost_m, prefix) -> dict | None:
    """Reachability hull via pgr_drivingDistance -> GeoJSON geometry dict or None."""
    if not HAS_PSYCOPG2:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                vid = _nearest_vertex(cur, prefix, center_lon, center_lat)
                if vid is None:
                    return None
                cur.execute(
                    "SELECT ST_AsGeoJSON(ST_ConvexHull(ST_Collect(v.the_geom))) "
                    f"FROM pgr_drivingDistance(%s, %s, %s, directed := false) dd "
                    f"JOIN {_vertices(prefix)} v ON dd.node = v.id",
                    (_edges_sql(prefix), vid, max_cost_m),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                return json.loads(row[0])
        finally:
            conn.close()
    except Exception:
        log.exception("pgRouting isochrone query failed")
        return None


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


# --- Handlers ------------------------------------------------------------------


def _handle_route(payload: dict) -> dict:
    """osm.Routing.PgRouting.Route — point-to-point via pgr_dijkstra."""
    from_lat, from_lon = payload.get("from_lat", 0), payload.get("from_lon", 0)
    to_lat, to_lon = payload.get("to_lat", 0), payload.get("to_lon", 0)
    from_name, to_name = payload.get("from_name", ""), payload.get("to_name", "")
    profile = payload.get("profile", "car")
    prefix = payload.get("prefix", _PREFIX)
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"PgRouting.Route: {from_name or 'origin'} -> {to_name or 'dest'} ({profile})")

    res = _pg_route(from_lon, from_lat, to_lon, to_lat, prefix)
    if res:
        distance_km = res["distance_km"]
        coords = res["coordinates"] or [[from_lon, from_lat], [to_lon, to_lat]]
        backend = "pgrouting"
    else:
        distance_km = round(_haversine_km(from_lon, from_lat, to_lon, to_lat) * 1.3, 2)
        coords = [[from_lon, from_lat], [to_lon, to_lat]]
        backend = "estimate"
    duration_min = round(distance_km / _speed(profile) * 60, 1)

    slug = f"{from_name or 'origin'}-{to_name or 'dest'}-{profile}".replace(" ", "_")
    output_path = os.path.join(_output_dir(), f"pgrouting-route-{slug}.geojson")
    with open(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"from": from_name, "to": to_name, "distance_km": distance_km,
                           "duration_min": duration_min, "profile": profile}}]}, f)

    if step_log:
        step_log(f"PgRouting.Route: {distance_km} km, {duration_min} min ({backend})", level="success")

    return {"result": {"route": {
        "from_name": from_name, "to_name": to_name, "distance_km": distance_km,
        "duration_min": duration_min, "output_path": output_path, "profile": profile,
        "backend": backend, "format": "GeoJSON"}, "waypoint_count": 2}}


def _handle_matrix(payload: dict) -> dict:
    """osm.Routing.PgRouting.Matrix — NxN via pgr_dijkstraCostMatrix."""
    points = _parse_points(payload.get("points", ""))
    profile = payload.get("profile", "car")
    prefix = payload.get("prefix", _PREFIX)
    step_log = payload.get("_step_log")
    if len(points) < 2:
        return {"result": {"output_path": "", "point_count": 0, "durations": "[]",
                           "distances": "[]", "profile": profile, "backend": "none", "format": "JSON"}}

    if step_log:
        step_log(f"PgRouting.Matrix: {len(points)}x{len(points)} ({profile})")

    speed = _speed(profile)
    res = _pg_matrix(points, prefix)
    if res:
        _vids, distances = res
        durations = [[round((m / 1000) / speed * 3600, 1) for m in row] for row in distances]
        backend = "pgrouting"
    else:
        coords = [(p.get("lon", 0), p.get("lat", 0)) for p in points]
        distances, durations = [], []
        for a in coords:
            dr, mr = [], []
            for b in coords:
                km = _haversine_km(a[0], a[1], b[0], b[1]) * 1.3
                mr.append(round(km * 1000, 1))
                dr.append(round(km / speed * 3600, 1))
            distances.append(mr)
            durations.append(dr)
        backend = "estimate"

    output_path = os.path.join(_output_dir(), f"pgrouting-matrix-{len(points)}pts-{profile}.json")
    with open(output_path, "w") as f:
        json.dump({"durations": durations, "distances": distances,
                   "profile": profile, "backend": backend}, f)

    if step_log:
        step_log(f"PgRouting.Matrix: {len(points)}x{len(points)} ({backend})", level="success")

    return {"result": {"output_path": output_path, "point_count": len(points),
                       "durations": json.dumps(durations), "distances": json.dumps(distances),
                       "profile": profile, "backend": backend, "format": "JSON"}}


def _handle_isochrone(payload: dict) -> dict:
    """osm.Routing.PgRouting.Isochrone — reachability hull via pgr_drivingDistance."""
    center_lat, center_lon = payload.get("center_lat", 0), payload.get("center_lon", 0)
    center_name = payload.get("center_name", "")
    time_minutes = payload.get("time_minutes", 15)
    profile = payload.get("profile", "car")
    prefix = payload.get("prefix", _PREFIX)
    step_log = payload.get("_step_log")

    if step_log:
        step_log(f"PgRouting.Isochrone: {center_name or 'center'}, {time_minutes}min ({profile})")

    # Distance-optimal topology: reachable distance budget = speed * time.
    max_cost_m = _speed(profile) * (time_minutes / 60) * 1000
    geom = _pg_isochrone(center_lon, center_lat, max_cost_m, prefix)
    if geom:
        features = [{"type": "Feature", "geometry": geom,
                     "properties": {"center_name": center_name, "time_minutes": time_minutes,
                                    "profile": profile}}]
        backend = "pgrouting"
    else:
        features = []
        backend = "none"

    output_path = os.path.join(
        _output_dir(),
        f"pgrouting-isochrone-{center_name or 'center'}-{time_minutes}min-{profile}.geojson".replace(" ", "_"),
    )
    with open(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)

    if step_log:
        step_log(f"PgRouting.Isochrone: {time_minutes}min polygon ({backend})", level="success")

    return {"result": {"output_path": output_path, "center_name": center_name,
                       "time_minutes": time_minutes, "profile": profile,
                       "backend": backend, "format": "GeoJSON"}}


PGROUTING_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.Route": _handle_route,
    f"{NAMESPACE}.Matrix": _handle_matrix,
    f"{NAMESPACE}.Isochrone": _handle_isochrone,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = PGROUTING_DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown pgRouting routing facet: {facet_name}")
    return handler(payload)
