"""Park/protected area extractor plugin — area-based.

Extracts national parks, state parks, nature reserves, and other protected areas.
Features are streamed to disk during scanning.
"""

from __future__ import annotations

from ...parks.park_extractor import ParkType, calculate_area_km2, classify_park, matches_park_type
from ..plugin_base import ElementType, ExtractorPlugin, PluginResult, TagInterest


class ParkPlugin(ExtractorPlugin):
    """Extract parks and protected areas from OSM areas."""

    @property
    def category(self) -> str:
        return "parks"

    @property
    def element_types(self) -> ElementType:
        return ElementType.AREA

    @property
    def tag_interest(self) -> TagInterest:
        return TagInterest(
            keys={"boundary", "leisure", "protect_class"},
        )

    def __init__(self) -> None:
        self._total_area = 0.0
        self._type_counts: dict[str, int] = {}

    def begin(self, pbf_stem: str, output_dir: str) -> None:
        path = f"{output_dir}/{pbf_stem}_parks.geojson"
        self._open_writer(path)

    def process_area(
        self,
        area_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        orig_id: int,
        from_way: bool,
    ) -> None:
        if not matches_park_type(tags, ParkType.ALL):
            return

        area_km2 = calculate_area_km2(geometry) if geometry else 0.0
        classification = classify_park(tags)

        self._total_area += area_km2
        self._type_counts[classification] = self._type_counts.get(classification, 0) + 1

        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": orig_id,
                    "osm_type": "way" if from_way else "relation",
                    "name": tags.get("name", ""),
                    "park_type": classification,
                    "protect_class": tags.get("protect_class", ""),
                    "designation": tags.get("designation", ""),
                    "operator": tags.get("operator", ""),
                    "area_km2": round(area_km2, 2),
                },
                "geometry": geometry,
            }
        )

    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        if self._writer is None:
            path = f"{output_dir}/{pbf_stem}_parks.geojson"
            return PluginResult(category=self.category, output_path=path, feature_count=0)

        self._writer.close()
        return PluginResult(
            category=self.category,
            output_path=self._writer.path,
            feature_count=self._writer.feature_count,
            metadata={
                "total_area_km2": round(self._total_area, 2),
                "park_types": self._type_counts,
            },
        )
