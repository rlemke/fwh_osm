"""Population/places extractor plugin — nodes and relations.

Extracts places (cities, towns, villages, etc.) with population data.
Features are streamed to disk during scanning.
"""

from __future__ import annotations

from ..plugin_base import ElementType, ExtractorPlugin, PluginResult, TagInterest

# Place types recognized by the population extractor
PLACE_TYPES = {
    "city",
    "town",
    "village",
    "hamlet",
    "suburb",
    "country",
    "state",
    "county",
    "municipality",
}


class PopulationPlugin(ExtractorPlugin):
    """Extract populated places from OSM nodes."""

    @property
    def category(self) -> str:
        return "population"

    @property
    def element_types(self) -> ElementType:
        return ElementType.NODE

    @property
    def tag_interest(self) -> TagInterest:
        return TagInterest(keys={"place"})

    def __init__(self) -> None:
        self._type_counts: dict[str, int] = {}
        self._total_pop = 0

    def begin(self, pbf_stem: str, output_dir: str) -> None:
        path = f"{output_dir}/{pbf_stem}_population.geojson"
        self._open_writer(path)

    def process_node(self, node_id: int, tags: dict[str, str], lon: float, lat: float) -> None:
        place = tags.get("place", "")
        if place not in PLACE_TYPES:
            return

        pop_str = tags.get("population", "")
        try:
            population = int(pop_str) if pop_str else 0
        except ValueError:
            population = 0

        self._type_counts[place] = self._type_counts.get(place, 0) + 1
        self._total_pop += population

        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": node_id,
                    "osm_type": "node",
                    "place": place,
                    "name": tags.get("name", ""),
                    "population": population,
                    "admin_level": tags.get("admin_level", ""),
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                },
            }
        )

    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        if self._writer is None:
            path = f"{output_dir}/{pbf_stem}_population.geojson"
            return PluginResult(category=self.category, output_path=path, feature_count=0)

        self._writer.close()
        return PluginResult(
            category=self.category,
            output_path=self._writer.path,
            feature_count=self._writer.feature_count,
            metadata={"place_types": self._type_counts, "total_population": self._total_pop},
        )
