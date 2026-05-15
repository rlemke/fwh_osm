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
    region_from_path,
    resolve,
    resolve_batch,
)


def _download_as_osm_cache(
    geofabrik_path: str, region: dict | None = None
) -> dict:
    """Download a PBF and return an OSMCache dict with the embedded Region.

    When ``region`` is None, the helper resolves the path via
    ``region_from_path`` so every OSMCache has a populated region — even
    when callers only know the path string.
    """
    if region is None:
        region = region_from_path(geofabrik_path).to_dict()
    return to_osm_cache(download_region(geofabrik_path), region=region)


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

    # Resolve the path back into a typed Region so OSMCache.region is always
    # populated — downstream handlers shouldn't care whether the cache was
    # produced via the new Region-driven path or the legacy string entry.
    typed_region = region_from_path(geofabrik_path, query=region).to_dict()
    cache = to_osm_cache(
        download_region(geofabrik_path), region=typed_region
    )
    if step_log:
        step_log(
            f"CacheRegion: '{region}' -> {geofabrik_path} (source={cache.get('source', 'unknown')})"
        )
    return {"cache": cache}


def handle_region_download(params: dict[str, Any]) -> dict[str, Any]:
    """Download (or return cached) the Geofabrik PBF for a typed Region.

    Backs ``osm.cache.Download(region: Region) => (cache: OSMCache)``. The
    Region's ``geofabrik_path`` is the authoritative input; everything else
    on the Region struct (name, level, continent, ...) is metadata used only
    for logging and downstream traceability.

    Pairs with ``osm.Region.ResolveRegions`` for the cross-region pattern:

        resolved = ResolveRegions(names = ["Africa", "California", ...])
        andThen foreach r in resolved.regions {
            cache = Download(region = $.r)
            ...
        }

    Idempotent: re-running with the same Region returns the existing cache
    entry with ``wasInCache = true``.

    Params:
        region: A Region dict matching the ``osm.types.Region`` schema.
            ``region["geofabrik_path"]`` is required; other fields are
            metadata.
    """
    region = params.get("region") or {}
    if not isinstance(region, dict):
        raise ValueError(
            f"Download: 'region' must be a Region dict, got {type(region).__name__}"
        )

    geofabrik_path = region.get("geofabrik_path") or ""
    if not geofabrik_path:
        # Falling back to canonical when geofabrik_path is missing keeps the
        # handler usable with hand-constructed Region literals.
        geofabrik_path = region.get("canonical") or ""
    if not geofabrik_path:
        raise ValueError(
            "Download: Region is missing geofabrik_path (and canonical). "
            "Resolve the region via osm.Region.ResolveRegions first, or "
            "construct the Region with a geofabrik_path."
        )

    step_log = params.get("_step_log")
    # Pass the input Region through verbatim so OSMCache.region carries the
    # caller's typed metadata (name, level, continent, ...) rather than the
    # path-only fallback.
    cache = to_osm_cache(download_region(geofabrik_path), region=region)
    if step_log:
        display = region.get("name") or geofabrik_path
        source = cache.get("source", "unknown")
        step_log(
            f"Download: '{display}' -> {geofabrik_path} (source={source}, "
            f"wasInCache={cache.get('wasInCache', False)})"
        )
    return {"cache": cache}


def handle_resolve_region(params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a single region name to a canonical Region.

    Backs ``osm.Region.ResolveRegion(name, prefer_continent) => (region: Region)``.
    The legacy ``(cache, resolution)`` shape has been retired — callers should
    chain ``osm.cache.Download(region = resolved.region)`` for the cache.

    On no match, returns a Region with all fields empty rather than failing;
    the caller can decide whether to abort (Download with an empty
    geofabrik_path will raise) or continue with a soft handling.

    Params:
        name: Human-friendly region name (e.g. "Colorado", "UK", "Quebec").
            Per-name qualifier suffixes ("Georgia, US" / "Georgia (country)")
            and canonical paths ("north-america/us/georgia") are supported.
        prefer_continent: Optional continent for batch-style disambiguation
            (e.g. "NorthAmerica"). Qualifier suffix takes precedence.
    """
    name = params["name"]
    prefer_continent = params.get("prefer_continent", "") or None
    step_log = params.get("_step_log")

    # Use the batch resolver so qualifier-suffix parsing and the new
    # path-based continent matching are applied consistently with
    # ResolveRegions.
    result = resolve_batch(
        names=[name], prefer_continent=prefer_continent, strict=False
    )

    if not result.regions:
        if step_log:
            step_log(f"ResolveRegion: no match for '{name}'")
        empty = region_from_path("", query=name).to_dict()
        return {"region": empty}

    region = result.regions[0]
    if step_log:
        step_log(
            f"ResolveRegion: '{name}' -> {region.name} ({region.canonical}, "
            f"level={region.level})"
        )
    return {"region": region.to_dict()}


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
    "osm.cache.Download": handle_region_download,
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
