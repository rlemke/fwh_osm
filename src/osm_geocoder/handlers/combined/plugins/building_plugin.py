"""Building footprint extractor plugin — area-based.

Extracts building footprints with type classification and height data.
Features are streamed to disk during scanning.
"""

from __future__ import annotations

from ...buildings.building_extractor import classify_building, parse_height
from ..plugin_base import ElementType, ExtractorPlugin, PluginResult, TagInterest

try:
    from shapely.geometry import shape

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False


class BuildingPlugin(ExtractorPlugin):
    """Extract building footprints from OSM areas."""

    @property
    def category(self) -> str:
        return "buildings"

    @property
    def element_types(self) -> ElementType:
        return ElementType.AREA

    @property
    def tag_interest(self) -> TagInterest:
        return TagInterest(keys={"building"})

    def __init__(self) -> None:
        self._type_counts: dict[str, int] = {}

    def begin(self, pbf_stem: str, output_dir: str) -> None:
        path = f"{output_dir}/{pbf_stem}_buildings.geojson"
        self._open_writer(path)

    def process_area(
        self,
        area_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        orig_id: int,
        from_way: bool,
    ) -> None:
        building = tags.get("building", "")
        if not building:
            return

        classification = classify_building(tags)
        height = parse_height(tags.get("height") or tags.get("building:height"))
        levels = _parse_int(tags.get("building:levels"))

        area_km2 = 0.0
        if geometry and HAS_SHAPELY:
            try:
                geom = shape(geometry)
                if not geom.is_valid:
                    geom = geom.buffer(0)
                # Rough area in degrees² → km² (approximate)
                import math

                bounds = geom.bounds
                mid_lat = (bounds[1] + bounds[3]) / 2
                m_per_deg_lat = 111132.92
                m_per_deg_lon = 111132.92 * math.cos(math.radians(mid_lat))
                area_km2 = geom.area * m_per_deg_lat * m_per_deg_lon / 1_000_000
            except Exception:
                pass

        self._type_counts[classification] = self._type_counts.get(classification, 0) + 1

        self._writer.write_feature(
            {
                "type": "Feature",
                "properties": {
                    "osm_id": orig_id,
                    "osm_type": "way" if from_way else "relation",
                    "building": building,
                    "building_type": classification,
                    "name": tags.get("name", ""),
                    "height": height,
                    "levels": levels,
                    "area_km2": round(area_km2, 6),
                },
                "geometry": geometry,
            }
        )

    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        if self._writer is None:
            path = f"{output_dir}/{pbf_stem}_buildings.geojson"
            return PluginResult(category=self.category, output_path=path, feature_count=0)

        self._writer.close()
        return PluginResult(
            category=self.category,
            output_path=self._writer.path,
            feature_count=self._writer.feature_count,
            metadata={"building_types": self._type_counts},
        )


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None
