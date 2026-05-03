"""AFL handler registration for osm.db.ImportGeoJSON event facet.

Imports GeoJSON output from OSM extractors into MongoDB with 2dsphere
indexing.  Works in both full and lite agents (no pyosmium dependency).
"""

import logging
import os
import re

from .osm_store import import_geojson

log = logging.getLogger(__name__)

NAMESPACE = "osm.db"
FACET = "ImportGeoJSON"
QUALIFIED = f"{NAMESPACE}.{FACET}"


def _slugify(name: str) -> str:
    """Convert a region name to a slug: 'North Carolina' -> 'north-carolina'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _handler(payload: dict) -> dict:
    """Handle an osm.db.ImportGeoJSON event."""
    output_path = payload.get("output_path", "")
    category = payload.get("category", "")
    region = payload.get("region", "")
    feature_count = payload.get("feature_count", 0)
    step_log = payload.get("_step_log")
    heartbeat = payload.get("_heartbeat")

    if not output_path or not os.path.isfile(output_path):
        msg = f"ImportGeoJSON: file not found: {output_path}"
        log.warning(msg)
        if step_log:
            step_log(msg, level="warning")
        return {"imported_count": 0, "dataset_key": "", "collection": "osm_features"}

    region_slug = _slugify(region) if region else "unknown"
    dataset_key = f"osm.{category}.{region_slug}" if category else f"osm.{region_slug}"

    if step_log:
        step_log(
            f"ImportGeoJSON: importing {output_path} as {dataset_key} (~{feature_count} features)"
        )

    try:
        result = import_geojson(
            path=output_path,
            dataset_key=dataset_key,
            category=category,
            region=region_slug,
            heartbeat=heartbeat,
        )

        if step_log:
            step_log(
                f"ImportGeoJSON: imported {result['imported_count']} features into {dataset_key}",
                level="success",
            )

        return result

    except Exception as e:
        log.error("ImportGeoJSON failed for %s: %s", dataset_key, e)
        if step_log:
            step_log(f"ImportGeoJSON: failed: {e}", level="error")
        return {"imported_count": 0, "dataset_key": dataset_key, "collection": "osm_features"}


# RegistryRunner dispatch
_DISPATCH = {
    QUALIFIED: _handler,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload.get("_facet_name", QUALIFIED)
    handler_fn = _DISPATCH.get(facet_name)
    if handler_fn is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler_fn(payload)


def register_import_handlers(poller) -> None:
    """Register with AgentPoller."""
    poller.register(QUALIFIED, _handler)
    log.debug("Registered import handler: %s", QUALIFIED)


def register_handlers(runner) -> None:
    """Register with RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )
