"""Event-facet handlers for the ``osm.emergency`` namespace.

Thin dispatch over :mod:`emergency_ops` — follows the transform/filters
handler pattern (per-facet functions, RegistryRunner ``handle`` dispatch +
poller helper). All facets are pure/cheap except RenderAtlas (moderate).
"""

from __future__ import annotations

import logging
import os

from . import emergency_ops as ops

log = logging.getLogger(__name__)

NAMESPACE = "osm.emergency"


def _h_top_cities(p: dict) -> dict:
    return ops.top_cities(p["input_path"], int(p.get("max_cities") or 10),
                          int(p.get("min_population") or 0))


def _h_build_category_set(p: dict) -> dict:
    return ops.build_category_set(p.get("hospitals_path") or "", p.get("fire_path") or "",
                                  p.get("police_path") or "", p.get("shelters_path") or "")


def _h_nearest_candidates(p: dict) -> dict:
    return ops.nearest_candidates(p["facilities_path"], float(p["from_lat"]),
                                  float(p["from_lon"]), int(p.get("k") or 5))


def _h_category_metrics(p: dict) -> dict:
    return ops.category_metrics(p.get("category") or "", p.get("network_distances") or [],
                                p.get("bucket_counts") or {}, int(p.get("facility_count") or 0),
                                p.get("city") or {})


def _h_city_readiness(p: dict) -> dict:
    return ops.city_readiness(p.get("city") or {}, p.get("category_metrics") or [])


def _h_region_readiness(p: dict) -> dict:
    return ops.region_readiness(p.get("region") or "", p.get("city_metrics") or [],
                                p.get("weights") or "")


def _h_rank_regions(p: dict) -> dict:
    return ops.rank_regions(p.get("region_metrics") or [])


def _h_render_atlas(p: dict) -> dict:
    return ops.render_atlas(p["layer_path"], p.get("rankings") or [],
                            p.get("title") or "Emergency-access atlas")


_DISPATCH: dict[str, callable] = {
    f"{NAMESPACE}.TopCities": _h_top_cities,
    f"{NAMESPACE}.BuildCategorySet": _h_build_category_set,
    f"{NAMESPACE}.NearestCandidates": _h_nearest_candidates,
    f"{NAMESPACE}.CategoryMetrics": _h_category_metrics,
    f"{NAMESPACE}.CityReadiness": _h_city_readiness,
    f"{NAMESPACE}.RegionReadiness": _h_region_readiness,
    f"{NAMESPACE}.RankRegions": _h_rank_regions,
    f"{NAMESPACE}.RenderAtlas": _h_render_atlas,
}


def handle(payload: dict) -> dict:
    """RegistryRunner dispatch entrypoint."""
    facet_name = payload["_facet_name"]
    handler = _DISPATCH.get(facet_name)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet_name}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register all osm.emergency facets with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_emergency_handlers(poller) -> None:
    """Register all osm.emergency event facet handlers with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
        log.debug("Registered emergency handler: %s", facet_name)
