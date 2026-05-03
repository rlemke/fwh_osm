"""Event facet handlers for OSM region resolution.

Handles ResolveRegion, ResolveRegions, and ListRegions event facets by
delegating to the region_resolver module.
"""

import os
from typing import Any

from ..shared.pbf_cache import download_region, to_osm_cache
from ..shared.region_resolver import (
    list_geographic_features,
    list_regions,
    resolve,
)


def _download_as_osm_cache(geofabrik_path: str) -> dict:
    """Download a PBF via the shared cache library and return an OSMCache dict."""
    return to_osm_cache(download_region(geofabrik_path))


def handle_resolve_region(params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a region name and download the best matching OSM cache.

    Params:
        name: Human-friendly region name (e.g. "Colorado", "UK")
        prefer_continent: Optional continent for disambiguation (e.g. "NorthAmerica")
    """
    name = params["name"]
    prefer_continent = params.get("prefer_continent", "") or None
    step_log = params.get("_step_log")

    result = resolve(name, prefer_continent=prefer_continent)

    if not result.matches:
        if step_log:
            step_log(f"ResolveRegion: no match for '{name}'")

        return {
            "cache": {
                "url": "",
                "path": "",
                "date": "",
                "size": 0,
                "wasInCache": False,
            },
            "resolution": {
                "query": name,
                "matched_name": "",
                "region_namespace": "",
                "continent": "",
                "geofabrik_path": "",
                "is_ambiguous": False,
                "disambiguation": f"No region found for '{name}'",
            },
        }

    best = result.matches[0]
    cache = _download_as_osm_cache(best.geofabrik_path)
    source = cache.get("source", "unknown")
    if step_log:
        step_log(
            f"ResolveRegion: '{name}' -> {best.facet_name} ({best.geofabrik_path}, source={source})"
        )

    return {
        "cache": cache,
        "resolution": {
            "query": name,
            "matched_name": best.facet_name,
            "region_namespace": best.namespace,
            "continent": best.continent,
            "geofabrik_path": best.geofabrik_path,
            "is_ambiguous": result.is_ambiguous,
            "disambiguation": result.disambiguation,
        },
    }


def handle_resolve_regions(params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a region name and download all matching OSM caches.

    Params:
        name: Region or geographic feature name (e.g. "Alps", "Scandinavia")
        prefer_continent: Optional continent for disambiguation
    """
    name = params["name"]
    prefer_continent = params.get("prefer_continent", "") or None
    step_log = params.get("_step_log")

    result = resolve(name, prefer_continent=prefer_continent)
    if step_log:
        step_log(f"ResolveRegions: '{name}' -> {len(result.matches)} matches")

    caches = []
    regions = []
    for match in result.matches:
        cache = _download_as_osm_cache(match.geofabrik_path)
        source = cache.get("source", "unknown")
        if step_log:
            step_log(
                f"ResolveRegions: '{match.facet_name}' ({match.geofabrik_path}, source={source})"
            )
        caches.append(cache)
        regions.append(
            {
                "name": match.facet_name,
                "namespace": match.namespace,
                "continent": match.continent,
                "geofabrik_path": match.geofabrik_path,
            }
        )

    return {
        "caches": caches,
        "resolution": {
            "query": name,
            "match_count": len(result.matches),
            "is_geographic_feature": result.is_geographic_feature,
            "regions": regions,
        },
    }


def handle_list_regions(params: dict[str, Any]) -> dict[str, Any]:
    """List all available regions and geographic features.

    Params:
        continent: Optional continent filter (e.g. "Europe", "Africa")
    """
    continent = params.get("continent", "") or None

    regions = list_regions(continent=continent)
    features = list_geographic_features()

    region_list = [
        {
            "name": r.facet_name,
            "namespace": r.namespace,
            "continent": r.continent,
            "geofabrik_path": r.geofabrik_path,
        }
        for r in regions
    ]

    return {
        "result": {
            "region_count": len(region_list),
            "regions": region_list,
            "feature_count": len(features),
            "geographic_features": dict(features.items()),
        },
    }


# RegistryRunner dispatch adapter
_DISPATCH = {
    "osm.Region.ResolveRegion": handle_resolve_region,
    "osm.Region.ResolveRegions": handle_resolve_regions,
    "osm.Region.ListRegions": handle_list_regions,
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


def register_region_handlers(poller) -> None:
    """Register all region resolution handlers with the poller."""
    handlers = {
        "osm.Region.ResolveRegion": handle_resolve_region,
        "osm.Region.ResolveRegions": handle_resolve_regions,
        "osm.Region.ListRegions": handle_list_regions,
    }

    for facet_name, handler in handlers.items():
        poller.register(facet_name, handler)
