"""POI event facet handlers for OSM point-of-interest extraction.

Handles Cities, Towns, Suburbs, Villages, Hamlets, Countries, POI,
and GeoOSMCache events defined in osmpoi.afl under osm.POIs.

Delegates to combined_scan() from combined_handler.py for actual PBF
extraction via pyosmium, then filters the resulting GeoJSON for the
specific place type.
"""

import json
import logging
import os
from datetime import UTC, datetime

from ..combined.combined_handler import HAS_OSMIUM, combined_scan
from ..shared.output_cache import cached_result, save_result_meta

log = logging.getLogger(__name__)

NAMESPACE = "osm.POIs"

# Maps POI facet names to (return_param, place_type, min_population)
POI_FACETS: dict[str, tuple[str, str, int]] = {
    "Cities": ("cities", "city", 0),
    "Towns": ("towns", "town", 0),
    "Suburbs": ("towns", "suburb", 0),
    "Villages": ("villages", "village", 0),
    "Hamlets": ("villages", "hamlet", 0),
    "Countries": ("villages", "country", 0),
    "POI": ("pois", "all", 0),
    "GeoOSMCache": ("geojson", "all", 0),
}


def _make_poi_handler(facet_name: str, return_param: str, place_type: str, min_pop: int):
    """Create a handler for a POI event facet using combined_scan extraction."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        cache = payload.get("cache", {})
        pbf_path = cache.get("path", "")
        step_log = payload.get("_step_log")

        # Check output cache
        cache_params = {"place_type": place_type, "min_population": min_pop}
        hit = cached_result(qualified, cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: extracting {place_type} from {pbf_path}")
        log.info("%s extracting %s from %s", facet_name, place_type, pbf_path)

        if not HAS_OSMIUM or not pbf_path:
            if step_log:
                step_log(
                    f"{facet_name}: skipped (osmium={'available' if HAS_OSMIUM else 'missing'}, path={'set' if pbf_path else 'empty'})",
                    level="warning",
                )
            return {return_param: _empty_cache(cache)}

        try:
            scan_result = combined_scan(pbf_path, ["population"])
            pop_result = scan_result.results.get("population")

            if not pop_result or not pop_result.get("output_path"):
                if step_log:
                    step_log(
                        f"{facet_name}: no population data from combined scan", level="warning"
                    )
                return {return_param: _empty_cache(cache)}

            output_path = pop_result["output_path"]

            # Filter for the specific place type if not "all"
            if place_type != "all":
                from facetwork.runtime.storage import get_storage_backend

                _st = get_storage_backend(output_path)
                with _st.open(output_path, "r") as f:
                    geojson = json.load(f)

                filtered = [
                    feat
                    for feat in geojson.get("features", [])
                    if feat.get("properties", {}).get("place") == place_type
                ]
                feature_count = len(filtered)
            else:
                # For "all", just count all features
                from facetwork.runtime.storage import get_storage_backend

                _st = get_storage_backend(output_path)
                with _st.open(output_path, "r") as f:
                    geojson = json.load(f)
                feature_count = len(geojson.get("features", []))

            if step_log:
                step_log(
                    f"{facet_name}: extracted {feature_count} {place_type} features",
                    level="success",
                )
            rv = {
                return_param: {
                    "url": cache.get("url", ""),
                    "path": output_path,
                    "date": datetime.now(UTC).isoformat(),
                    "size": feature_count,
                    "wasInCache": False,
                }
            }
            save_result_meta(qualified, cache, cache_params, rv)
            return rv
        except Exception as exc:
            log.error("Failed to extract %s from %s: %s", place_type, pbf_path, exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to extract {place_type}: {exc}", level="error")
            raise

    return handler


def _empty_cache(cache: dict) -> dict:
    """Return an empty OSMCache result."""
    return {
        "url": cache.get("url", ""),
        "path": cache.get("path", ""),
        "date": cache.get("date", datetime.now(UTC).isoformat()),
        "size": 0,
        "wasInCache": True,
    }


def register_poi_handlers(poller) -> None:
    """Register all POI event facet handlers with the poller."""
    if not HAS_OSMIUM:
        return
    for facet_name, (return_param, place_type, min_pop) in POI_FACETS.items():
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(
            qualified_name, _make_poi_handler(facet_name, return_param, place_type, min_pop)
        )
        log.debug("Registered POI handler: %s", qualified_name)


# RegistryRunner dispatch adapter
_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, (return_param, place_type, min_pop) in POI_FACETS.items():
        _DISPATCH[f"{NAMESPACE}.{facet_name}"] = _make_poi_handler(
            facet_name, return_param, place_type, min_pop
        )


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
