"""Event facet handlers for OSM region caching and resolution.

Handles ``osm.ops.CacheRegion`` (the PBF download step every region facet
and composed analysis workflow starts from) plus ``osm.Region.ResolveRegion``
/ ``ResolveRegions`` / ``ListRegions``, all delegating to the shared
``pbf_cache`` / ``region_resolver`` libraries.
"""

import os
from typing import Any

from ..shared.pbf_cache import download_region, to_osm_cache
from ..shared.region_resolver import (
    StrictResolutionError,
    list_regions_typed,
    resolve,
    resolve_batch,
)


def _download_as_osm_cache(geofabrik_path: str) -> dict:
    """Download a PBF via the shared cache library and return an OSMCache dict."""
    return to_osm_cache(download_region(geofabrik_path))


def handle_cache_region(params: dict[str, Any]) -> dict[str, Any]:
    """Download (or reuse the cached) Geofabrik PBF for ``region``.

    The ``osm.ops.CacheRegion`` event facet — the first step of every
    ``osm.cache.*`` region facet and every composed analysis workflow.

    Params:
        region: a Geofabrik path ("africa/algeria",
            "north-america/us/california") used directly, or a human-friendly
            name ("Algeria", "California", "Liechtenstein") which is resolved
            to the best-matching Geofabrik path first.
    """
    region = params["region"]
    step_log = params.get("_step_log")

    geofabrik_path = region
    if "/" not in region:
        # Friendly name (no path separator) — resolve to a Geofabrik path.
        result = resolve(region)
        if result.matches:
            geofabrik_path = result.matches[0].geofabrik_path

    cache = to_osm_cache(download_region(geofabrik_path))
    if step_log:
        step_log(
            f"CacheRegion: '{region}' -> {geofabrik_path} (source={cache.get('source', 'unknown')})"
        )
    return {"cache": cache}


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
    """Batch resolve a list of region names to ``Region`` records.

    Backs ``osm.Region.ResolveRegions(names: [String], prefer_continent,
    strict)``. Returns the resolved regions as a flat list (feature
    expansions like ``"Alps"`` contribute every constituent) plus a
    diagnostics record listing unresolved names, ambiguous matches, and
    feature expansions.

    Params:
        names: List of region names. Continents, countries, subnational
            regions, and named geographic features may be freely mixed.
            Per-name qualifier suffixes ("Georgia, US" / "Georgia
            (country)") and canonical Geofabrik paths
            ("north-america/us/georgia") are supported.
        prefer_continent: Batch-wide tiebreaker (only used for names
            without a qualifier suffix).
        strict: When True, fail the step if any name is unresolved or
            ambiguous. When False (default), return partial results and
            let the caller inspect diagnostics.
    """
    names = params.get("names") or []
    prefer_continent = params.get("prefer_continent", "") or None
    strict = bool(params.get("strict", False))
    step_log = params.get("_step_log")

    try:
        result = resolve_batch(
            names=names, prefer_continent=prefer_continent, strict=strict
        )
    except StrictResolutionError as exc:
        if step_log:
            step_log(f"ResolveRegions: strict failure — {exc}")
        raise

    if step_log:
        step_log(
            f"ResolveRegions: {len(names)} input → {len(result.regions)} regions "
            f"(unresolved={len(result.diagnostics.unresolved)}, "
            f"ambiguous={len(result.diagnostics.ambiguous)}, "
            f"expanded={len(result.diagnostics.expanded)})"
        )

    return {
        "regions": [r.to_dict() for r in result.regions],
        "diagnostics": result.diagnostics.to_dict(),
    }


def handle_list_regions(params: dict[str, Any]) -> dict[str, Any]:
    """List the known region catalog as ``Region`` records.

    Backs ``osm.Region.ListRegions(parent_canonical, level, continent)``.

    Params:
        parent_canonical: Filter to regions whose parent path equals this
            (e.g. ``"north-america/canada"`` for Canadian provinces).
        level: Filter to regions of this hierarchical level
            (``"planet"``, ``"continent"``, ``"country"``, ``"subnational"``).
        continent: Filter to regions in this continent display bucket
            (``"Europe"``, ``"NorthAmerica"``, …). Top-level extracts like
            Russia / Antarctica / Planet have ``continent == ""``.
    """
    parent_canonical = params.get("parent_canonical", "") or None
    level = params.get("level", "") or None
    continent = params.get("continent", "") or None

    regions = list_regions_typed(
        parent_canonical=parent_canonical,
        level=level,
        continent=continent,
    )

    return {
        "regions": [r.to_dict() for r in regions],
    }


# RegistryRunner dispatch adapter
_DISPATCH = {
    "osm.ops.CacheRegion": handle_cache_region,
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
    """Register all region cache/resolution handlers with the poller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
