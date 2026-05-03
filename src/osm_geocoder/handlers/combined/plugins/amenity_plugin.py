"""Amenity extractor plugin — node-only.

Extracts amenities (food, shopping, healthcare, etc.) from OSM nodes.
Features are streamed to disk during scanning.
"""

from __future__ import annotations

from ...amenities.amenity_extractor import classify_amenity
from ..plugin_base import ElementType, ExtractorPlugin, PluginResult, TagInterest


class AmenityPlugin(ExtractorPlugin):
    """Extract amenities by category from OSM nodes."""

    @property
    def category(self) -> str:
        return "amenities"

    @property
    def element_types(self) -> ElementType:
        return ElementType.NODE

    @property
    def tag_interest(self) -> TagInterest:
        return TagInterest(keys={"amenity", "shop", "tourism"})

    def __init__(self) -> None:
        self._category_counts: dict[str, int] = {}

    def begin(self, pbf_stem: str, output_dir: str) -> None:
        path = f"{output_dir}/{pbf_stem}_amenities.geojson"
        self._open_writer(path)

    def process_node(self, node_id: int, tags: dict[str, str], lon: float, lat: float) -> None:
        amenity = tags.get("amenity", "")
        shop = tags.get("shop", "")
        tourism = tags.get("tourism", "")
        if not amenity and not shop and not tourism:
            return

        cat = classify_amenity(tags)
        self._category_counts[cat] = self._category_counts.get(cat, 0) + 1

        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": node_id,
                    "osm_type": "node",
                    "amenity": amenity,
                    "shop": shop,
                    "tourism": tourism,
                    "category": cat,
                    "name": tags.get("name", ""),
                    "opening_hours": tags.get("opening_hours", ""),
                    "phone": tags.get("phone", ""),
                    "website": tags.get("website", ""),
                    "cuisine": tags.get("cuisine", ""),
                    "brand": tags.get("brand", ""),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        )

    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        if self._writer is None:
            path = f"{output_dir}/{pbf_stem}_amenities.geojson"
            return PluginResult(category=self.category, output_path=path, feature_count=0)

        self._writer.close()
        return PluginResult(
            category=self.category,
            output_path=self._writer.path,
            feature_count=self._writer.feature_count,
            metadata={"categories": self._category_counts},
        )
