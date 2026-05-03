"""Administrative boundary extractor plugin — area + relation based.

Extracts administrative boundaries (country, state, county, city) and
natural boundaries (water, forest, park).
Features are streamed to disk during scanning.
"""

from __future__ import annotations

import importlib.util

from ...boundaries.boundary_extractor import (
    ADMIN_LEVEL_CITY,
    ADMIN_LEVEL_COUNTRY,
    ADMIN_LEVEL_COUNTY,
    ADMIN_LEVEL_STATE,
)
from ..plugin_base import ElementType, ExtractorPlugin, PluginResult, TagInterest

HAS_SHAPELY = importlib.util.find_spec("shapely") is not None

# Admin levels we extract
ADMIN_LEVELS = {
    ADMIN_LEVEL_COUNTRY,
    ADMIN_LEVEL_STATE,
    ADMIN_LEVEL_COUNTY,
    ADMIN_LEVEL_CITY,
}

ADMIN_LEVEL_NAMES = {
    ADMIN_LEVEL_COUNTRY: "country",
    ADMIN_LEVEL_STATE: "state",
    ADMIN_LEVEL_COUNTY: "county",
    ADMIN_LEVEL_CITY: "city",
}

# Natural boundary tag keys
NATURAL_KEYS = {"natural", "landuse", "water"}


class BoundaryPlugin(ExtractorPlugin):
    """Extract administrative and natural boundaries."""

    @property
    def category(self) -> str:
        return "boundaries"

    @property
    def element_types(self) -> ElementType:
        return ElementType.AREA

    @property
    def tag_interest(self) -> TagInterest:
        return TagInterest(keys={"boundary", "natural", "landuse", "water"})

    def __init__(self) -> None:
        self._admin_count = 0
        self._natural_count = 0

    def begin(self, pbf_stem: str, output_dir: str) -> None:
        path = f"{output_dir}/{pbf_stem}_boundaries.geojson"
        self._open_writer(path)

    def process_area(
        self,
        area_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        orig_id: int,
        from_way: bool,
    ) -> None:
        boundary = tags.get("boundary", "")

        if boundary == "administrative":
            self._handle_admin(orig_id, tags, geometry, from_way)
        elif boundary in ("national_park", "protected_area"):
            self._handle_natural(orig_id, tags, geometry, from_way, "park")
        elif any(k in tags for k in NATURAL_KEYS):
            natural = tags.get("natural", "")
            landuse = tags.get("landuse", "")
            water = tags.get("water", "")
            if natural == "water" or water:
                self._handle_natural(orig_id, tags, geometry, from_way, "water")
            elif natural == "wood" or landuse == "forest":
                self._handle_natural(orig_id, tags, geometry, from_way, "forest")

    def _handle_admin(
        self,
        orig_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        from_way: bool,
    ) -> None:
        try:
            level = int(tags.get("admin_level", "0"))
        except ValueError:
            return
        if level not in ADMIN_LEVELS:
            return

        area_km2 = self._calc_area(geometry)
        self._admin_count += 1
        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": orig_id,
                    "osm_type": "way" if from_way else "relation",
                    "boundary_type": "administrative",
                    "admin_level": level,
                    "admin_type": ADMIN_LEVEL_NAMES.get(level, ""),
                    "name": tags.get("name", ""),
                    "area_km2": area_km2,
                },
                "geometry": geometry,
            }
        )

    def _handle_natural(
        self,
        orig_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        from_way: bool,
        natural_type: str,
    ) -> None:
        area_km2 = self._calc_area(geometry)
        self._natural_count += 1
        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": orig_id,
                    "osm_type": "way" if from_way else "relation",
                    "boundary_type": "natural",
                    "natural_type": natural_type,
                    "name": tags.get("name", ""),
                    "area_km2": area_km2,
                },
                "geometry": geometry,
            }
        )

    def _calc_area(self, geometry: dict | None) -> float:
        if not geometry or not HAS_SHAPELY:
            return 0.0
        try:
            from ...parks.park_extractor import calculate_area_km2

            return round(calculate_area_km2(geometry), 2)
        except Exception:
            return 0.0

    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        if self._writer is None:
            path = f"{output_dir}/{pbf_stem}_boundaries.geojson"
            return PluginResult(category=self.category, output_path=path, feature_count=0)

        self._writer.close()
        return PluginResult(
            category=self.category,
            output_path=self._writer.path,
            feature_count=self._writer.feature_count,
            metadata={
                "administrative": self._admin_count,
                "natural": self._natural_count,
            },
        )
