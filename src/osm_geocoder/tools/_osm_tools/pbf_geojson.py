"""Shared PBF → GeoJSON conversion library.

Single source of truth for converting cached PBFs to GeoJSON via
``osmium export``. Used by both the ``convert-pbf-geojson`` CLI tool and
the FFL ``osm.ops.ConvertPbfToGeoJson`` handler.

Per-region ``threading.Lock`` serializes in-process concurrent calls so
only one conversion happens per region. No global manifest lock is
needed — sidecars are per-entry.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _osm_tools import sidecar
from _osm_tools.storage import (
    LocalStorage,
    Storage,
    get_storage,
    local_staging_subdir,
)

NAMESPACE = "osm"
SOURCE_CACHE_TYPE = "pbf"
OUTPUT_CACHE_TYPE = "geojson"
FORMAT_EXT = {"geojson": "geojson", "geojsonseq": "geojsonseq"}
DEFAULT_FORMAT = "geojsonseq"
CHUNK_SIZE = 1024 * 1024

_region_locks: dict[str, threading.Lock] = {}
_region_locks_guard = threading.Lock()


def _region_lock(region: str, fmt: str) -> threading.Lock:
    key = f"{region}::{fmt}"
    with _region_locks_guard:
        lock = _region_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _region_locks[key] = lock
        return lock


@dataclass
class ConvertResult:
    """Outcome of a ``convert_region`` call."""

    region: str
    path: str
    relative_path: str
    format: str
    size_bytes: int
    sha256: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    skipped: bool = False
    sidecar: dict[str, Any] = field(default_factory=dict)


class ConversionError(RuntimeError):
    """Raised when a conversion fails (osmium failure, missing PBF, etc.)."""


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def geojson_rel_path(region: str, fmt: str) -> str:
    return f"{region}-latest.{FORMAT_EXT[fmt]}"


def geojson_abs_path(region: str, fmt: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, geojson_rel_path(region, fmt), s))


def _osmium_version(osmium_bin: str) -> str:
    try:
        result = subprocess.run(
            [osmium_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or "").splitlines()
        return first_line[0].strip() if first_line else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha256_file(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def _staging_path(region: str, fmt: str, storage: Any = None) -> Path:
    """Local path for osmium's output before it's finalized to ``storage``.

    On object-store backends (S3/MinIO, HDFS) the destination is an ``s3://`` /
    ``hdfs://`` URI — NOT a local path — so staging MUST live on local scratch;
    only ``finalize_from_local`` uploads it. Staging "adjacent to destination"
    there would resolve, via ``geojson_abs_path``'s ``Path()``, to a mangled
    ``s3:/…`` path written on the container's internal disk (which previously
    ENOSPC-crashed mongo). ``local_staging_subdir`` roots staging at
    ``FW_LOCAL_SCRATCH`` (the external scratch disk). For local storage, stage
    adjacent to the destination (same FS → atomic rename).
    """
    s = storage or get_storage()
    safe = region.replace("/", "_")
    fname = f"{safe}-latest.{FORMAT_EXT[fmt]}.tmp"
    force_local_scratch = (os.environ.get("FW_CONVERT_STAGING") or "").lower() == "tmp"
    if force_local_scratch or getattr(s, "name", "local") != "local":
        return Path(local_staging_subdir("facetwork-geojson-staging")) / fname
    out = geojson_abs_path(region, fmt, s)
    return out.with_name(out.name + ".tmp")


def is_up_to_date(
    region: str,
    fmt: str,
    pbf_side: dict,
    out_path: Any,
    storage: Any = None,
) -> bool:
    """True if the cached GeoJSON still matches the source PBF's SHA-256.

    ``out_path`` may be a local path or an ``s3://``/``hdfs://`` URI; existence
    and size are checked via the storage backend so this works on any backend.
    """
    s = storage or get_storage()
    out_path = str(out_path)
    out_rel = geojson_rel_path(region, fmt)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s)
    if not existing:
        return False
    extra = existing.get("extra") or {}
    if extra.get("format") != fmt:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    if not s.exists(out_path):
        return False
    return s.size(out_path) == existing.get("size_bytes")


def convert_region(
    region: str,
    *,
    fmt: str = DEFAULT_FORMAT,
    force: bool = False,
    osmium_bin: str = "osmium",
    storage: Any = None,
    max_pbf_mb: int = 0,
) -> ConvertResult:
    """Convert a region's PBF to GeoJSON. Thread-safe per (region, fmt).

    Storage-agnostic: the cached PBF is localized (downloaded from S3/MinIO or
    HDFS to a local cache, a no-op for local storage) before osmium reads it,
    and the GeoJSON output is finalized back to the same backend — so on
    ``FW_STORAGE=s3`` both the read and the write go through MinIO.

    ``max_pbf_mb`` (0 = no limit) skips regions whose cached PBF exceeds the
    limit, returning ``ConvertResult(skipped=True)`` WITHOUT localizing or
    converting (so an oversized PBF isn't even downloaded). Falls back to the
    ``FW_OSM_MAX_PBF_MB`` env var when the argument is 0. This lives here in the
    shared library — not in any one handler — so every caller (the
    ``convert-pbf-geojson`` CLI, the FFL ``osm.Source.PBF.ToGeoJson`` handler,
    and any other consumer) gets the same size-gate behaviour.
    """
    if fmt not in FORMAT_EXT:
        raise ConversionError(f"unknown format: {fmt!r} (valid: {', '.join(FORMAT_EXT)})")
    s = storage or get_storage()

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise ConversionError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )

    # Size gate (shared by CLI + handler): skip oversized PBFs BEFORE localizing.
    effective_max = int(max_pbf_mb or 0) or int(os.environ.get("FW_OSM_MAX_PBF_MB") or 0)
    pbf_bytes = int(pbf_side.get("size_bytes") or 0)
    if effective_max > 0 and pbf_bytes > effective_max * 1024 * 1024:
        return ConvertResult(
            region=region,
            path="",
            relative_path="",
            format=fmt,
            size_bytes=pbf_bytes,
            sha256="",
            generated_at="",
            duration_seconds=0.0,
            was_cached=False,
            source_url=pbf_side.get("source", {}).get("url", ""),
            source_pbf_path="",
            skipped=True,
        )

    # Use the raw cache *string* (not pbf_abs_path's Path, which collapses the
    # "s3://" double slash to "s3:/") so the storage ops get a valid URI.
    src_uri = sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not s.exists(src_uri):
        raise ConversionError(f"pbf file missing in storage: {src_uri}")
    # osmium can only read a local file — localize the cached PBF (downloads
    # from MinIO/HDFS; no-op on local storage).
    src_pbf = Path(s.localize(src_uri))
    source_url = pbf_side.get("source", {}).get("url", "")

    with _region_lock(region, fmt):
        out_rel = geojson_rel_path(region, fmt)
        # Destination as a raw storage *string* (not geojson_abs_path's Path,
        # which collapses "s3://" → "s3:/" and yields an empty bucket on write).
        out_uri = sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s)

        if not force and is_up_to_date(region, fmt, pbf_side, out_uri, s):
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s) or {}
            return ConvertResult(
                region=region,
                path=out_uri,
                relative_path=out_rel,
                format=fmt,
                size_bytes=existing.get("size_bytes", s.size(out_uri)),
                sha256=existing.get("sha256", ""),
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        s.mkdir_p(Storage.dirname(out_uri))  # local: mkdir; object stores: no-op
        staging = _staging_path(region, fmt, s)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()

        # Disk-backed node-location index on local scratch (alongside staging),
        # so osmium does NOT hold every node's coordinates in RAM. The default
        # flex_mem index grows unbounded and OOM-kills osmium on large regions
        # (continents need GBs just for the index) on memory-constrained hosts.
        # sparse_file_array suits OSM extracts (sparse global node IDs); override
        # with FW_OSMIUM_INDEX_TYPE (e.g. flex_mem for small-region speed).
        index_type = os.environ.get("FW_OSMIUM_INDEX_TYPE", "sparse_file_array")
        idx_path = staging.with_name(staging.name + ".nodeidx")
        idx_path.unlink(missing_ok=True)
        index_arg = (
            f"{index_type},{idx_path}" if index_type.endswith("_file_array") else index_type
        )

        cmd = [
            osmium_bin,
            "export",
            "-i",
            index_arg,
            "-f",
            fmt,
            "-o",
            str(staging),
            "--overwrite",
            str(src_pbf),
        ]
        start = time.monotonic()
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            if staging.exists():
                staging.unlink()
            idx_path.unlink(missing_ok=True)
            stderr = (exc.stderr or "").strip()
            raise ConversionError(f"osmium export failed: {stderr or exc}") from exc
        except BaseException:
            if staging.exists():
                staging.unlink()
            idx_path.unlink(missing_ok=True)
            raise
        idx_path.unlink(missing_ok=True)  # success: drop the (large) on-disk index
        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(staging)

        s.finalize_from_local(str(staging), out_uri)

        generated_at = sidecar.utcnow_iso()
        side = sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            out_rel,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source={
                "namespace": NAMESPACE,
                "cache_type": SOURCE_CACHE_TYPE,
                "relative_path": pbf_rel,
                "sha256": pbf_side.get("sha256"),
                "size_bytes": pbf_side.get("size_bytes"),
                "source_checksum": pbf_side.get("source", {}).get("source_checksum"),
                "source_timestamp": pbf_side.get("source", {}).get("source_timestamp"),
                "downloaded_at": pbf_side.get("source", {}).get("downloaded_at"),
            },
            tool={
                "command": "osmium export",
                "osmium_version": _osmium_version(osmium_bin),
            },
            extra={
                "region": region,
                "format": fmt,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return ConvertResult(
            region=region,
            path=out_uri,
            relative_path=out_rel,
            format=fmt,
            size_bytes=size,
            sha256=sha256_hex,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_url=source_url,
            source_pbf_path=str(src_pbf),
            sidecar=side,
        )


def to_osm_cache(
    result: ConvertResult, region: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Map a ``ConvertResult`` to the ``OSMCache`` dict FFL handlers return.

    ``region`` should be the typed ``osm.types.Region`` dict for the
    conversion's source. When None, a minimal placeholder is built from
    ``ConvertResult.region`` (the geofabrik path string).
    """
    if region is None:
        path = result.region
        region = {
            "query": "",
            "name": "",
            "canonical": path,
            "level": "",
            "level_label": "",
            "parent_canonical": "",
            "continent": "",
            "geofabrik_path": path,
        }
    return {
        "region": region,
        "url": result.source_url,
        "path": result.path,
        "date": result.generated_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "convert",
    }
