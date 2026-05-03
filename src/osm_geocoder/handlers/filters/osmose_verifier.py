"""Standalone local PBF/GeoJSON verifier for OSM data quality analysis.

Performs deep quality checks on .osm.pbf files (from GeoFabrik, via OSMCache)
and GeoJSON files with no network dependency.  Single-pass PBF processing
(nodes -> ways -> relations) enables reference integrity checking without
a second pass.

Severity levels:
    1 (error)   — reference integrity failures, out-of-bounds coords,
                   degenerate geometry, duplicate IDs
    2 (warning) — missing name on named features, unclosed polygons
    3 (info)    — empty tag values
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from facetwork.runtime.storage import localize

from ..shared._output import ensure_dir, open_output, resolve_output_dir
from ..shared.scan_progress import ScanProgressTracker, get_file_size

log = logging.getLogger(__name__)

try:
    import osmium

    HAS_OSMIUM = True
except ImportError:
    HAS_OSMIUM = False
    osmium = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Result of a verification operation."""

    output_path: str
    issue_count: int
    node_count: int
    way_count: int
    relation_count: int
    format: str = "GeoJSON"
    verify_date: str = ""


@dataclass
class VerifySummaryData:
    """Aggregate statistics from a verification run."""

    total_issues: int = 0
    geometry_issues: int = 0
    tag_issues: int = 0
    reference_issues: int = 0
    coordinate_issues: int = 0
    duplicate_issues: int = 0
    level_1: int = 0
    level_2: int = 0
    level_3: int = 0
    tag_coverage_pct: float = 0.0
    avg_tags_per_element: float = 0.0
    verify_date: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NAMED_FEATURE_KEYS = frozenset(
    {
        "amenity",
        "shop",
        "tourism",
        "leisure",
        "office",
        "building",
        "highway",
        "railway",
        "aeroway",
        "waterway",
        "place",
        "historic",
        "natural",
    }
)

_POLYGON_TAG_KEYS = frozenset(
    {
        "building",
        "landuse",
        "natural",
        "leisure",
        "amenity",
        "area",
        "boundary",
        "place",
    }
)


def _should_have_name(tags: dict[str, str]) -> bool:
    """Return True if the element is a named feature type that should have a name tag."""
    return bool(_NAMED_FEATURE_KEYS & tags.keys())


def _is_polygon_tagged(tags: dict[str, str]) -> bool:
    """Return True if the element has tags suggesting it should be a closed polygon."""
    return bool(_POLYGON_TAG_KEYS & tags.keys())


def _make_issue(
    issue_type: str,
    level: int,
    message: str,
    element_type: str,
    element_id: int,
    lon: float | None = None,
    lat: float | None = None,
) -> dict[str, Any]:
    """Build a GeoJSON Feature for a single issue."""
    coords = [lon or 0.0, lat or 0.0]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coords},
        "properties": {
            "issue_type": issue_type,
            "level": level,
            "message": message,
            "element_type": element_type,
            "element_id": element_id,
        },
    }


# ---------------------------------------------------------------------------
# VerificationHandler — single-pass pyosmium handler
# ---------------------------------------------------------------------------


class VerificationHandler(osmium.SimpleHandler if HAS_OSMIUM else object):  # type: ignore[misc]
    """Single-pass PBF handler that collects data quality issues."""

    def __init__(
        self,
        *,
        check_geometry: bool = True,
        check_tags: bool = True,
        check_references: bool = True,
        check_coordinates: bool = True,
        check_duplicates: bool = True,
        required_tags: list[str] | None = None,
        progress: ScanProgressTracker | None = None,
    ) -> None:
        if HAS_OSMIUM:
            super().__init__()
        self.check_geometry = check_geometry
        self.check_tags = check_tags
        self.check_references = check_references
        self.check_coordinates = check_coordinates
        self.check_duplicates = check_duplicates
        self.required_tags = required_tags or []
        self._progress = progress

        # Collected issues
        self.issues: list[dict[str, Any]] = []

        # Reference tracking sets
        self._node_ids: set[int] = set()
        self._way_ids: set[int] = set()

        # Counters
        self.node_count: int = 0
        self.way_count: int = 0
        self.relation_count: int = 0
        self.total_tag_count: int = 0
        self.elements_with_tags: int = 0

    # -- helpers --

    @staticmethod
    def _tags_to_dict(tags) -> dict[str, str]:
        return {t.k: t.v for t in tags}

    def _check_tags_common(
        self,
        tags: dict[str, str],
        element_type: str,
        element_id: int,
        lon: float | None,
        lat: float | None,
    ) -> None:
        """Tag checks shared across node/way/relation."""
        tag_count = len(tags)
        self.total_tag_count += tag_count
        if tag_count > 0:
            self.elements_with_tags += 1

        # Level 3: empty tag values
        for k, v in tags.items():
            if v == "":
                self.issues.append(
                    _make_issue(
                        "tag",
                        3,
                        f"Empty value for tag '{k}'",
                        element_type,
                        element_id,
                        lon,
                        lat,
                    )
                )

        # Level 2: missing name on named features
        if _should_have_name(tags) and "name" not in tags:
            self.issues.append(
                _make_issue(
                    "tag",
                    2,
                    "Missing 'name' tag on named feature",
                    element_type,
                    element_id,
                    lon,
                    lat,
                )
            )

        # Required tags
        for rt in self.required_tags:
            if rt not in tags:
                self.issues.append(
                    _make_issue(
                        "tag",
                        2,
                        f"Missing required tag '{rt}'",
                        element_type,
                        element_id,
                        lon,
                        lat,
                    )
                )

    # -- pyosmium callbacks --

    def node(self, n) -> None:  # type: ignore[override]
        self.node_count += 1
        if self._progress:
            self._progress.tick("node")
        nid = n.id

        # Duplicate check
        if self.check_duplicates:
            if nid in self._node_ids:
                self.issues.append(
                    _make_issue(
                        "duplicate",
                        1,
                        f"Duplicate node ID {nid}",
                        "node",
                        nid,
                    )
                )
        self._node_ids.add(nid)

        # Coordinate checks
        try:
            lon = n.location.lon
            lat = n.location.lat
        except osmium.InvalidLocationError:
            if self.check_coordinates:
                self.issues.append(
                    _make_issue(
                        "coordinate",
                        1,
                        "Invalid/missing location",
                        "node",
                        nid,
                    )
                )
            return

        if self.check_coordinates:
            if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
                self.issues.append(
                    _make_issue(
                        "coordinate",
                        1,
                        f"Out-of-bounds coordinates ({lon}, {lat})",
                        "node",
                        nid,
                        lon,
                        lat,
                    )
                )
            if lon == 0.0 and lat == 0.0:
                self.issues.append(
                    _make_issue(
                        "coordinate",
                        1,
                        "Null Island (0, 0) coordinates",
                        "node",
                        nid,
                        lon,
                        lat,
                    )
                )

        # Tag checks
        if self.check_tags:
            tags = self._tags_to_dict(n.tags)
            self._check_tags_common(tags, "node", nid, lon, lat)

    def way(self, w) -> None:  # type: ignore[override]
        self.way_count += 1
        if self._progress:
            self._progress.tick("way")
        wid = w.id

        # Duplicate check
        if self.check_duplicates:
            if wid in self._way_ids:
                self.issues.append(
                    _make_issue(
                        "duplicate",
                        1,
                        f"Duplicate way ID {wid}",
                        "way",
                        wid,
                    )
                )
        self._way_ids.add(wid)

        node_refs = [nd.ref for nd in w.nodes]

        # Geometry checks
        if self.check_geometry:
            if len(node_refs) < 2:
                self.issues.append(
                    _make_issue(
                        "geometry",
                        1,
                        f"Degenerate way with {len(node_refs)} node(s)",
                        "way",
                        wid,
                    )
                )

            tags = self._tags_to_dict(w.tags)
            if _is_polygon_tagged(tags) and len(node_refs) >= 2:
                if node_refs[0] != node_refs[-1]:
                    self.issues.append(
                        _make_issue(
                            "geometry",
                            2,
                            "Unclosed polygon-tagged way",
                            "way",
                            wid,
                        )
                    )

        # Reference integrity
        if self.check_references:
            for ref in node_refs:
                if ref not in self._node_ids:
                    self.issues.append(
                        _make_issue(
                            "reference",
                            1,
                            f"Way references unknown node {ref}",
                            "way",
                            wid,
                        )
                    )
                    break  # one issue per way is enough

        # Tag checks
        if self.check_tags:
            tags = self._tags_to_dict(w.tags)
            self._check_tags_common(tags, "way", wid, None, None)

    def relation(self, r) -> None:  # type: ignore[override]
        self.relation_count += 1
        if self._progress:
            self._progress.tick("relation")
        rid = r.id

        # Reference integrity
        if self.check_references:
            for m in r.members:
                mtype = m.type
                mref = m.ref
                if mtype == "n" and mref not in self._node_ids:
                    self.issues.append(
                        _make_issue(
                            "reference",
                            1,
                            f"Relation references unknown node {mref}",
                            "relation",
                            rid,
                        )
                    )
                    break
                if mtype == "w" and mref not in self._way_ids:
                    self.issues.append(
                        _make_issue(
                            "reference",
                            1,
                            f"Relation references unknown way {mref}",
                            "relation",
                            rid,
                        )
                    )
                    break

        # Tag checks
        if self.check_tags:
            tags = self._tags_to_dict(r.tags)
            self._check_tags_common(tags, "relation", rid, None, None)


def _build_summary(handler: VerificationHandler) -> VerifySummaryData:
    """Build a VerifySummaryData from a completed handler."""
    geometry = 0
    tag = 0
    reference = 0
    coordinate = 0
    duplicate = 0
    lv1 = 0
    lv2 = 0
    lv3 = 0

    for issue in handler.issues:
        props = issue["properties"]
        itype = props["issue_type"]
        level = props["level"]

        if itype == "geometry":
            geometry += 1
        elif itype == "tag":
            tag += 1
        elif itype == "reference":
            reference += 1
        elif itype == "coordinate":
            coordinate += 1
        elif itype == "duplicate":
            duplicate += 1

        if level == 1:
            lv1 += 1
        elif level == 2:
            lv2 += 1
        elif level == 3:
            lv3 += 1

    total_elements = handler.node_count + handler.way_count + handler.relation_count
    tag_coverage = (
        (handler.elements_with_tags / total_elements * 100.0) if total_elements > 0 else 0.0
    )
    avg_tags = (handler.total_tag_count / total_elements) if total_elements > 0 else 0.0

    return VerifySummaryData(
        total_issues=len(handler.issues),
        geometry_issues=geometry,
        tag_issues=tag,
        reference_issues=reference,
        coordinate_issues=coordinate,
        duplicate_issues=duplicate,
        level_1=lv1,
        level_2=lv2,
        level_3=lv3,
        tag_coverage_pct=round(tag_coverage, 2),
        avg_tags_per_element=round(avg_tags, 4),
        verify_date=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_pbf(
    pbf_path: str,
    output_dir: str | None = None,
    *,
    check_geometry: bool = True,
    check_tags: bool = True,
    check_references: bool = True,
    check_coordinates: bool = True,
    check_duplicates: bool = True,
    required_tags: list[str] | None = None,
    step_log=None,
) -> tuple[VerifyResult, VerifySummaryData]:
    """Run verification on a .osm.pbf file and write issues GeoJSON.

    Args:
        pbf_path: Path to the .osm.pbf file.
        output_dir: Base directory for output (subdirectory osm-osmose/ is created).
        check_geometry: Enable geometry checks (degenerate ways, unclosed polygons).
        check_tags: Enable tag checks (empty values, missing names).
        check_references: Enable reference integrity checks.
        check_coordinates: Enable coordinate range checks.
        check_duplicates: Enable duplicate ID checks.
        required_tags: Additional tags that must be present on tagged elements.

    Returns:
        Tuple of (VerifyResult, VerifySummaryData).
    """
    if output_dir is None:
        import os

        from facetwork.config import get_output_base

        output_dir = os.path.join(get_output_base(), "osm", "osmose")

    if not HAS_OSMIUM:
        raise RuntimeError("pyosmium is required for PBF verification")

    local_path = localize(str(pbf_path))
    file_size = get_file_size(str(local_path))
    progress = ScanProgressTracker(file_size, step_log, label="OSMOSE Verify")

    handler = VerificationHandler(
        check_geometry=check_geometry,
        check_tags=check_tags,
        check_references=check_references,
        check_coordinates=check_coordinates,
        check_duplicates=check_duplicates,
        required_tags=required_tags,
        progress=progress,
    )

    handler.apply_file(str(local_path), locations=True)
    progress.finish()

    # Write issues GeoJSON
    out_subdir = resolve_output_dir("osm-osmose", default_local=output_dir)
    output_path = f"{out_subdir}/verify-issues.geojson"
    ensure_dir(output_path)

    geojson = {"type": "FeatureCollection", "features": handler.issues}
    with open_output(output_path) as f:
        json.dump(geojson, f)

    verify_date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = _build_summary(handler)

    result = VerifyResult(
        output_path=output_path,
        issue_count=len(handler.issues),
        node_count=handler.node_count,
        way_count=handler.way_count,
        relation_count=handler.relation_count,
        verify_date=verify_date,
    )

    return result, summary


def verify_geojson(
    input_path: str,
    output_dir: str | None = None,
) -> tuple[VerifyResult, VerifySummaryData]:
    """Validate a GeoJSON file for structure, geometries, and coordinate ranges.

    Args:
        input_path: Path to the GeoJSON file.
        output_dir: Base directory for output.

    Returns:
        Tuple of (VerifyResult, VerifySummaryData).
    """
    if output_dir is None:
        import os

        from facetwork.config import get_output_base

        output_dir = os.path.join(get_output_base(), "osm", "osmose")

    issues: list[dict[str, Any]] = []

    try:
        with open(input_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("Failed to load GeoJSON: %s", e)
        return _empty_verify_result(), VerifySummaryData()

    # Structure checks
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        issues.append(
            _make_issue(
                "geometry",
                1,
                "GeoJSON root is not a FeatureCollection",
                "file",
                0,
            )
        )

    features = data.get("features", [])
    node_count = 0
    geometry_issues = 0
    tag_issues = 0
    coordinate_issues = 0
    lv1 = 0
    lv2 = 0
    lv3 = 0
    elements_with_props = 0
    total_props = 0

    for idx, feat in enumerate(features):
        fid = idx + 1
        node_count += 1

        # Geometry validation
        geom = feat.get("geometry")
        if geom is None:
            issues.append(_make_issue("geometry", 1, "Feature missing geometry", "feature", fid))
            geometry_issues += 1
            lv1 += 1
            continue

        geom_type = geom.get("type", "")
        coords = geom.get("coordinates")

        if not geom_type or coords is None:
            issues.append(_make_issue("geometry", 1, "Invalid geometry structure", "feature", fid))
            geometry_issues += 1
            lv1 += 1
            continue

        # Coordinate range check on Point geometries
        if geom_type == "Point" and isinstance(coords, list) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
            if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
                issues.append(
                    _make_issue(
                        "coordinate",
                        1,
                        f"Out-of-bounds coordinates ({lon}, {lat})",
                        "feature",
                        fid,
                        lon,
                        lat,
                    )
                )
                coordinate_issues += 1
                lv1 += 1

        # Property completeness
        props = feat.get("properties") or {}
        prop_count = len(props)
        total_props += prop_count
        if prop_count > 0:
            elements_with_props += 1

        for k, v in props.items():
            if v == "" or v is None:
                issues.append(
                    _make_issue(
                        "tag",
                        3,
                        f"Empty property '{k}'",
                        "feature",
                        fid,
                    )
                )
                tag_issues += 1
                lv3 += 1

    # Write issues GeoJSON
    out_subdir = resolve_output_dir("osm-osmose", default_local=output_dir)
    output_path = f"{out_subdir}/verify-issues.geojson"
    ensure_dir(output_path)

    geojson_out = {"type": "FeatureCollection", "features": issues}
    with open_output(output_path) as f:
        json.dump(geojson_out, f)

    verify_date = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag_coverage = (elements_with_props / node_count * 100.0) if node_count > 0 else 0.0
    avg_tags = (total_props / node_count) if node_count > 0 else 0.0

    result = VerifyResult(
        output_path=output_path,
        issue_count=len(issues),
        node_count=node_count,
        way_count=0,
        relation_count=0,
        verify_date=verify_date,
    )

    summary = VerifySummaryData(
        total_issues=len(issues),
        geometry_issues=geometry_issues,
        tag_issues=tag_issues,
        reference_issues=0,
        coordinate_issues=coordinate_issues,
        duplicate_issues=0,
        level_1=lv1,
        level_2=lv2,
        level_3=lv3,
        tag_coverage_pct=round(tag_coverage, 2),
        avg_tags_per_element=round(avg_tags, 4),
        verify_date=verify_date,
    )

    return result, summary


def compute_verify_summary(input_path: str) -> VerifySummaryData:
    """Read a verification GeoJSON and tally issues by type and severity.

    Args:
        input_path: Path to verify-issues.geojson.

    Returns:
        VerifySummaryData with tallied counts.
    """
    try:
        with open(input_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("Failed to load verification data: %s", e)
        return VerifySummaryData()

    features = data.get("features", [])

    geometry = 0
    tag = 0
    reference = 0
    coordinate = 0
    duplicate = 0
    lv1 = 0
    lv2 = 0
    lv3 = 0

    for feat in features:
        props = feat.get("properties", {})
        itype = props.get("issue_type", "")
        level = props.get("level", 0)

        if itype == "geometry":
            geometry += 1
        elif itype == "tag":
            tag += 1
        elif itype == "reference":
            reference += 1
        elif itype == "coordinate":
            coordinate += 1
        elif itype == "duplicate":
            duplicate += 1

        if level == 1:
            lv1 += 1
        elif level == 2:
            lv2 += 1
        elif level == 3:
            lv3 += 1

    return VerifySummaryData(
        total_issues=len(features),
        geometry_issues=geometry,
        tag_issues=tag,
        reference_issues=reference,
        coordinate_issues=coordinate,
        duplicate_issues=duplicate,
        level_1=lv1,
        level_2=lv2,
        level_3=lv3,
        verify_date=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _empty_verify_result() -> VerifyResult:
    """Return an empty VerifyResult."""
    return VerifyResult(
        output_path="",
        issue_count=0,
        node_count=0,
        way_count=0,
        relation_count=0,
    )
