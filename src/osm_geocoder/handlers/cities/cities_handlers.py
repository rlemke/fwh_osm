"""Event-facet handlers for osm.Cities.

Currently exposes ``osm.Cities.TierCitiesByPopulation`` — tiers a
populated-places GeoJSON into disjoint zoom bands.

The handler is a thin dispatcher: it converts payload parameters into
``Tier`` objects and routes to ``_osm_tools.tier_cities.tier_cities``,
which is the same code path the CLI tool exercises.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..shared._output import resolve_output_dir, uri_stem
from ..shared.output_cache import cached_result, save_result_meta

# Make tools/_osm_tools/ importable without requiring the whole tools/ dir
# to be a Python package.
_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from _osm_tools.tier_cities import (  # noqa: E402
    DEFAULT_TIERS,
    Tier,
    split_by_tier,
    tier_cities,
)

log = logging.getLogger(__name__)

NAMESPACE = "osm.Cities"


def _build_tiers(payload: dict) -> tuple[Tier, ...]:
    """Build the tier tuple from facet params, falling back to defaults."""
    # Tiers can come in via four explicit (zoom, min_pop) pairs.
    candidates: list[Tier] = []
    for suffix in ("a", "b", "c", "d"):
        zoom = payload.get(f"zoom_{suffix}")
        pop = payload.get(f"min_pop_{suffix}")
        if zoom is None or pop is None:
            continue
        try:
            candidates.append(Tier(zoom=int(zoom), min_population=int(pop)))
        except (TypeError, ValueError):
            continue
    if not candidates:
        return DEFAULT_TIERS
    return tuple(candidates)


def _make_tier_cities_by_population_handler(facet_name: str):
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")
        tiers = _build_tiers(payload)

        cache_params = {
            "tiers": [(t.zoom, t.min_population) for t in tiers],
        }
        input_cache = {"path": input_path, "size": 0}
        hit = cached_result(qualified, input_cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            tier_desc = ", ".join(
                f"z{t.zoom}>={t.min_population:,}" for t in tiers
            )
            step_log(f"{facet_name}: tiering {input_path} ({tier_desc})")
        log.info(
            "%s tiering %s with %d tiers",
            facet_name,
            input_path,
            len(tiers),
        )

        if not input_path:
            return {"result": _empty_result(tiers)}

        out_dir = resolve_output_dir("osm-cities")
        output_path = f"{out_dir}/{uri_stem(input_path)}_tiered.geojson"

        try:
            result = tier_cities(input_path, output_path, tiers=tiers)
        except Exception as exc:
            log.error("Failed to tier cities: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to tier cities: {exc}", level="error")
            raise

        if step_log:
            summary = ", ".join(
                f"z{z}={n}" for z, n in sorted(result.tier_counts.items())
            )
            step_log(
                f"{facet_name}: {result.total_count} cities tiered ({summary})",
                level="success",
            )

        rv = {"result": _result_to_dict(result, tiers)}
        save_result_meta(qualified, input_cache, cache_params, rv)
        return rv

    return handler


def _result_to_dict(result, tiers: tuple[Tier, ...]) -> dict:
    counts = result.tier_counts
    zooms = [t.zoom for t in tiers]
    # Surface the four tiers as named fields so the FFL workflow can yield them.
    a, b, c, d = (zooms + [None, None, None, None])[:4]
    return {
        "output_path": result.output_path,
        "total_count": result.total_count,
        "tier_a_zoom": a if a is not None else 0,
        "tier_a_count": counts.get(a, 0) if a is not None else 0,
        "tier_b_zoom": b if b is not None else 0,
        "tier_b_count": counts.get(b, 0) if b is not None else 0,
        "tier_c_zoom": c if c is not None else 0,
        "tier_c_count": counts.get(c, 0) if c is not None else 0,
        "tier_d_zoom": d if d is not None else 0,
        "tier_d_count": counts.get(d, 0) if d is not None else 0,
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _empty_result(tiers: tuple[Tier, ...]) -> dict:
    a, b, c, d = (
        [t.zoom for t in tiers] + [0, 0, 0, 0]
    )[:4]
    return {
        "output_path": "",
        "total_count": 0,
        "tier_a_zoom": a,
        "tier_a_count": 0,
        "tier_b_zoom": b,
        "tier_b_count": 0,
        "tier_c_zoom": c,
        "tier_c_count": 0,
        "tier_d_zoom": d,
        "tier_d_count": 0,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


def _make_split_tiers_handler(facet_name: str):
    """Split a tiered cities GeoJSON into one file per zoom band."""
    qualified = f"{NAMESPACE}.{facet_name}"

    def handler(payload: dict) -> dict:
        input_path = payload.get("input_path", "")
        step_log = payload.get("_step_log")
        zooms = (
            int(payload.get("zoom_a", 3) or 3),
            int(payload.get("zoom_b", 6) or 6),
            int(payload.get("zoom_c", 8) or 8),
            int(payload.get("zoom_d", 10) or 10),
        )

        cache_params = {"zooms": list(zooms)}
        input_cache = {"path": input_path, "size": 0}
        hit = cached_result(qualified, input_cache, cache_params, step_log)
        if hit is not None:
            return hit

        if step_log:
            step_log(f"{facet_name}: splitting {input_path} into zooms {list(zooms)}")
        log.info("%s splitting %s into zooms %s", facet_name, input_path, list(zooms))

        if not input_path:
            return {"result": _empty_split_result(zooms)}

        out_dir = resolve_output_dir("osm-cities") + f"/{uri_stem(input_path)}_split"
        try:
            result = split_by_tier(input_path, out_dir, zooms=zooms)
        except Exception as exc:
            log.error("Failed to split tiers: %s", exc)
            if step_log:
                step_log(f"{facet_name}: FAILED to split tiers: {exc}", level="error")
            raise

        if step_log:
            summary = ", ".join(f"z{z}={n}" for z, n in sorted(result.counts.items()))
            step_log(f"{facet_name}: split into {summary}", level="success")

        rv = {"result": _split_result_to_dict(result, zooms)}
        save_result_meta(qualified, input_cache, cache_params, rv)
        return rv

    return handler


def _split_result_to_dict(result, zooms: tuple[int, int, int, int]) -> dict:
    a, b, c, d = zooms
    return {
        "tier_a_path": result.output_paths.get(a, ""),
        "tier_a_count": result.counts.get(a, 0),
        "tier_b_path": result.output_paths.get(b, ""),
        "tier_b_count": result.counts.get(b, 0),
        "tier_c_path": result.output_paths.get(c, ""),
        "tier_c_count": result.counts.get(c, 0),
        "tier_d_path": result.output_paths.get(d, ""),
        "tier_d_count": result.counts.get(d, 0),
        "format": result.format,
        "extraction_date": result.extraction_date,
    }


def _empty_split_result(zooms: tuple[int, int, int, int]) -> dict:
    return {
        "tier_a_path": "", "tier_a_count": 0,
        "tier_b_path": "", "tier_b_count": 0,
        "tier_c_path": "", "tier_c_count": 0,
        "tier_d_path": "", "tier_d_count": 0,
        "format": "GeoJSON",
        "extraction_date": datetime.now(UTC).isoformat(),
    }


CITIES_FACETS = [
    ("TierCitiesByPopulation", _make_tier_cities_by_population_handler),
    ("SplitTiers", _make_split_tiers_handler),
]


_DISPATCH: dict[str, callable] = {}


def _build_dispatch() -> None:
    for facet_name, handler_factory in CITIES_FACETS:
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


def register_cities_handlers(poller) -> None:
    """Register all cities event facet handlers with an AgentPoller."""
    for facet_name, handler_factory in CITIES_FACETS:
        qualified_name = f"{NAMESPACE}.{facet_name}"
        poller.register(qualified_name, handler_factory(facet_name))
        log.debug("Registered cities handler: %s", qualified_name)
