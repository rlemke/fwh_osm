"""Base class and types for extractor plugins.

Each plugin declares which OSM element types and tags it cares about,
then receives matching elements via process_* callbacks.  Features are
streamed to disk via :class:`GeoJSONStreamWriter` during the scan —
plugins no longer need to accumulate features in memory.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Any

from ..shared._output import ensure_dir, open_output
from ..shared.geojson_writer import GeoJSONStreamWriter

log = logging.getLogger(__name__)


class ElementType(Flag):
    """OSM element types a plugin can process."""

    NODE = auto()
    WAY = auto()
    AREA = auto()
    RELATION = auto()


@dataclass
class TagInterest:
    """Declares which tags a plugin is interested in.

    *keys* — match if ANY of these keys is present (e.g. ``{"amenity", "shop"}``)
    *key_values* — match if key=value (e.g. ``{"highway": {"cycleway", "path"}}``)

    A tag dict matches if it satisfies *keys* OR *key_values*.
    """

    keys: set[str] = field(default_factory=set)
    key_values: dict[str, set[str]] = field(default_factory=dict)

    def matches(self, tags: dict[str, str]) -> bool:
        """Return True if tags match this interest."""
        for key in self.keys:
            if key in tags:
                return True
        for key, values in self.key_values.items():
            if key in tags and tags[key] in values:
                return True
        return False


@dataclass
class PluginResult:
    """Per-plugin extraction result."""

    category: str
    output_path: str
    feature_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ExtractorPlugin(ABC):
    """Abstract base for combined-scan extractor plugins.

    Plugins stream features to disk via :class:`GeoJSONStreamWriter`.
    Call :meth:`begin` before the scan to open the output file, then
    write features via ``self._writer.write_feature(...)`` in the
    ``process_*`` callbacks.  :meth:`finalize` closes the writer and
    returns the result.
    """

    _writer: GeoJSONStreamWriter | None = None

    @property
    @abstractmethod
    def category(self) -> str:
        """Short name used in the categories list (e.g. 'roads')."""

    @property
    @abstractmethod
    def element_types(self) -> ElementType:
        """Which element types this plugin processes."""

    @property
    @abstractmethod
    def tag_interest(self) -> TagInterest:
        """Tag filter for pre-screening elements."""

    def begin(self, pbf_stem: str, output_dir: str) -> None:  # noqa: B027
        """Open the output stream before scanning.

        Subclasses can override to open additional writers, but should
        call ``super().begin(...)`` or set ``self._writer`` themselves.
        """

    def process_node(  # noqa: B027
        self, node_id: int, tags: dict[str, str], lon: float, lat: float
    ) -> None:
        """Called for each matching node."""

    def process_way(  # noqa: B027
        self,
        way_id: int,
        tags: dict[str, str],
        coords: list[tuple[float, float]],
    ) -> None:
        """Called for each matching way.

        *coords* are already resolved via ``locations=True``.
        """

    def process_area(  # noqa: B027
        self,
        area_id: int,
        tags: dict[str, str],
        geometry: dict | None,
        orig_id: int,
        from_way: bool,
    ) -> None:
        """Called for each matching area.

        *geometry* is a GeoJSON dict extracted via WKBFactory, or None on error.
        """

    def process_relation(  # noqa: B027
        self, relation_id: int, tags: dict[str, str], members: list[dict]
    ) -> None:
        """Called for each matching relation."""

    @abstractmethod
    def finalize(self, pbf_stem: str, output_dir: str) -> PluginResult:
        """Close output stream and return result.  Called after the scan."""

    def _open_writer(self, output_path: str) -> GeoJSONStreamWriter:
        """Open a streaming GeoJSON writer for the given path.

        Uses atomic mode so the output file only appears once the scan
        completes successfully — prevents corrupt partial files when
        the process is killed mid-scan.
        """
        writer = GeoJSONStreamWriter(output_path, atomic=True)
        self._writer = writer
        return writer

    def _write_geojson(self, features: list[dict], output_path: str) -> int:
        """Helper: write a GeoJSON FeatureCollection and return feature count.

        .. deprecated:: Use streaming via ``_open_writer`` / ``_writer.write_feature`` instead.
        """
        ensure_dir(output_path)
        geojson = {"type": "FeatureCollection", "features": features}
        with open_output(output_path) as f:
            json.dump(geojson, f)
        return len(features)
