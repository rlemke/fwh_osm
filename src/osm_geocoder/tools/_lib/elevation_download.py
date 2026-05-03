"""Elevation raster download library — Copernicus DEM GLO-30 via gdalwarp.

Pulls Copernicus DEM 30m tiles from the AWS Open Data registry and
mosaics them into a GeoTIFF cropped to a caller-supplied bbox. Each
call produces one entry at ``cache/elevation/srtm/<name>-latest.tif``
with a sibling ``.meta.json`` sidecar.

Namespace: ``elevation`` (not ``osm`` — elevation is its own domain).
Cache_type: ``srtm`` for Copernicus (same 1-degree-grid shape as SRTM).

Cache validity:
- bbox matches what the sidecar recorded, AND
- source identifier (``cop-dem-30m``) matches, AND
- ``elevation_version`` matches.
"""

from __future__ import annotations

import hashlib
import math
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

NAMESPACE = "elevation"
CACHE_TYPE = "srtm"
ELEVATION_VERSION = 1
CHUNK_SIZE = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 1800

SOURCE_COP_DEM_30M = "cop-dem-30m"
SUPPORTED_SOURCES = (SOURCE_COP_DEM_30M,)

_COP_DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(name: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _build_locks[name] = lock
        return lock


@dataclass
class DownloadResult:
    name: str
    path: str
    relative_path: str
    source: str
    bbox: tuple[float, float, float, float]
    tile_urls: list[str]
    size_bytes: int
    sha256: str
    elevation_version: int
    generated_at: str
    duration_seconds: float
    was_cached: bool
    sidecar: dict[str, Any] = field(default_factory=dict)


class ElevationError(RuntimeError):
    pass


def raster_rel_path(name: str) -> str:
    return f"{name}-latest.tif"


def raster_abs_path(name: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, CACHE_TYPE, raster_rel_path(name), s))


def _staging_path(name: str) -> Path:
    base = tempfile.gettempdir()
    safe = name.replace("/", "_")
    return Path(base) / "facetwork-elevation-staging" / f"{safe}-latest.tif"


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ElevationError("bbox must be (west, south, east, north)")
    w, s, e, n = bbox
    if not (-180.0 <= w < e <= 180.0):
        raise ElevationError(f"bbox longitudes out of range: west={w} east={e}")
    if not (-90.0 <= s < n <= 90.0):
        raise ElevationError(f"bbox latitudes out of range: south={s} north={n}")


def _cop_dem_tile_name(lat_deg: int, lon_deg: int) -> str:
    lat_prefix = "N" if lat_deg >= 0 else "S"
    lon_prefix = "E" if lon_deg >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_"
        f"{lat_prefix}{abs(lat_deg):02d}_00_"
        f"{lon_prefix}{abs(lon_deg):03d}_00_DEM"
    )


def _cop_dem_tile_url(lat_deg: int, lon_deg: int) -> str:
    name = _cop_dem_tile_name(lat_deg, lon_deg)
    return f"{_COP_DEM_BASE}/{name}/{name}.tif"


def _tiles_for_bbox(
    bbox: tuple[float, float, float, float], source: str
) -> list[str]:
    if source != SOURCE_COP_DEM_30M:
        raise ElevationError(f"unsupported source: {source!r}")
    w, s, e, n = bbox
    lat_start = math.floor(s)
    lat_end = math.floor(n - 1e-9)
    lon_start = math.floor(w)
    lon_end = math.floor(e - 1e-9)
    urls: list[str] = []
    for lat in range(lat_start, lat_end + 1):
        for lon in range(lon_start, lon_end + 1):
            urls.append(_cop_dem_tile_url(lat, lon))
    return urls


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


def _gdalwarp_version(gdalwarp_bin: str) -> str:
    try:
        r = subprocess.run(
            [gdalwarp_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = (r.stdout or r.stderr or "").splitlines()
        return out[0].strip() if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def is_up_to_date(
    name: str,
    source: str,
    bbox: tuple[float, float, float, float],
    storage: Any = None,
) -> bool:
    s = storage or LocalStorage()
    rel = raster_rel_path(name)
    existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
    if not existing:
        return False
    extra = existing.get("extra") or {}
    if extra.get("source") != source:
        return False
    if list(bbox) != extra.get("bbox"):
        return False
    if extra.get("elevation_version") != ELEVATION_VERSION:
        return False
    out_abs = raster_abs_path(name, s)
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == existing.get("size_bytes")


def download_elevation(
    name: str,
    bbox: tuple[float, float, float, float],
    *,
    source: str = SOURCE_COP_DEM_30M,
    force: bool = False,
    gdalwarp_bin: str = "gdalwarp",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> DownloadResult:
    """Download a cropped elevation raster for ``bbox`` under the name ``name``."""
    if not name or "/" in name:
        raise ElevationError(
            f"name must be non-empty and contain no '/': {name!r}"
        )
    _validate_bbox(bbox)
    if source not in SUPPORTED_SOURCES:
        raise ElevationError(
            f"unknown source: {source!r}. Supported: {', '.join(SUPPORTED_SOURCES)}"
        )
    s = storage or LocalStorage()

    with _build_lock(name):
        out_abs = raster_abs_path(name, s)
        rel = raster_rel_path(name)

        if not force and is_up_to_date(name, source, bbox, s):
            existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s) or {}
            extra = existing.get("extra") or {}
            return DownloadResult(
                name=name,
                path=str(out_abs),
                relative_path=rel,
                source=source,
                bbox=bbox,
                tile_urls=extra.get("tile_urls", []),
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                elevation_version=ELEVATION_VERSION,
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                sidecar=existing,
            )

        urls = _tiles_for_bbox(bbox, source)
        if not urls:
            raise ElevationError(
                f"no tiles computed for bbox={bbox} source={source}"
            )

        staging = _staging_path(name)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()

        vsi_urls = [f"/vsicurl/{u}" for u in urls]

        w, s_lat, e, n = bbox
        cmd = [
            gdalwarp_bin,
            "-overwrite",
            "-of", "GTiff",
            "-co", "COMPRESS=LZW",
            "-co", "TILED=YES",
            "-t_srs", "EPSG:4326",
            "-te", f"{w}", f"{s_lat}", f"{e}", f"{n}",
            *vsi_urls,
            str(staging),
        ]
        start = time.monotonic()
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            if staging.exists():
                staging.unlink()
            stderr = (exc.stderr or "").strip()
            raise ElevationError(f"gdalwarp failed: {stderr or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            if staging.exists():
                staging.unlink()
            raise ElevationError(
                f"gdalwarp timed out after {timeout_seconds}s"
            ) from exc
        except FileNotFoundError as exc:
            raise ElevationError(
                f"{gdalwarp_bin!r} not found. Install GDAL "
                "('brew install gdal' or tools/install-tools.sh)."
            ) from exc
        except BaseException:
            if staging.exists():
                staging.unlink()
            raise
        elapsed = time.monotonic() - start

        size, sha = _sha256_file(staging)

        s.finalize_from_local(str(staging), str(out_abs))

        generated_at = sidecar.utcnow_iso()
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            rel,
            kind="file",
            size_bytes=size,
            sha256=sha,
            tool={
                "command": "gdalwarp",
                "gdal_version": _gdalwarp_version(gdalwarp_bin),
            },
            extra={
                "name": name,
                "source": source,
                "bbox": list(bbox),
                "tile_urls": urls,
                "tile_count": len(urls),
                "elevation_version": ELEVATION_VERSION,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return DownloadResult(
            name=name,
            path=str(out_abs),
            relative_path=rel,
            source=source,
            bbox=bbox,
            tile_urls=urls,
            size_bytes=size,
            sha256=sha,
            elevation_version=ELEVATION_VERSION,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            sidecar=side,
        )


def list_rasters(storage: Any = None) -> list[dict[str, Any]]:
    s = storage or LocalStorage()
    out = sidecar.list_entries(NAMESPACE, CACHE_TYPE, s)
    out.sort(key=lambda e: (e.get("extra") or {}).get("name", ""))
    return out
