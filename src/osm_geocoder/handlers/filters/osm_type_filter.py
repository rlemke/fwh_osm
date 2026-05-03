"""OSM element type and tag filtering for PBF files.

Filters OSM data by element type (node, way, relation) and tags,
with optional dependency inclusion for complete geometry reconstruction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from facetwork.runtime.storage import get_storage_backend, localize

from ..shared._output import ensure_dir, open_output, resolve_output_dir, uri_stem
from ..shared.scan_progress import ScanProgressTracker, get_file_size

_storage = get_storage_backend()

log = logging.getLogger(__name__)

# Check for pyosmium availability
try:
    import osmium

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False


class OSMType(Enum):
    """OSM element types."""

    NODE = "node"
    WAY = "way"
    RELATION = "relation"
    ALL = "*"

    @classmethod
    def from_string(cls, value: str) -> OSMType:
        """Parse an OSM type string (case-insensitive)."""
        normalized = value.lower().strip()
        aliases = {
            "node": cls.NODE,
            "n": cls.NODE,
            "way": cls.WAY,
            "w": cls.WAY,
            "relation": cls.RELATION,
            "rel": cls.RELATION,
            "r": cls.RELATION,
            "*": cls.ALL,
            "all": cls.ALL,
            "any": cls.ALL,
        }
        if normalized in aliases:
            return aliases[normalized]
        raise ValueError(f"Unknown OSM type: {value}")


@dataclass
class OSMFilteredFeatures:
    """Result of an OSM filtering operation."""

    output_path: str
    feature_count: int
    original_count: int
    osm_type: str
    filter_applied: str
    dependencies_included: bool
    dependency_count: int = 0
    format: str = "GeoJSON"
    extraction_date: str = ""


@dataclass
class OSMElement:
    """Represents an OSM element (node, way, or relation)."""

    id: int
    osm_type: OSMType
    tags: dict[str, str]
    lat: float | None = None  # For nodes
    lon: float | None = None  # For nodes
    node_refs: list[int] = field(default_factory=list)  # For ways
    members: list[dict] = field(default_factory=list)  # For relations

    def to_geojson_feature(
        self, node_coords: dict[int, tuple[float, float]] | None = None
    ) -> dict | None:
        """Convert to GeoJSON feature.

        Args:
            node_coords: Mapping of node IDs to (lon, lat) coordinates for ways

        Returns:
            GeoJSON feature dict, or None if geometry cannot be constructed
        """
        properties = {
            "osm_id": self.id,
            "osm_type": self.osm_type.value,
            **self.tags,
        }

        if self.osm_type == OSMType.NODE:
            if self.lat is None or self.lon is None:
                return None
            return {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "Point",
                    "coordinates": [self.lon, self.lat],
                },
            }

        if self.osm_type == OSMType.WAY:
            if not self.node_refs:
                return None
            if node_coords is None:
                # No coordinates available, return with empty geometry
                return {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": None,
                }

            # Build coordinate list from node references
            coords = []
            for node_id in self.node_refs:
                if node_id in node_coords:
                    coords.append(list(node_coords[node_id]))

            if len(coords) < 2:
                return None

            # Determine if this is a polygon (closed way) or linestring
            is_closed = len(coords) >= 4 and coords[0] == coords[-1]

            # Check tags to determine if it should be a polygon
            area_tags = {"building", "landuse", "natural", "leisure", "amenity", "boundary"}
            is_area = bool(area_tags & set(self.tags.keys())) or self.tags.get("area") == "yes"

            if is_closed and is_area:
                return {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coords],
                    },
                }
            else:
                return {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                }

        if self.osm_type == OSMType.RELATION:
            # Relations are complex - for now, return with member info in properties
            properties["members"] = self.members
            return {
                "type": "Feature",
                "properties": properties,
                "geometry": None,  # Would need full member resolution
            }

        return None


class OSMTypeHandler(osmium.SimpleHandler if HAS_OSMIUM else object):
    """Handler for extracting OSM elements by type and tags."""

    def __init__(
        self,
        osm_type: OSMType = OSMType.ALL,
        tag_key: str | None = None,
        tag_value: str | None = None,
        include_dependencies: bool = False,
        progress=None,
    ):
        if HAS_OSMIUM:
            super().__init__()
        self.osm_type = osm_type
        self.tag_key = tag_key
        self.tag_value = tag_value if tag_value != "*" else None
        self.include_dependencies = include_dependencies
        self._progress = progress

        # Collected elements
        self.elements: list[OSMElement] = []
        self.total_count = 0

        # For dependency tracking
        self.needed_node_ids: set[int] = set()
        self.dependency_nodes: dict[int, tuple[float, float]] = {}

    def _matches_filter(self, tags: osmium.osm.TagList, element_type: OSMType) -> bool:
        """Check if element matches the type and tag filter."""
        # Check type
        if self.osm_type != OSMType.ALL and self.osm_type != element_type:
            return False

        # Check tag if specified
        if self.tag_key:
            tag_dict = {t.k: t.v for t in tags}
            if self.tag_key not in tag_dict:
                return False
            if self.tag_value and tag_dict[self.tag_key] != self.tag_value:
                return False

        return True

    def node(self, n):
        """Process a node."""
        self.total_count += 1
        if self._progress:
            self._progress.tick("node")

        # Check if this is a dependency node we need
        if self.include_dependencies and n.id in self.needed_node_ids:
            self.dependency_nodes[n.id] = (n.location.lon, n.location.lat)

        if self._matches_filter(n.tags, OSMType.NODE):
            self.elements.append(
                OSMElement(
                    id=n.id,
                    osm_type=OSMType.NODE,
                    tags={t.k: t.v for t in n.tags},
                    lat=n.location.lat,
                    lon=n.location.lon,
                )
            )

    def way(self, w):
        """Process a way."""
        self.total_count += 1
        if self._progress:
            self._progress.tick("way")

        if self._matches_filter(w.tags, OSMType.WAY):
            node_refs = [n.ref for n in w.nodes]
            self.elements.append(
                OSMElement(
                    id=w.id,
                    osm_type=OSMType.WAY,
                    tags={t.k: t.v for t in w.tags},
                    node_refs=node_refs,
                )
            )
            # Track needed nodes for geometry reconstruction
            if self.include_dependencies:
                self.needed_node_ids.update(node_refs)

    def relation(self, r):
        """Process a relation."""
        self.total_count += 1
        if self._progress:
            self._progress.tick("relation")

        if self._matches_filter(r.tags, OSMType.RELATION):
            members = [{"type": m.type, "ref": m.ref, "role": m.role} for m in r.members]
            self.elements.append(
                OSMElement(
                    id=r.id,
                    osm_type=OSMType.RELATION,
                    tags={t.k: t.v for t in r.tags},
                    members=members,
                )
            )


def filter_pbf_by_type(
    input_path: str | Path,
    osm_type: str | OSMType = OSMType.ALL,
    tag_key: str | None = None,
    tag_value: str | None = None,
    include_dependencies: bool = False,
    output_path: str | Path | None = None,
    step_log=None,
) -> OSMFilteredFeatures:
    """Filter a PBF file by OSM element type and/or tags.

    Args:
        input_path: Path to input PBF file
        osm_type: OSM element type to filter (node, way, relation, or *)
        tag_key: Optional tag key to filter by
        tag_value: Optional tag value (use "*" or None for any value)
        include_dependencies: If True, include referenced nodes for ways
        output_path: Path to output GeoJSON file (default: adds _filtered suffix)
        step_log: Optional callback for progress reporting.

    Returns:
        OSMFilteredFeatures with output path and counts
    """
    if not HAS_OSMIUM:
        raise RuntimeError("pyosmium is required for PBF filtering")

    input_path = Path(localize(str(input_path)))
    if output_path is None:
        out_dir = resolve_output_dir("osm-filtered")
        output_path_str = f"{out_dir}/{input_path.stem}.filtered.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    # Parse OSM type
    if isinstance(osm_type, str):
        osm_type = OSMType.from_string(osm_type)

    # Create handler and process file
    file_size = get_file_size(str(input_path))
    progress = ScanProgressTracker(file_size, step_log, label="ExtractAndFilter")

    handler = OSMTypeHandler(
        osm_type=osm_type,
        tag_key=tag_key,
        tag_value=tag_value,
        include_dependencies=include_dependencies,
        progress=progress,
    )

    # First pass: collect matching elements and identify needed nodes
    handler.apply_file(str(input_path))
    progress.finish()

    # Second pass: collect dependency nodes if needed
    if include_dependencies and handler.needed_node_ids:
        # Create a simple handler just for collecting node coordinates
        class NodeCollector(osmium.SimpleHandler):
            def __init__(self, needed_ids: set[int]):
                super().__init__()
                self.needed_ids = needed_ids
                self.coords: dict[int, tuple[float, float]] = {}

            def node(self, n):
                if n.id in self.needed_ids:
                    self.coords[n.id] = (n.location.lon, n.location.lat)

        collector = NodeCollector(handler.needed_node_ids)
        collector.apply_file(str(input_path), locations=True)
        handler.dependency_nodes = collector.coords

    # Convert to GeoJSON
    features = []
    node_coords = handler.dependency_nodes if include_dependencies else None

    for element in handler.elements:
        feature = element.to_geojson_feature(node_coords)
        if feature:
            features.append(feature)

    output_geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    # Write output
    with open_output(output_path_str) as f:
        json.dump(output_geojson, f, indent=2)

    # Build filter description
    filter_desc = _describe_osm_filter(osm_type, tag_key, tag_value, include_dependencies)

    return OSMFilteredFeatures(
        output_path=output_path_str,
        feature_count=len(features),
        original_count=handler.total_count,
        osm_type=osm_type.value,
        filter_applied=filter_desc,
        dependencies_included=include_dependencies,
        dependency_count=len(handler.dependency_nodes) if include_dependencies else 0,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def filter_geojson_by_osm_type(
    input_path: str | Path,
    osm_type: str | OSMType = OSMType.ALL,
    tag_key: str | None = None,
    tag_value: str | None = None,
    output_path: str | Path | None = None,
    heartbeat: callable | None = None,
    task_uuid: str = "",
) -> OSMFilteredFeatures:
    """Filter a GeoJSON file by OSM element type and/or tags.

    This filters GeoJSON features that have osm_type and tag properties.

    Args:
        input_path: Path to input GeoJSON file
        osm_type: OSM element type to filter (node, way, relation, or *)
        tag_key: Optional tag key to filter by
        tag_value: Optional tag value (use "*" or None for any value)
        output_path: Path to output GeoJSON file (default: adds _filtered suffix)
        heartbeat: Optional callback to signal progress during long operations

    Returns:
        OSMFilteredFeatures with output path and counts
    """

    input_path = str(input_path)
    if output_path is None:
        out_dir = resolve_output_dir("osm-filtered")
        output_path_str = f"{out_dir}/{uri_stem(input_path)}_filtered.geojson"
    else:
        output_path_str = str(output_path)
    ensure_dir(output_path_str)

    # Parse OSM type
    if isinstance(osm_type, str):
        osm_type = OSMType.from_string(osm_type)

    # Stream features to avoid loading multi-GB files into memory.
    # Write to local temp file to avoid VirtioFS write stalls.
    import os
    import shutil
    import tempfile

    from facetwork.runtime.storage import localize

    from ..shared.geojson_writer import GeoJSONStreamWriter, iter_geojson_features

    local_path = localize(input_path)

    original_count = 0
    from facetwork.config import get_temp_dir

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
    os.close(tmp_fd)

    try:
        with GeoJSONStreamWriter(tmp_path) as writer:
            for feature in iter_geojson_features(local_path, heartbeat):
                original_count += 1
                props = feature.get("properties", {})

                # Check OSM type
                if osm_type != OSMType.ALL:
                    feature_type = props.get("osm_type", "")
                    if feature_type != osm_type.value:
                        continue

                # Check tag if specified
                if tag_key:
                    if tag_key not in props:
                        continue
                    if tag_value and tag_value != "*" and props[tag_key] != tag_value:
                        continue

                writer.write_feature(feature)

        ensure_dir(output_path_str)
        shutil.move(tmp_path, output_path_str)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    filter_desc = _describe_osm_filter(osm_type, tag_key, tag_value, False)

    return OSMFilteredFeatures(
        output_path=output_path_str,
        feature_count=writer.feature_count,
        original_count=original_count,
        osm_type=osm_type.value,
        filter_applied=filter_desc,
        dependencies_included=False,
        extraction_date=datetime.now(UTC).isoformat(),
    )


def _describe_osm_filter(
    osm_type: OSMType,
    tag_key: str | None,
    tag_value: str | None,
    include_dependencies: bool,
) -> str:
    """Build a human-readable filter description."""
    parts = []

    if osm_type != OSMType.ALL:
        parts.append(f"type={osm_type.value}")

    if tag_key:
        if tag_value and tag_value != "*":
            parts.append(f"{tag_key}={tag_value}")
        else:
            parts.append(f"{tag_key}=*")

    if include_dependencies:
        parts.append("+deps")

    return ", ".join(parts) if parts else "all elements"
