"""Streaming GeoJSON FeatureCollection reader and writer.

Reads and writes features one at a time instead of accumulating in memory.
This is critical for multi-GB GeoJSON files that exceed available RAM
(e.g. 2.7 GB combined OSM extract in a 7.6 GB Docker container).

Reader: ``iter_geojson_features()`` yields features from a FeatureCollection
file using a line-based brace-depth parser — memory usage stays proportional
to one feature.

Writer: ``GeoJSONStreamWriter`` writes features incrementally, producing
valid GeoJSON output identical to ``json.dump({"type": "FeatureCollection",
"features": [...]}, f)``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from typing import IO

from facetwork.config import get_output_base, get_temp_dir

from ._output import ensure_dir, open_output

log = logging.getLogger(__name__)


def _is_large_file(path: str, threshold: int = 50 * 1024 * 1024) -> bool:
    """Check if a file exceeds the size threshold (default 50 MB)."""
    try:
        return os.path.getsize(path) > threshold
    except OSError:
        return False


def _is_compact_json(path: str) -> bool:
    """Check if a JSON file is compact (few lines relative to size).

    The streaming parser requires indented JSON with newlines between features.
    Compact single-line JSON must be loaded entirely.
    """
    try:
        with open(path) as f:
            # Read first 8 KB — if there are very few newlines, it's compact
            sample = f.read(8192)
            newlines = sample.count("\n")
            return newlines < 5
    except OSError:
        return False


def iter_geojson_features(
    path: str,
    heartbeat: callable | None = None,
    heartbeat_interval: float = 30.0,
) -> Iterator[dict]:
    """Iterate over features in a GeoJSON FeatureCollection.

    For large files (>50 MB), uses a line-based streaming parser that keeps
    memory proportional to one feature.  For small files, falls back to
    ``json.load()`` which is simpler and handles all JSON formatting styles.

    Args:
        path: Path to a GeoJSON FeatureCollection file
        heartbeat: Optional callback to call periodically
        heartbeat_interval: Seconds between heartbeat calls

    Yields:
        Feature dicts from the FeatureCollection
    """
    # For /Volumes/ paths (VirtioFS), use local cache to avoid GIL-blocking reads.
    # Check the local cache FIRST to avoid stat() calls on VirtioFS.
    read_path = path
    if path.startswith("/Volumes/"):
        local_dir = os.path.join(get_output_base(), "cache", "osm-local")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, os.path.basename(path))
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            log.info("Using cached local copy: %s", local_path)
            read_path = local_path
        else:
            try:
                import subprocess

                log.info("Copying %s -> %s", path, local_path)
                subprocess.run(["cp", path, local_path], check=True)
                log.info("Copy complete: %s", local_path)
                read_path = local_path
            except Exception as exc:
                log.warning("Failed to localize %s: %s", path, exc)

    if not _is_large_file(read_path):
        # Small file: just load it entirely (handles compact and indented JSON)
        with open(read_path) as f:
            geojson = json.load(f)
        yield from geojson.get("features", [])
        return

    # Large file: stream line by line — but only works for indented JSON.
    # Compact (single-line) JSON must be loaded entirely since the line-based
    # parser cannot split features without newlines.
    if _is_compact_json(read_path):
        log.info("Large compact JSON detected, loading entirely: %s", read_path)
        with open(read_path) as f:
            geojson = json.load(f)
        yield from geojson.get("features", [])
        return

    yield from _stream_features(read_path, heartbeat, heartbeat_interval)


def _stream_features(
    path: str,
    heartbeat: callable | None = None,
    heartbeat_interval: float = 30.0,
) -> Iterator[dict]:
    """Stream features from a large indented GeoJSON file.

    Uses brace-depth tracking to extract one feature object at a time.
    Assumes the file was written with ``json.dump(..., indent=2)`` or similar.
    """
    last_hb = time.monotonic()
    feature_buf: list[str] = []
    brace_depth = 0
    in_features_array = False
    feature_count = 0

    with open(path) as f:
        for line in f:
            stripped = line.strip()

            # Detect the start of the "features" array
            if not in_features_array:
                if '"features"' in stripped and "[" in stripped:
                    in_features_array = True
                    idx = stripped.index("[")
                    rest = stripped[idx + 1 :].strip()
                    if rest and rest not in ("]", "],", "]}"):
                        feature_buf.append(rest)
                        brace_depth = rest.count("{") - rest.count("}")
                continue

            # End of features array
            if stripped in ("]", "],", "]}"):
                if feature_buf:
                    text = "\n".join(feature_buf).rstrip().rstrip(",")
                    if text:
                        try:
                            yield json.loads(text)
                            feature_count += 1
                        except json.JSONDecodeError:
                            log.warning("Skipped malformed feature #%d", feature_count)
                break

            # Track brace depth for current feature
            if brace_depth == 0 and stripped.startswith("{"):
                feature_buf = [stripped]
                brace_depth = stripped.count("{") - stripped.count("}")
            elif brace_depth > 0:
                feature_buf.append(stripped)
                brace_depth += stripped.count("{") - stripped.count("}")

            # Feature complete when braces balance
            if brace_depth == 0 and feature_buf:
                text = "\n".join(feature_buf).rstrip().rstrip(",")
                try:
                    yield json.loads(text)
                    feature_count += 1
                except json.JSONDecodeError:
                    log.warning("Skipped malformed feature #%d", feature_count)
                feature_buf = []

                # Heartbeat
                if heartbeat and time.monotonic() - last_hb > heartbeat_interval:
                    heartbeat()
                    last_hb = time.monotonic()

    log.info("Streamed %d features from %s", feature_count, path)


class GeoJSONStreamWriter:
    """Write GeoJSON features to a file incrementally.

    Usage::

        with GeoJSONStreamWriter("/Volumes/afl_data/output/osm/roads.geojson") as w:
            w.write_feature({"type": "Feature", ...})
        print(w.feature_count)

    When *atomic* is True, writes go to a temporary file under ``AFL_OUTPUT_BASE/tmp``
    and are moved to *path* only on successful :meth:`close`.  This
    prevents corrupt partial files when the process is killed mid-scan
    (especially on VirtioFS mounts).
    """

    def __init__(self, path: str, *, atomic: bool = False) -> None:
        ensure_dir(path)
        self._count = 0
        self._closed = False
        self.path = path
        self._atomic = atomic
        self._tmp_path: str | None = None

        if atomic:
            import tempfile

            fd, self._tmp_path = tempfile.mkstemp(suffix=".geojson", dir=get_temp_dir())
            os.close(fd)
            self._f: IO = open(self._tmp_path, "w")
        else:
            self._f: IO = open_output(path)
        self._f.write('{"type": "FeatureCollection", "features": [')

    @property
    def feature_count(self) -> int:
        return self._count

    def write_feature(self, feature: dict) -> None:
        """Serialize and write a single GeoJSON Feature."""
        if self._closed:
            raise RuntimeError("Writer is closed")
        if self._count > 0:
            self._f.write(", ")
        self._f.write(json.dumps(feature, ensure_ascii=False))
        self._count += 1

    def close(self) -> None:
        """Write closing brackets and close the file handle.

        For atomic writers, moves the temp file to the final path.
        """
        if self._closed:
            return
        self._closed = True
        self._f.write("]}")
        self._f.close()
        if self._atomic and self._tmp_path:
            import shutil

            ensure_dir(self.path)
            shutil.move(self._tmp_path, self.path)
            self._tmp_path = None

    def __enter__(self) -> GeoJSONStreamWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is not None and self._atomic and self._tmp_path:
            # Exception during write — clean up temp file, don't move to final path
            self._closed = True
            try:
                self._f.close()
            except Exception:
                pass
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None
            return
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup of temp file if writer was not properly closed."""
        if self._tmp_path is not None:
            try:
                self._f.close()
            except Exception:
                pass
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
