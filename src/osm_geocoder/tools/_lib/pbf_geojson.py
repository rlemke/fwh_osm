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

from _lib import sidecar
from _lib.storage import LocalStorage

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
    sidecar: dict[str, Any] = field(default_factory=dict)


class ConversionError(RuntimeError):
    """Raised when a conversion fails (osmium failure, missing PBF, etc.)."""


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def geojson_rel_path(region: str, fmt: str) -> str:
    return f"{region}-latest.{FORMAT_EXT[fmt]}"


def geojson_abs_path(region: str, fmt: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
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
    """Stage adjacent to destination unless ``AFL_CONVERT_STAGING=tmp``."""
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-geojson-staging" / f"{safe}-latest.{FORMAT_EXT[fmt]}.tmp"
    out = geojson_abs_path(region, fmt, storage)
    return out.with_name(out.name + ".tmp")


def is_up_to_date(
    region: str,
    fmt: str,
    pbf_side: dict,
    out_abs: Path,
    storage: Any = None,
) -> bool:
    """True if the cached GeoJSON still matches the source PBF's SHA-256."""
    s = storage or LocalStorage()
    out_rel = geojson_rel_path(region, fmt)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s)
    if not existing:
        return False
    extra = existing.get("extra") or {}
    if extra.get("format") != fmt:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == existing.get("size_bytes")


def convert_region(
    region: str,
    *,
    fmt: str = DEFAULT_FORMAT,
    force: bool = False,
    osmium_bin: str = "osmium",
    storage: Any = None,
) -> ConvertResult:
    """Convert a region's PBF to GeoJSON. Thread-safe per (region, fmt)."""
    if fmt not in FORMAT_EXT:
        raise ConversionError(f"unknown format: {fmt!r} (valid: {', '.join(FORMAT_EXT)})")
    s = storage or LocalStorage()

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise ConversionError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )
    src_pbf = pbf_abs_path(region, s)
    if not src_pbf.exists():
        raise ConversionError(f"pbf file missing on disk: {src_pbf}")
    source_url = pbf_side.get("source", {}).get("url", "")

    with _region_lock(region, fmt):
        out_abs = geojson_abs_path(region, fmt, s)
        out_rel = geojson_rel_path(region, fmt)

        if not force and is_up_to_date(region, fmt, pbf_side, out_abs, s):
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s) or {}
            return ConvertResult(
                region=region,
                path=str(out_abs),
                relative_path=out_rel,
                format=fmt,
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        out_abs.parent.mkdir(parents=True, exist_ok=True)
        staging = _staging_path(region, fmt, s)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()

        cmd = [
            osmium_bin,
            "export",
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
            stderr = (exc.stderr or "").strip()
            raise ConversionError(f"osmium export failed: {stderr or exc}") from exc
        except BaseException:
            if staging.exists():
                staging.unlink()
            raise
        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(staging)

        s.finalize_from_local(str(staging), str(out_abs))

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
            path=str(out_abs),
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


def to_osm_cache(result: ConvertResult) -> dict[str, Any]:
    """Map a ``ConvertResult`` to the ``OSMCache`` dict FFL handlers return."""
    return {
        "url": result.source_url,
        "path": result.path,
        "date": result.generated_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "convert",
    }
