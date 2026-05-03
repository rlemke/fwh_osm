"""Standalone HTTP downloader with filesystem caching for OSM data.

Downloads .osm.pbf files from Geofabrik and caches them locally.
Also provides a generic download_url() for fetching any URL to any path
(local or HDFS).  No Facetwork dependencies — can be used independently.

Concurrent access is safe: per-path locks prevent duplicate downloads when
multiple threads request the same file, and atomic temp-file renames ensure
the cache file is always either absent or complete (never partial).
"""

import logging
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime

import requests

from facetwork.config import get_output_base
from facetwork.runtime.storage import get_storage_backend

log = logging.getLogger(__name__)

CACHE_DIR = os.environ.get("AFL_CACHE_DIR", os.path.join(get_output_base(), "cache", "osm"))
_storage = get_storage_backend(CACHE_DIR)
GEOFABRIK_BASE = "https://download.geofabrik.de"
GEOFABRIK_MIRROR = os.environ.get("AFL_GEOFABRIK_MIRROR", "/Volumes/afl_data/osm")
USER_AGENT = "Facetwork-OSM-Example/1.0"

FORMAT_EXTENSIONS = {
    "pbf": "osm.pbf",
    "shp": "free.shp.zip",
}

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()

_LOG_INTERVAL = 100 * 1024 * 1024  # log every 100 MB

# PBF files start with a 4-byte big-endian blob header size, then a protobuf
# field containing the string "OSMHeader".  We read the first 32 bytes and
# check for this marker to reject corrupt/HTML downloads.
_PBF_MARKER = b"OSMHeader"
_PBF_CHECK_BYTES = 32


def verify_pbf(path: str, storage=None) -> None:
    """Verify that a file is a valid OSM PBF by checking the header.

    Args:
        path: Path to the file to verify.
        storage: Optional storage backend. When None, uses os.path / open()
            directly (local filesystem).

    Raises:
        ValueError: If the file is missing, too small, or not a valid PBF.
    """
    try:
        size = storage.getsize(path) if storage else os.path.getsize(path)
    except OSError as e:
        raise ValueError(f"Cannot read file: {e}") from e

    if size < _PBF_CHECK_BYTES:
        raise ValueError(
            f"File too small to be a valid PBF ({size} bytes): {path}"
        )

    try:
        if storage:
            with storage.open(path, "rb") as f:
                header = f.read(_PBF_CHECK_BYTES)
        else:
            with open(path, "rb") as f:
                header = f.read(_PBF_CHECK_BYTES)
    except OSError as e:
        raise ValueError(f"Cannot read file: {e}") from e

    if _PBF_MARKER not in header:
        # Try to detect what the file actually is
        if header.lstrip().startswith(b"<"):
            detail = "file contains HTML/XML (likely an error page)"
        else:
            detail = f"missing OSMHeader marker in first {_PBF_CHECK_BYTES} bytes"
        raise ValueError(f"Not a valid PBF file ({detail}): {path}")


def _fmt_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _get_path_lock(path: str) -> threading.Lock:
    """Return a per-path lock, creating one if needed."""
    if path not in _path_locks:
        with _path_locks_guard:
            if path not in _path_locks:
                _path_locks[path] = threading.Lock()
    return _path_locks[path]


def _mirror_file_exists(path: str) -> bool:
    """Check if a mirror file exists, with SMB mount fallback.

    On macOS Docker + SMB mounts, ``os.path.isfile()`` can return False even
    when the file is present (``stat()`` fails but ``readdir()`` works).
    Falls back to checking the parent directory listing.
    """
    if os.path.isfile(path):
        return True
    # Fallback: check via directory listing (readdir works on SMB)
    parent = os.path.dirname(path)
    basename = os.path.basename(path)
    try:
        return basename in os.listdir(parent)
    except OSError:
        return False


def _mirror_file_size(path: str) -> int:
    """Get mirror file size, returning 0 if stat() fails (SMB mounts)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _cache_hit(url: str, path: str, storage=None) -> dict:
    """Build result dict for a cache hit."""
    if storage is None:
        storage = _storage
    return {
        "url": url,
        "path": path,
        "date": datetime.now(UTC).isoformat(),
        "size": storage.getsize(path),
        "wasInCache": True,
    }


def _cache_miss(url: str, path: str, storage=None) -> dict:
    """Build result dict for a cache miss (freshly downloaded)."""
    if storage is None:
        storage = _storage
    return {
        "url": url,
        "path": path,
        "date": datetime.now(UTC).isoformat(),
        "size": storage.getsize(path),
        "wasInCache": False,
    }


def _stream_to_file(url: str, path: str, storage) -> None:
    """Download URL content and stream it to path via storage backend."""
    log.info("download: starting %s -> %s", url, path)
    response = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=300)
    response.raise_for_status()
    try:
        total = int(response.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        total = 0
    written = 0
    next_log = _LOG_INTERVAL
    with storage.open(path, "wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            f.write(chunk)
            written += len(chunk)
            if written >= next_log:
                log.info(
                    "download: %s — %s / %s",
                    os.path.basename(path),
                    _fmt_bytes(written),
                    _fmt_bytes(total) if total else "?",
                )
                next_log += _LOG_INTERVAL
    log.info("download: finished %s (%s)", os.path.basename(path), _fmt_bytes(written))


def _copy_to_cache(src_path: str, dst_path: str) -> None:
    """Copy a local file to the configured cache location (local or HDFS).

    Uses ``subprocess cp`` instead of ``shutil.copy2`` / ``open()`` because
    Python's ``open()`` can hang indefinitely on VirtioFS mounts in Docker
    containers when reading large files (e.g. 1.3 GB PBF).
    """
    fname = os.path.basename(src_path)
    _storage.makedirs(_storage.dirname(dst_path), exist_ok=True)
    is_local = not CACHE_DIR.startswith("hdfs://")
    if is_local:
        tmp_path = dst_path + f".tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            log.info("cache-copy: start %s -> %s (via cp)", fname, dst_path)
            subprocess.run(["cp", src_path, tmp_path], check=True)
            os.replace(tmp_path, dst_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    else:
        # HDFS: cp source to a local temp file first (avoids VirtioFS hang
        # on open()), then stream the local copy to HDFS.
        with tempfile.NamedTemporaryFile(suffix=f".{fname}", delete=False) as tf:
            local_tmp = tf.name
        try:
            log.info("cache-copy: start %s -> local tmp (via cp)", fname)
            subprocess.run(["cp", src_path, local_tmp], check=True)
            src_size = os.path.getsize(local_tmp)
            log.info("cache-copy: %s (%s) -> HDFS %s", fname, _fmt_bytes(src_size), dst_path)
            copied = 0
            next_log = _LOG_INTERVAL
            with open(local_tmp, "rb") as src:
                with _storage.open(dst_path, "wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        dst.write(chunk)
                        copied += len(chunk)
                        if copied >= next_log:
                            log.info(
                                "cache-copy: %s — %s / %s buffered",
                                fname,
                                _fmt_bytes(copied),
                                _fmt_bytes(src_size),
                            )
                            next_log += _LOG_INTERVAL
                    log.info("cache-copy: %s — uploading %s to HDFS...", fname, _fmt_bytes(copied))
                # dst.close() happens here — the actual HDFS upload
            log.info("cache-copy: %s — upload complete", fname)
        finally:
            try:
                os.unlink(local_tmp)
            except OSError:
                pass
    log.info("cache-copy: finished %s", fname)


def geofabrik_url(region_path: str, fmt: str = "pbf") -> str:
    """Build the full Geofabrik download URL for a region path."""
    ext = FORMAT_EXTENSIONS[fmt]
    return f"{GEOFABRIK_BASE}/{region_path}-latest.{ext}"


def cache_path(region_path: str, fmt: str = "pbf") -> str:
    """Build the cache file path for a region path."""
    ext = FORMAT_EXTENSIONS[fmt]
    return _storage.join(CACHE_DIR, f"{region_path}-latest.{ext}")


def _cache_is_hdfs() -> bool:
    """Return True when the cache directory is an HDFS URI."""
    return CACHE_DIR.startswith("hdfs://")


def download(region_path: str, fmt: str = "pbf") -> dict:
    """Download an OSM region file, using the local cache if available.

    When ``AFL_GEOFABRIK_MIRROR`` is set and the cache is local (not HDFS),
    the mirror path is returned directly — no copy needed.  When the cache
    is on HDFS, mirror files are still copied so that distributed agents
    can access the data.

    Args:
        region_path: Geofabrik region path (e.g. "africa/algeria").
        fmt: Download format — "pbf" (default) or "shp" for shapefiles.

    Returns:
        OSMCache dict with url, path, date, size, and wasInCache fields.

    Raises:
        requests.HTTPError: If the download fails.
    """
    url = geofabrik_url(region_path, fmt)
    local_path = cache_path(region_path, fmt)

    # Fast path — already cached, no lock needed
    if _storage.exists(local_path):
        if fmt == "pbf":
            try:
                verify_pbf(local_path, _storage)
            except ValueError as e:
                log.warning("cache-corrupt: %s — %s, removing", region_path, e)
                try:
                    _storage.remove(local_path)
                except OSError:
                    pass
                # Fall through to re-download
            else:
                result = _cache_hit(url, local_path)
                result["source"] = "cache"
                log.info("cache-hit: %s (%s)", region_path, _fmt_bytes(result["size"]))
                return result
        else:
            result = _cache_hit(url, local_path)
            result["source"] = "cache"
            log.info("cache-hit: %s (%s)", region_path, _fmt_bytes(result["size"]))
            return result

    # Check local mirror
    if GEOFABRIK_MIRROR:
        ext = FORMAT_EXTENSIONS[fmt]
        mirror_path = os.path.join(GEOFABRIK_MIRROR, f"{region_path}-latest.{ext}")
        if _mirror_file_exists(mirror_path):
            if _cache_is_hdfs():
                # HDFS cache: copy so distributed agents can access it
                with _get_path_lock(local_path):
                    if _storage.exists(local_path):
                        result = _cache_hit(url, local_path)
                        result["source"] = "cache"
                        log.info(
                            "cache-hit: %s (after lock, %s)",
                            region_path,
                            _fmt_bytes(result["size"]),
                        )
                        return result
                    _copy_to_cache(mirror_path, local_path)
                result = _cache_miss(url, local_path)
                result["source"] = "mirror"
                log.info(
                    "cache-seeded: %s from mirror (%s)",
                    region_path,
                    _fmt_bytes(result["size"]),
                )
                return result
            else:
                # Local cache: use the mirror path directly, no copy
                if fmt == "pbf":
                    try:
                        verify_pbf(mirror_path)
                    except ValueError as e:
                        log.warning("mirror-corrupt: %s — %s, skipping", region_path, e)
                        # Fall through to download from Geofabrik
                    else:
                        mirror_size = _mirror_file_size(mirror_path)
                        log.info("mirror-direct: %s (%s)", region_path, _fmt_bytes(mirror_size))
                        return {
                            "url": url,
                            "path": mirror_path,
                            "date": datetime.now(UTC).isoformat(),
                            "size": mirror_size,
                            "wasInCache": True,
                            "source": "mirror",
                        }
                else:
                    mirror_size = _mirror_file_size(mirror_path)
                    log.info("mirror-direct: %s (%s)", region_path, _fmt_bytes(mirror_size))
                    return {
                        "url": url,
                        "path": mirror_path,
                        "date": datetime.now(UTC).isoformat(),
                        "size": mirror_size,
                        "wasInCache": True,
                        "source": "mirror",
                    }

    with _get_path_lock(local_path):
        # Re-check after acquiring lock
        if _storage.exists(local_path):
            result = _cache_hit(url, local_path)
            result["source"] = "cache"
            log.info("cache-hit: %s (after lock, %s)", region_path, _fmt_bytes(result["size"]))
            return result

        # Download to temp file, then atomic rename
        log.info("cache-miss: %s — downloading from %s", region_path, url)
        _storage.makedirs(_storage.dirname(local_path), exist_ok=True)
        tmp_path = local_path + f".tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            _stream_to_file(url, tmp_path, _storage)
            if fmt == "pbf":
                verify_pbf(tmp_path)
            os.replace(tmp_path, local_path)
        except BaseException:
            try:
                _storage.remove(tmp_path)
            except OSError:
                pass
            raise

    result = _cache_miss(url, local_path)
    result["source"] = "download"
    log.info("cache-downloaded: %s (%s)", region_path, _fmt_bytes(result["size"]))
    return result


def download_url(url: str, path: str, force: bool = False) -> dict:
    """Download any URL to a local or HDFS file path.

    Args:
        url: The URL to download from.
        path: Destination file path (local filesystem or ``hdfs://`` URI).
        force: If True, re-download even when the file already exists.

    Returns:
        OSMCache-compatible dict with url, path, date, size, and wasInCache fields.

    Raises:
        requests.HTTPError: If the download fails.
    """
    storage = get_storage_backend(path)
    is_local = not path.startswith("hdfs://")

    # Fast path — already cached, no lock needed
    if not force and storage.exists(path):
        return _cache_hit(url, path, storage)

    with _get_path_lock(path):
        # Re-check after acquiring lock
        if not force and storage.exists(path):
            return _cache_hit(url, path, storage)

        storage.makedirs(storage.dirname(path), exist_ok=True)

        if is_local:
            tmp_path = path + f".tmp.{os.getpid()}.{threading.get_ident()}"
            try:
                _stream_to_file(url, tmp_path, storage)
                os.replace(tmp_path, path)
            except BaseException:
                try:
                    storage.remove(tmp_path)
                except OSError:
                    pass
                raise
        else:
            _stream_to_file(url, path, storage)

    return _cache_miss(url, path, storage)
