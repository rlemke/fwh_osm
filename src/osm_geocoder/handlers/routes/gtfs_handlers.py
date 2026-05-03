"""GTFS transit feed event facet handlers.

Handles transit extraction events defined in osmgtfs.afl under
osm.Transit.GTFS.
"""

import logging
import os
from datetime import UTC, datetime

from .gtfs_extractor import (
    AccessibilityResult,
    CoverageResult,
    DensityResult,
    FrequencyResult,
    GTFSRouteFeatures,
    NearestStopResult,
    StopResult,
    TransitStats,
    compute_coverage_gaps,
    compute_route_density,
    compute_service_frequency,
    compute_stop_accessibility,
    compute_transit_statistics,
    download_gtfs_feed,
    extract_routes,
    extract_stops,
    find_nearest_stops,
    generate_transit_report,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Transit.GTFS"


# ── Result converters ───────────────────────────────────────────────────


def _stop_result_to_dict(result: StopResult) -> dict:
    """Convert a StopResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "stop_count": result.stop_count,
        "bbox_min_lat": result.bbox_min_lat,
        "bbox_min_lon": result.bbox_min_lon,
        "bbox_max_lat": result.bbox_max_lat,
        "bbox_max_lon": result.bbox_max_lon,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _route_result_to_dict(result: GTFSRouteFeatures) -> dict:
    """Convert a GTFSRouteFeatures to a dictionary."""
    return {
        "output_path": result.output_path,
        "route_count": result.route_count,
        "has_shapes": result.has_shapes,
        "route_types": result.route_types,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _frequency_result_to_dict(result: FrequencyResult) -> dict:
    """Convert a FrequencyResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "stop_count": result.stop_count,
        "avg_trips_per_day": result.avg_trips_per_day,
        "max_trips_per_day": result.max_trips_per_day,
        "service_date": result.service_date,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _stats_to_dict(stats: TransitStats) -> dict:
    """Convert TransitStats to a dictionary."""
    return {
        "agency_name": stats.agency_name,
        "stop_count": stats.stop_count,
        "route_count": stats.route_count,
        "trip_count": stats.trip_count,
        "has_shapes": stats.has_shapes,
        "route_type_counts": stats.route_type_counts,
        "extraction_date": stats.extraction_date,
    }


def _nearest_result_to_dict(result: NearestStopResult) -> dict:
    """Convert a NearestStopResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "feature_count": result.feature_count,
        "matched_count": result.matched_count,
        "avg_distance_m": result.avg_distance_m,
        "max_distance_m": result.max_distance_m,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _accessibility_result_to_dict(result: AccessibilityResult) -> dict:
    """Convert an AccessibilityResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "total_features": result.total_features,
        "within_400m": result.within_400m,
        "within_800m": result.within_800m,
        "beyond_800m": result.beyond_800m,
        "pct_within_400m": result.pct_within_400m,
        "pct_within_800m": result.pct_within_800m,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _coverage_result_to_dict(result: CoverageResult) -> dict:
    """Convert a CoverageResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "total_cells": result.total_cells,
        "covered_cells": result.covered_cells,
        "gap_cells": result.gap_cells,
        "coverage_pct": result.coverage_pct,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _density_result_to_dict(result: DensityResult) -> dict:
    """Convert a DensityResult to a dictionary."""
    return {
        "output_path": result.output_path,
        "total_cells": result.total_cells,
        "max_routes_per_cell": result.max_routes_per_cell,
        "avg_routes_per_cell": result.avg_routes_per_cell,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


# ── Empty result helpers ────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _empty_feed() -> dict:
    return {
        "url": "",
        "path": "",
        "date": _now_iso(),
        "size": 0,
        "wasInCache": False,
        "agency_name": "",
        "has_shapes": False,
    }


def _empty_stop_result() -> dict:
    return {
        "output_path": "",
        "stop_count": 0,
        "bbox_min_lat": 0.0,
        "bbox_min_lon": 0.0,
        "bbox_max_lat": 0.0,
        "bbox_max_lon": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_route_result() -> dict:
    return {
        "output_path": "",
        "route_count": 0,
        "has_shapes": False,
        "route_types": "",
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_frequency_result() -> dict:
    return {
        "output_path": "",
        "stop_count": 0,
        "avg_trips_per_day": 0.0,
        "max_trips_per_day": 0,
        "service_date": "",
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_stats() -> dict:
    return {
        "agency_name": "",
        "stop_count": 0,
        "route_count": 0,
        "trip_count": 0,
        "has_shapes": False,
        "route_type_counts": "",
        "extraction_date": _now_iso(),
    }


def _empty_nearest_result() -> dict:
    return {
        "output_path": "",
        "feature_count": 0,
        "matched_count": 0,
        "avg_distance_m": 0.0,
        "max_distance_m": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_accessibility_result() -> dict:
    return {
        "output_path": "",
        "total_features": 0,
        "within_400m": 0,
        "within_800m": 0,
        "beyond_800m": 0,
        "pct_within_400m": 0.0,
        "pct_within_800m": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_coverage_result() -> dict:
    return {
        "output_path": "",
        "total_cells": 0,
        "covered_cells": 0,
        "gap_cells": 0,
        "coverage_pct": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_density_result() -> dict:
    return {
        "output_path": "",
        "total_cells": 0,
        "max_routes_per_cell": 0,
        "avg_routes_per_cell": 0.0,
        "format": "GeoJSON",
        "extraction_date": _now_iso(),
    }


def _empty_report() -> dict:
    return {
        "feed_agency": "",
        "stop_count": 0,
        "route_count": 0,
        "trip_count": 0,
        "has_shapes": False,
        "stops_path": "",
        "routes_path": "",
        "frequency_path": "",
        "osm_integration": False,
        "nearest_stops_path": "",
        "accessibility_path": "",
        "coverage_path": "",
        "density_path": "",
        "extraction_date": _now_iso(),
    }


# ── Handler factories ───────────────────────────────────────────────────


def _make_download_feed_handler(facet_name: str):
    """Create handler for DownloadFeed event facet."""

    def handler(payload: dict) -> dict:
        url = payload.get("url", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: downloading GTFS feed from {url}")
        log.info("%s downloading GTFS feed from %s", facet_name, url)

        if not url:
            return {"feed": _empty_feed()}

        try:
            feed = download_gtfs_feed(url)
            if step_log:
                step_log(
                    f"{facet_name}: downloaded feed ({feed.get('agency_name', 'unknown')})",
                    level="success",
                )
            return {"feed": feed}
        except Exception as exc:
            log.error("Failed to download GTFS feed: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to download GTFS feed: {exc}", level="error")
            raise

    return handler


def _make_extract_stops_handler(facet_name: str):
    """Create handler for ExtractStops event facet."""

    def handler(payload: dict) -> dict:
        feed = payload.get("feed", {})
        feed_path = feed.get("path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: extracting stops from {feed_path}")
        log.info("%s extracting stops from %s", facet_name, feed_path)

        if not feed_path:
            return {"result": _empty_stop_result()}

        try:
            result = extract_stops(feed_path)
            if step_log:
                step_log(f"{facet_name}: extracted {result.stop_count} stops", level="success")
            return {"result": _stop_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to extract stops: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to extract stops: {exc}", level="error")
            raise

    return handler


def _make_extract_routes_handler(facet_name: str):
    """Create handler for ExtractRoutes event facet."""

    def handler(payload: dict) -> dict:
        feed = payload.get("feed", {})
        feed_path = feed.get("path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: extracting routes from {feed_path}")
        log.info("%s extracting routes from %s", facet_name, feed_path)

        if not feed_path:
            return {"result": _empty_route_result()}

        try:
            result = extract_routes(feed_path)
            if step_log:
                step_log(f"{facet_name}: extracted {result.route_count} routes", level="success")
            return {"result": _route_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to extract routes: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to extract routes: {exc}", level="error")
            raise

    return handler


def _make_service_frequency_handler(facet_name: str):
    """Create handler for ServiceFrequency event facet."""

    def handler(payload: dict) -> dict:
        feed = payload.get("feed", {})
        feed_path = feed.get("path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: computing service frequency from {feed_path}")
        log.info("%s computing service frequency from %s", facet_name, feed_path)

        if not feed_path:
            return {"result": _empty_frequency_result()}

        try:
            result = compute_service_frequency(feed_path)
            if step_log:
                step_log(
                    f"{facet_name}: {result.stop_count} stops, avg {result.avg_trips_per_day:.1f} trips/day",
                    level="success",
                )
            return {"result": _frequency_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to compute service frequency: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute service frequency: {exc}", level="error")
            raise

    return handler


def _make_transit_statistics_handler(facet_name: str):
    """Create handler for TransitStatistics event facet."""

    def handler(payload: dict) -> dict:
        feed = payload.get("feed", {})
        feed_path = feed.get("path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: computing transit statistics from {feed_path}")
        log.info("%s computing transit statistics from %s", facet_name, feed_path)

        if not feed_path:
            return {"stats": _empty_stats()}

        try:
            stats = compute_transit_statistics(feed_path)
            if step_log:
                step_log(
                    f"{facet_name}: {stats.stop_count} stops, {stats.route_count} routes, {stats.trip_count} trips",
                    level="success",
                )
            return {"stats": _stats_to_dict(stats)}
        except Exception as exc:
            log.error("Failed to compute transit statistics: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute transit statistics: {exc}", level="error")
            raise

    return handler


def _make_nearest_stops_handler(facet_name: str):
    """Create handler for NearestStops event facet."""

    def handler(payload: dict) -> dict:
        osm_path = payload.get("osm_geojson_path", "")
        stops_path = payload.get("stops_geojson_path", "")
        max_dist = payload.get("max_distance_m", 2000)
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: finding nearest stops (max {max_dist}m)")
        log.info("%s finding nearest stops (max %dm)", facet_name, max_dist)

        if not osm_path or not stops_path:
            return {"result": _empty_nearest_result()}

        try:
            result = find_nearest_stops(osm_path, stops_path, float(max_dist))
            if step_log:
                step_log(
                    f"{facet_name}: {result.matched_count}/{result.feature_count} matched (avg {result.avg_distance_m:.0f}m)",
                    level="success",
                )
            return {"result": _nearest_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to find nearest stops: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to find nearest stops: {exc}", level="error")
            raise

    return handler


def _make_stop_accessibility_handler(facet_name: str):
    """Create handler for StopAccessibility event facet."""

    def handler(payload: dict) -> dict:
        osm_path = payload.get("osm_geojson_path", "")
        stops_path = payload.get("stops_geojson_path", "")
        threshold = payload.get("walk_threshold_m", 400)
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: computing stop accessibility (threshold {threshold}m)")
        log.info("%s computing stop accessibility (threshold %dm)", facet_name, threshold)

        if not osm_path or not stops_path:
            return {"result": _empty_accessibility_result()}

        try:
            result = compute_stop_accessibility(osm_path, stops_path, float(threshold))
            if step_log:
                step_log(
                    f"{facet_name}: {result.within_400m}/{result.total_features} within 400m ({result.pct_within_400m:.1f}%)",
                    level="success",
                )
            return {"result": _accessibility_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to compute stop accessibility: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute stop accessibility: {exc}", level="error")
            raise

    return handler


def _make_coverage_gaps_handler(facet_name: str):
    """Create handler for CoverageGaps event facet."""

    def handler(payload: dict) -> dict:
        stops_path = payload.get("stops_geojson_path", "")
        osm_path = payload.get("osm_geojson_path", "")
        cell_size = payload.get("cell_size_m", 500)
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: computing coverage gaps (cell {cell_size}m)")
        log.info("%s computing coverage gaps (cell %dm)", facet_name, cell_size)

        if not stops_path or not osm_path:
            return {"result": _empty_coverage_result()}

        try:
            result = compute_coverage_gaps(stops_path, osm_path, float(cell_size))
            if step_log:
                step_log(
                    f"{facet_name}: {result.covered_cells}/{result.total_cells} cells covered ({result.coverage_pct:.1f}%)",
                    level="success",
                )
            return {"result": _coverage_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to compute coverage gaps: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute coverage gaps: {exc}", level="error")
            raise

    return handler


def _make_route_density_handler(facet_name: str):
    """Create handler for RouteDensity event facet."""

    def handler(payload: dict) -> dict:
        routes_path = payload.get("routes_geojson_path", "")
        cell_size = payload.get("cell_size_m", 500)
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: computing route density (cell {cell_size}m)")
        log.info("%s computing route density (cell %dm)", facet_name, cell_size)

        if not routes_path:
            return {"result": _empty_density_result()}

        try:
            result = compute_route_density(routes_path, float(cell_size))
            if step_log:
                step_log(
                    f"{facet_name}: {result.total_cells} cells, max {result.max_routes_per_cell} routes/cell",
                    level="success",
                )
            return {"result": _density_result_to_dict(result)}
        except Exception as exc:
            log.error("Failed to compute route density: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to compute route density: {exc}", level="error")
            raise

    return handler


def _make_generate_report_handler(facet_name: str):
    """Create handler for GenerateReport event facet."""

    def handler(payload: dict) -> dict:
        feed = payload.get("feed", {})
        feed_path = feed.get("path", "")
        osm_path = payload.get("osm_geojson_path", "")
        step_log = payload.get("_step_log")

        if step_log:
            step_log(f"{facet_name}: generating transit report from {feed_path}")
        log.info("%s generating transit report from %s", facet_name, feed_path)

        if not feed_path:
            return {"report": _empty_report()}

        try:
            report = generate_transit_report(feed_path, osm_path)
            if step_log:
                step_log(
                    f"{facet_name}: report generated ({report.get('stop_count', 0)} stops, {report.get('route_count', 0)} routes)",
                    level="success",
                )
            return {"report": report}
        except Exception as exc:
            log.error("Failed to generate transit report: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to generate transit report: {exc}", level="error")
            raise

    return handler


# ── Registration ────────────────────────────────────────────────────────


GTFS_FACETS = [
    ("DownloadFeed", _make_download_feed_handler),
    ("ExtractStops", _make_extract_stops_handler),
    ("ExtractRoutes", _make_extract_routes_handler),
    ("ServiceFrequency", _make_service_frequency_handler),
    ("TransitStatistics", _make_transit_statistics_handler),
    ("NearestStops", _make_nearest_stops_handler),
    ("StopAccessibility", _make_stop_accessibility_handler),
    ("CoverageGaps", _make_coverage_gaps_handler),
    ("RouteDensity", _make_route_density_handler),
    ("GenerateReport", _make_generate_report_handler),
]


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in GTFS_FACETS:
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = handler_factory(facet_name)


_build_dispatch()


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


def register_gtfs_handlers(poller) -> None:
    """Register all GTFS event facet handlers with the poller."""
    for facet_name, handler_factory in GTFS_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered GTFS handler: %s", qualified_name)
