"""Combined single-pass OSM scanner.

Runs selected extractor plugins in one ``apply_file()`` call.
Uses ``locations=True`` so way node coordinates are resolved inline —
no manual node cache or second pass needed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from facetwork.runtime.storage import localize

from ..shared._output import resolve_output_dir
from ..shared.scan_progress import ScanProgressTracker, get_file_size
from .plugin_base import ElementType, ExtractorPlugin, PluginResult

log = logging.getLogger(__name__)

try:
    import osmium

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False

try:
    from shapely import wkb
    from shapely.geometry import mapping

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False


@dataclass
class CombinedScanResult:
    """Result of a combined multi-category scan."""

    pbf_path: str
    categories: list[str]
    results: dict[str, PluginResult] = field(default_factory=dict)
    scan_duration_seconds: float = 0.0
    total_features: int = 0
    extraction_date: str = ""


def _build_plugin_registry() -> dict[str, type[ExtractorPlugin]]:
    """Lazy-load plugin classes to avoid import errors when osmium is missing."""
    from .plugins.amenity_plugin import AmenityPlugin
    from .plugins.boundary_plugin import BoundaryPlugin
    from .plugins.building_plugin import BuildingPlugin
    from .plugins.park_plugin import ParkPlugin
    from .plugins.population_plugin import PopulationPlugin
    from .plugins.road_plugin import RoadPlugin
    from .plugins.route_plugin import RoutePlugin

    return {
        "amenities": AmenityPlugin,
        "population": PopulationPlugin,
        "roads": RoadPlugin,
        "routes": RoutePlugin,
        "parks": ParkPlugin,
        "buildings": BuildingPlugin,
        "boundaries": BoundaryPlugin,
    }


# Public registry — populated lazily
PLUGIN_REGISTRY: dict[str, type[ExtractorPlugin]] = {}


def _ensure_registry() -> dict[str, type[ExtractorPlugin]]:
    global PLUGIN_REGISTRY
    if not PLUGIN_REGISTRY:
        PLUGIN_REGISTRY.update(_build_plugin_registry())
    return PLUGIN_REGISTRY


class _CombinedHandler(osmium.SimpleHandler if HAS_OSMIUM else object):
    """Single-pass pyosmium handler that dispatches to multiple plugins."""

    def __init__(
        self,
        plugins: list[ExtractorPlugin],
        progress: ScanProgressTracker | None = None,
        step_log: Any = None,
    ):
        if HAS_OSMIUM:
            super().__init__()
        self._plugins = plugins
        self._progress = progress
        self._step_log = step_log

        # Pre-partition plugins by element type for fast dispatch
        self._node_plugins = [p for p in plugins if ElementType.NODE in p.element_types]
        self._way_plugins = [p for p in plugins if ElementType.WAY in p.element_types]
        self._area_plugins = [p for p in plugins if ElementType.AREA in p.element_types]
        self._relation_plugins = [p for p in plugins if ElementType.RELATION in p.element_types]

        self._wkb_factory = osmium.geom.WKBFactory() if HAS_OSMIUM and HAS_SHAPELY else None

    def _tags_to_dict(self, tags) -> dict[str, str]:
        return {t.k: t.v for t in tags}

    def node(self, n):
        if self._progress:
            self._progress.tick("node")
        if not self._node_plugins:
            return
        tags = self._tags_to_dict(n.tags)
        if not tags:
            return
        lon, lat = n.location.lon, n.location.lat
        for plugin in self._node_plugins:
            if plugin.tag_interest.matches(tags):
                try:
                    plugin.process_node(n.id, tags, lon, lat)
                except Exception as exc:
                    log.warning(
                        "%s: error processing node %d: %s",
                        plugin.category,
                        n.id,
                        exc,
                    )
                    if self._step_log:
                        self._step_log(
                            f"{plugin.category}: error processing node {n.id}: {exc}",
                            level="warning",
                        )

    def way(self, w):
        if self._progress:
            self._progress.tick("way")
        if not self._way_plugins:
            return
        tags = self._tags_to_dict(w.tags)
        if not tags:
            return

        # Build coords eagerly from locations=True
        coords: list[tuple[float, float]] = []
        for nd in w.nodes:
            try:
                coords.append((nd.location.lon, nd.location.lat))
            except Exception:
                pass  # InvalidLocationError — skip node

        for plugin in self._way_plugins:
            if plugin.tag_interest.matches(tags):
                try:
                    plugin.process_way(w.id, tags, coords)
                except Exception as exc:
                    log.warning(
                        "%s: error processing way %d: %s",
                        plugin.category,
                        w.id,
                        exc,
                    )
                    if self._step_log:
                        self._step_log(
                            f"{plugin.category}: error processing way {w.id}: {exc}",
                            level="warning",
                        )

    def area(self, a):
        if self._progress:
            self._progress.tick("area")
        if not self._area_plugins:
            return
        tags = self._tags_to_dict(a.tags)
        if not tags:
            return

        # Extract geometry once, share across plugins
        geometry = None
        if self._wkb_factory:
            try:
                wkb_data = self._wkb_factory.create_multipolygon(a)
                geom = wkb.loads(wkb_data, hex=True)
                geometry = mapping(geom)
            except Exception:
                pass

        orig_id = a.orig_id()
        from_way = a.from_way()

        for plugin in self._area_plugins:
            if plugin.tag_interest.matches(tags):
                try:
                    plugin.process_area(a.id, tags, geometry, orig_id, from_way)
                except Exception as exc:
                    log.warning(
                        "%s: error processing area %d: %s",
                        plugin.category,
                        a.id,
                        exc,
                    )
                    if self._step_log:
                        self._step_log(
                            f"{plugin.category}: error processing area {a.id}: {exc}",
                            level="warning",
                        )

    def relation(self, r):
        if self._progress:
            self._progress.tick("relation")
        if not self._relation_plugins:
            return
        tags = self._tags_to_dict(r.tags)
        if not tags:
            return

        members = [{"type": m.type, "ref": m.ref, "role": m.role} for m in r.members]

        for plugin in self._relation_plugins:
            if plugin.tag_interest.matches(tags):
                try:
                    plugin.process_relation(r.id, tags, members)
                except Exception as exc:
                    log.warning(
                        "%s: error processing relation %d: %s",
                        plugin.category,
                        r.id,
                        exc,
                    )
                    if self._step_log:
                        self._step_log(
                            f"{plugin.category}: error processing relation {r.id}: {exc}",
                            level="warning",
                        )


def combined_scan(
    pbf_path: str | Path,
    categories: list[str],
    output_dir: str | None = None,
    step_log: Any = None,
) -> CombinedScanResult:
    """Run a combined single-pass extraction.

    Args:
        pbf_path: Path to PBF file (local or HDFS).
        categories: List of plugin names to activate
            (e.g. ``["roads", "amenities", "parks"]``).
        output_dir: Base output directory. Defaults to
            ``resolve_output_dir("osm-combined")``.
        step_log: Optional callback for progress reporting.

    Returns:
        CombinedScanResult with per-category results.
    """
    if not HAS_OSMIUM:
        raise RuntimeError("pyosmium is required for combined scan")

    registry = _ensure_registry()

    # Validate categories
    unknown = [c for c in categories if c not in registry]
    if unknown:
        raise ValueError(f"Unknown categories: {unknown}. Available: {sorted(registry.keys())}")

    pbf_path = str(localize(str(pbf_path)))
    pbf_stem = Path(pbf_path).stem

    if output_dir is None:
        output_dir = resolve_output_dir("osm-combined")

    # Instantiate selected plugins and open output streams
    plugins = [registry[cat]() for cat in categories]
    for plugin in plugins:
        plugin.begin(pbf_stem, output_dir)

    # Progress tracker
    file_size = get_file_size(pbf_path)
    progress = ScanProgressTracker(
        file_size, step_log, label=f"CombinedScan[{','.join(categories)}]"
    )

    # Single-pass scan — features stream to disk via each plugin's writer
    handler = _CombinedHandler(plugins, progress=progress, step_log=step_log)
    t0 = time.monotonic()
    handler.apply_file(pbf_path, locations=True)
    scan_seconds = time.monotonic() - t0
    progress.finish()

    # Finalize each plugin — close writers, collect results
    results: dict[str, PluginResult] = {}
    total_features = 0
    num_plugins = len(plugins)
    for idx, plugin in enumerate(plugins, 1):
        if step_log:
            feat_count = plugin._writer.feature_count if plugin._writer else 0
            step_log(
                f"Finalizing {plugin.category} ({idx}/{num_plugins}): "
                f"closing stream with {feat_count:,} features"
            )
        t_fin = time.monotonic()
        try:
            result = plugin.finalize(pbf_stem, output_dir)
            results[plugin.category] = result
            total_features += result.feature_count
            if step_log:
                dur = time.monotonic() - t_fin
                step_log(
                    f"Finalized {plugin.category}: {result.feature_count:,} features in {dur:.1f}s",
                    level="success",
                )
        except Exception as exc:
            log.error("%s: finalize failed: %s", plugin.category, exc)
            if step_log:
                step_log(
                    f"Finalize {plugin.category} failed: {exc}",
                    level="error",
                )
            results[plugin.category] = PluginResult(
                category=plugin.category,
                output_path="",
                feature_count=0,
                error=str(exc),
            )

    return CombinedScanResult(
        pbf_path=pbf_path,
        categories=categories,
        results=results,
        scan_duration_seconds=round(scan_seconds, 2),
        total_features=total_features,
        extraction_date=datetime.now(UTC).isoformat(),
    )
