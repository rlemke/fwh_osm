"""Boundary extraction from OSM PBF files.

Uses ``osmium tags-filter`` to extract boundary relations from PBF into
OSM XML, then ``osm2geojson`` to assemble geometries (including
``type=boundary`` relations that pyosmium and GDAL cannot assemble).
Falls back to pyosmium when osmium-tool or osm2geojson are unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from facetwork.runtime.storage import get_storage_backend, localize

from ..shared._output import ensure_dir, open_output, resolve_output_dir
from ..shared.scan_progress import ScanProgressTracker, get_file_size

_storage = get_storage_backend()

try:
    import osmium
    from osmium import osm

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False
    osmium = None
    osm = None

try:
    import osm2geojson

    HAS_OSM2GEOJSON = True
except ImportError:
    HAS_OSM2GEOJSON = False
    osm2geojson = None

# Check for osmium-tool CLI
HAS_OSMIUM_TOOL = shutil.which("osmium") is not None

log = logging.getLogger(__name__)

# Default output directory for extracted boundaries
from facetwork.config import get_output_base

_LOCAL_OUTPUT = get_output_base()
DEFAULT_OUTPUT_DIR = Path(os.path.join(_LOCAL_OUTPUT, "osm", "boundaries"))

# Admin level mappings
ADMIN_LEVEL_COUNTRY = 2
ADMIN_LEVEL_STATE = 4
ADMIN_LEVEL_COUNTY = 6
ADMIN_LEVEL_CITY = 8

# Natural type tag filter expressions for ``osmium tags-filter``
_NATURAL_TAG_FILTERS: dict[str, list[str]] = {
    "water": ["r/natural=water", "r/water=lake,reservoir,pond"],
    "forest": ["r/natural=wood", "r/landuse=forest"],
    "park": ["r/leisure=park,nature_reserve", "r/boundary=national_park"],
}

# Natural type tag mappings (for pyosmium fallback)
NATURAL_TYPE_WATER = {"natural": ["water"], "water": ["lake", "reservoir", "pond"]}
NATURAL_TYPE_FOREST = {"natural": ["wood"], "landuse": ["forest"]}
NATURAL_TYPE_PARK = {"leisure": ["park", "nature_reserve"], "boundary": ["national_park"]}


@dataclass
class BoundaryFeature:
    """A single boundary feature extracted from OSM."""

    osm_id: int
    osm_type: str  # 'way' or 'relation'
    name: str
    admin_level: int | None
    boundary_type: str
    tags: dict[str, str]
    geometry: dict[str, Any] | None = None


@dataclass
class ExtractionResult:
    """Result of a boundary extraction operation."""

    output_path: str
    feature_count: int
    boundary_type: str
    admin_levels: str
    format: str = "GeoJSON"
    extraction_date: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ── Primary extraction: osmium-tool + osm2geojson ───────────────────────


def _build_tag_filters(
    admin_levels: list[int] | None,
    natural_types: list[str] | None,
) -> list[str]:
    """Build ``osmium tags-filter`` expressions."""
    filters: list[str] = []
    if admin_levels:
        filters.append("r/boundary=administrative")
    for nt in natural_types or []:
        filters.extend(_NATURAL_TAG_FILTERS.get(nt, []))
    return filters


def _matches_admin(tags: dict[str, str], admin_levels: set[int]) -> bool:
    if not admin_levels:
        return False
    if tags.get("boundary") != "administrative":
        return False
    try:
        return int(tags.get("admin_level", "")) in admin_levels
    except ValueError:
        return False


def _matches_natural(tags: dict[str, str], natural_types: list[str]) -> str | None:
    """Return matched natural type or None."""
    type_maps = {
        "water": NATURAL_TYPE_WATER,
        "forest": NATURAL_TYPE_FOREST,
        "park": NATURAL_TYPE_PARK,
    }
    for nt in natural_types:
        for tag_key, tag_values in type_maps.get(nt, {}).items():
            if tags.get(tag_key) in tag_values:
                return nt
    return None


def _get_pbf_bbox(pbf_path: str) -> tuple[float, float, float, float] | None:
    """Read the bounding box from a PBF file header via ``osmium fileinfo``."""
    try:
        result = subprocess.run(
            ["osmium", "fileinfo", "-j", pbf_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout)
        boxes = info.get("header", {}).get("boxes", [])
        if boxes:
            return tuple(boxes[0])  # (min_lon, min_lat, max_lon, max_lat)
    except Exception:
        pass
    return None


def _region_name_from_pbf(pbf_path: str) -> str | None:
    """Extract the region name from a Geofabrik PBF filename.

    ``california-latest.osm.pbf`` → ``california``
    ``district-of-columbia-latest.osm.pbf`` → ``district of columbia``
    ``new-york-latest.osm.pbf`` → ``new york``
    """
    stem = Path(pbf_path).stem  # e.g. "california-latest.osm" or "california-latest"
    # Remove common suffixes
    for suffix in (".osm", "-latest", "-internal"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem.replace("-", " ").lower() if stem else None


def _name_matches_region(feature_name: str, region: str) -> bool:
    """Check if a feature name matches the expected region (case-insensitive)."""
    return feature_name.lower().strip() == region


def _extract_via_osm2geojson(
    pbf_path: str,
    admin_levels: list[int] | None,
    natural_types: list[str] | None,
) -> list[BoundaryFeature]:
    """Extract using ``osmium tags-filter`` → ``osm2geojson``.

    For admin boundaries, filters to the region matching the PBF filename
    to exclude neighboring regions that overlap the extract.  Natural
    boundaries are filtered by centroid-in-bbox instead.
    """
    tag_filters = _build_tag_filters(admin_levels, natural_types)
    if not tag_filters:
        return []

    admin_set = set(admin_levels) if admin_levels else set()
    natural_list = natural_types or []
    region_name = _region_name_from_pbf(pbf_path)
    bbox = _get_pbf_bbox(pbf_path) if natural_types else None

    with tempfile.NamedTemporaryFile(suffix=".osm", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [
            "osmium",
            "tags-filter",
            pbf_path,
            *tag_filters,
            "-f",
            "osm",
            "-o",
            tmp_path,
            "--overwrite",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.warning("osmium tags-filter failed: %s", result.stderr[:500])
            return []

        with open(tmp_path) as f:
            xml = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    geojson = osm2geojson.xml2geojson(xml)
    features: list[BoundaryFeature] = []

    for feat in geojson.get("features", []):
        props = feat.get("properties", {})
        tags = props.get("tags", {})
        osm_id = props.get("id", 0)
        osm_type = props.get("type", "relation")
        name = tags.get("name", "")
        geometry = feat.get("geometry")

        # Match against requested criteria
        if _matches_admin(tags, admin_set):
            # For admin boundaries, filter to the region matching the PBF
            # filename to exclude neighboring states/counties
            if region_name and not _name_matches_region(name, region_name):
                continue
            features.append(
                BoundaryFeature(
                    osm_id=osm_id,
                    osm_type=osm_type,
                    name=name,
                    admin_level=int(tags["admin_level"]),
                    boundary_type="administrative",
                    tags=tags,
                    geometry=geometry,
                )
            )
        elif natural_list:
            nt = _matches_natural(tags, natural_list)
            if nt:
                # For natural boundaries, use bbox filtering to exclude
                # features from neighboring extracts
                if bbox and geometry:
                    try:
                        from shapely.geometry import shape as _shape

                        pt = _shape(geometry).representative_point()
                        min_lon, min_lat, max_lon, max_lat = bbox
                        if not (min_lon <= pt.x <= max_lon and min_lat <= pt.y <= max_lat):
                            continue
                    except Exception:
                        pass
                features.append(
                    BoundaryFeature(
                        osm_id=osm_id,
                        osm_type=osm_type,
                        name=name,
                        admin_level=None,
                        boundary_type=nt,
                        tags=tags,
                        geometry=geometry,
                    )
                )

    return features


# ── Fallback: pyosmium ──────────────────────────────────────────────────


if HAS_OSMIUM:

    class _PyosmiumHandler(osmium.SimpleHandler):
        """Pyosmium fallback handler (no geometry for type=boundary)."""

        def __init__(
            self,
            admin_levels: list[int] | None = None,
            natural_types: list[str] | None = None,
            progress=None,
        ):
            super().__init__()
            self.admin_levels = set(admin_levels) if admin_levels else set()
            self.natural_types = natural_types or []
            self.features: list[BoundaryFeature] = []
            self._seen_ids: set[int] = set()
            self._progress = progress

            try:
                from shapely import wkb as _wkb
                from shapely.geometry import mapping as _mapping

                self._wkb = _wkb
                self._mapping = _mapping
            except ImportError:
                self._wkb = None
                self._mapping = None
            self._wkb_factory = osmium.geom.WKBFactory()

        def _extract_tags(self, tags) -> dict[str, str]:
            return {tag.k: tag.v for tag in tags}

        def _get_geometry(self, area_obj) -> dict[str, Any] | None:
            if not self._wkb or not self._wkb_factory:
                return None
            try:
                wkb_data = self._wkb_factory.create_multipolygon(area_obj)
                geom = self._wkb.loads(wkb_data, hex=True)
                return self._mapping(geom)
            except Exception:
                return None

        def _matches_natural_type(self, tags) -> str | None:
            type_maps = {
                "water": NATURAL_TYPE_WATER,
                "forest": NATURAL_TYPE_FOREST,
                "park": NATURAL_TYPE_PARK,
            }
            for nt in self.natural_types:
                for tag_key, tag_values in type_maps.get(nt, {}).items():
                    if tag_key in tags and tags[tag_key] in tag_values:
                        return nt
            return None

        def area(self, a) -> None:
            if self._progress:
                self._progress.tick("area")
            tags = a.tags
            if "boundary" in tags and tags["boundary"] == "administrative":
                try:
                    admin_level = int(tags.get("admin_level", ""))
                except ValueError:
                    admin_level = None
                if admin_level is not None and admin_level in self.admin_levels:
                    oid = a.orig_id()
                    self._seen_ids.add(oid)
                    self.features.append(
                        BoundaryFeature(
                            osm_id=oid,
                            osm_type="relation" if a.from_way() is False else "way",
                            name=tags.get("name", ""),
                            admin_level=admin_level,
                            boundary_type="administrative",
                            tags=self._extract_tags(tags),
                            geometry=self._get_geometry(a),
                        )
                    )
                    return

            natural_type = self._matches_natural_type(tags)
            if natural_type:
                oid = a.orig_id()
                self._seen_ids.add(oid)
                self.features.append(
                    BoundaryFeature(
                        osm_id=oid,
                        osm_type="relation" if a.from_way() is False else "way",
                        name=tags.get("name", ""),
                        admin_level=None,
                        boundary_type=natural_type,
                        tags=self._extract_tags(tags),
                        geometry=self._get_geometry(a),
                    )
                )

        def relation(self, r) -> None:
            if self._progress:
                self._progress.tick("relation")
            tags = r.tags
            if "boundary" in tags and tags["boundary"] == "administrative":
                try:
                    admin_level = int(tags.get("admin_level", ""))
                except ValueError:
                    return
                if admin_level in self.admin_levels and r.id not in self._seen_ids:
                    self._seen_ids.add(r.id)
                    self.features.append(
                        BoundaryFeature(
                            osm_id=r.id,
                            osm_type="relation",
                            name=tags.get("name", ""),
                            admin_level=admin_level,
                            boundary_type="administrative",
                            tags=self._extract_tags(tags),
                            geometry=None,
                        )
                    )
                    return

            natural_type = self._matches_natural_type(tags)
            if natural_type and r.id not in self._seen_ids:
                self._seen_ids.add(r.id)
                self.features.append(
                    BoundaryFeature(
                        osm_id=r.id,
                        osm_type="relation",
                        name=tags.get("name", ""),
                        admin_level=None,
                        boundary_type=natural_type,
                        tags=self._extract_tags(tags),
                        geometry=None,
                    )
                )


def _extract_via_pyosmium(
    pbf_path: str,
    admin_levels: list[int] | None,
    natural_types: list[str] | None,
    step_log=None,
) -> list[BoundaryFeature]:
    """Fallback extraction via pyosmium (no geometry for type=boundary)."""
    file_size = get_file_size(pbf_path)
    progress = ScanProgressTracker(file_size, step_log, label="Boundaries")
    handler = _PyosmiumHandler(
        admin_levels=admin_levels, natural_types=natural_types, progress=progress
    )
    handler.apply_file(pbf_path, locations=True, idx="flex_mem")
    progress.finish()
    return handler.features


# ── Public API ──────────────────────────────────────────────────────────


def extract_boundaries(
    pbf_path: str | Path,
    admin_levels: list[int] | None = None,
    natural_types: list[str] | None = None,
    output_dir: Path | None = None,
) -> ExtractionResult:
    """Extract boundaries from a PBF file and write to GeoJSON.

    Uses osmium-tool + osm2geojson when available (full geometry for all
    relation types).  Falls back to pyosmium (geometry only for
    type=multipolygon relations).

    Args:
        pbf_path: Path to the OSM PBF file
        admin_levels: List of admin_level values to extract (e.g., [2, 4])
        natural_types: List of natural boundary types (e.g., ["water", "forest", "park"])
        output_dir: Directory to write output files

    Returns:
        ExtractionResult with output path and statistics
    """
    if not HAS_OSMIUM and not (HAS_OSMIUM_TOOL and HAS_OSM2GEOJSON):
        raise ImportError(
            "pyosmium or (osmium-tool + osm2geojson) required for boundary extraction"
        )

    pbf_str = str(pbf_path)
    backend = get_storage_backend(pbf_str)
    if not backend.exists(pbf_str):
        raise FileNotFoundError(f"PBF file not found: {pbf_path}")
    local_pbf = localize(pbf_str)

    if output_dir is None:
        out_base = resolve_output_dir("osm-boundaries")
    else:
        out_base = str(output_dir)

    # Build descriptive filename
    pbf_stem = Path(local_pbf).stem
    parts = [pbf_stem]
    if admin_levels:
        parts.append(f"admin{'-'.join(str(lvl) for lvl in sorted(admin_levels))}")
    if natural_types:
        parts.append("-".join(natural_types))
    output_name = "_".join(parts) + ".geojson"
    output_path = f"{out_base}/{output_name}"
    ensure_dir(output_path)

    # Primary: osmium-tool + osm2geojson (proper geometry for all types)
    if HAS_OSMIUM_TOOL and HAS_OSM2GEOJSON:
        features = _extract_via_osm2geojson(local_pbf, admin_levels, natural_types)
    elif HAS_OSMIUM:
        features = _extract_via_pyosmium(local_pbf, admin_levels, natural_types)
    else:
        features = []

    # Convert to GeoJSON FeatureCollection
    geojson = _features_to_geojson(features)

    with open_output(output_path) as f:
        json.dump(geojson, f, ensure_ascii=False, indent=2)

    log.info("Extracted %d boundaries to %s", len(features), output_path)

    return ExtractionResult(
        output_path=output_path,
        feature_count=len(features),
        boundary_type=_describe_boundary_type(admin_levels, natural_types),
        admin_levels=",".join(str(lvl) for lvl in (admin_levels or [])),
    )


def _features_to_geojson(features: list[BoundaryFeature]) -> dict[str, Any]:
    """Convert extracted features to GeoJSON FeatureCollection."""
    geojson_features = []
    for feat in features:
        properties = {
            "osm_id": feat.osm_id,
            "osm_type": feat.osm_type,
            "name": feat.name,
            "boundary_type": feat.boundary_type,
        }
        if feat.admin_level is not None:
            properties["admin_level"] = feat.admin_level
        properties.update(feat.tags)

        geojson_features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": feat.geometry,
            }
        )

    return {
        "type": "FeatureCollection",
        "features": geojson_features,
    }


def _describe_boundary_type(admin_levels: list[int] | None, natural_types: list[str] | None) -> str:
    """Generate a human-readable description of the boundary type."""
    parts = []
    if admin_levels:
        level_names = {2: "country", 4: "state", 6: "county", 8: "city"}
        parts.extend(level_names.get(lvl, f"admin{lvl}") for lvl in admin_levels)
    if natural_types:
        parts.extend(natural_types)
    return ", ".join(parts) if parts else "all"
