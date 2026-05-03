"""Shared Geofabrik PBF download library.

Single source of truth for downloading Geofabrik PBF files into the OSM
cache. Both the ``download-pbf`` CLI tool and the Facetwork FFL handlers
(``osm.ops.CacheRegion``) call ``download_region`` here, so they share
the same on-disk layout, the same sidecar metadata, and the same upstream
MD5 verification.

Layout (per ``agent-spec/cache-layout.agent-spec.yaml``)::

    cache/osm/pbf/<region>-latest.osm.pbf
    cache/osm/pbf/<region>-latest.osm.pbf.meta.json

where ``<region>`` mirrors Geofabrik's path (``north-america/us/california``,
``europe/germany/berlin``, etc.).

Thread-safety: a per-region ``threading.Lock`` ensures that if the handler
runtime invokes ``CacheRegion`` concurrently for the same region, only one
download actually happens. No global manifest lock is needed — sidecars
are per-entry.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover - optional, only needed for bulk streaming
    requests = None

from _lib import sidecar
from _lib.storage import LocalStorage, Storage, local_staging_subdir

NAMESPACE = "osm"
CACHE_TYPE = "pbf"
GEOFABRIK_BASE = "https://download.geofabrik.de"
USER_AGENT = "facetwork-osm-geocoder/1.0 (OSM PBF downloader)"

ProgressCallback = Callable[[str, int, int, bool], None]
"""Progress callback: (label, bytes_so_far, total_bytes, is_final)."""


@dataclass
class DownloadResult:
    """Outcome of a ``download_region`` call."""

    region: str
    path: str                   # absolute filesystem/HDFS path to the cached file
    relative_path: str          # path relative to the pbf/ cache dir
    source_url: str
    size_bytes: int
    sha256: str
    md5: str
    source_timestamp: str | None
    downloaded_at: str
    was_cached: bool            # True if the download was skipped (sidecar was up-to-date)
    sidecar: dict[str, Any] = field(default_factory=dict)


class DownloadError(RuntimeError):
    """Raised when a download fails (network, MD5 mismatch, etc.)."""


_region_locks: dict[str, threading.Lock] = {}
_region_locks_guard = threading.Lock()


def _region_lock(region: str) -> threading.Lock:
    with _region_locks_guard:
        lock = _region_locks.get(region)
        if lock is None:
            lock = threading.Lock()
            _region_locks[region] = lock
        return lock


def staging_path(region: str) -> str:
    """Path on local disk where a region is staged before finalization.

    Public so callers (e.g. the CLI progress display) can report it.
    """
    safe = region.replace("/", "_")
    dir_ = local_staging_subdir("facetwork-pbf-staging")
    return os.path.join(dir_, f"{safe}-latest.osm.pbf.tmp")


def region_to_paths(region: str) -> tuple[str, str]:
    """Return ``(relative_path, remote_url)`` for a Geofabrik region key."""
    region = region.strip().strip("/")
    if not region:
        raise ValueError("Empty region")
    rel = f"{region}-latest.osm.pbf"
    url = f"{GEOFABRIK_BASE}/{rel}"
    return rel, url


def relative_path_to_region(rel: str) -> str | None:
    """Inverse of ``region_to_paths``: extract the Geofabrik region key."""
    suffix = "-latest.osm.pbf"
    if not rel.endswith(suffix):
        return None
    return rel[: -len(suffix)]


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    req.get_method = lambda m=method: m
    return req


def fetch_md5(url: str) -> str:
    """Fetch Geofabrik's ``.md5`` file for ``url``; return the hex digest."""
    md5_url = url + ".md5"
    with urllib.request.urlopen(_request(md5_url), timeout=30) as resp:
        body = resp.read().decode("utf-8").strip()
    parts = body.split()
    if not parts or len(parts[0]) != 32:
        raise DownloadError(f"Unexpected .md5 body from {md5_url}: {body!r}")
    return parts[0].lower()


def head_last_modified(url: str) -> str | None:
    """Best-effort HEAD for upstream ``Last-Modified``; ISO-8601 UTC or ``None``."""
    try:
        with urllib.request.urlopen(_request(url, "HEAD"), timeout=30) as resp:
            lm = resp.headers.get("Last-Modified")
    except urllib.error.URLError:
        return None
    if not lm:
        return None
    try:
        dt = parsedate_to_datetime(lm)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _already_cached(
    rel_path: str,
    expected_md5: str,
    storage: Storage,
) -> dict[str, Any] | None:
    """Return the sidecar dict iff it matches ``expected_md5`` and artifact is intact."""
    side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel_path, storage)
    if not side:
        return None
    got_md5 = side.get("source", {}).get("source_checksum", {}).get("value")
    if got_md5 != expected_md5:
        return None
    art = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel_path, storage)
    if not storage.exists(art):
        return None
    if storage.size(art) != side.get("size_bytes"):
        return None
    return side


STREAM_CHUNK_SIZE = 64 * 1024
READ_TIMEOUT_SECONDS = 120
CONNECT_TIMEOUT_SECONDS = 30


def _stream_download(
    url: str,
    writer,
    label: str,
    on_progress: ProgressCallback | None,
) -> tuple[int, str, str]:
    """Stream ``url`` into ``writer``; compute SHA-256 and MD5."""
    if requests is None:
        return _stream_download_urllib(url, writer, label, on_progress)

    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    start = time.monotonic()
    last_report = start

    with requests.get(
        url,
        stream=True,
        headers={"User-Agent": USER_AGENT},
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_SIZE):
            if not chunk:
                continue
            writer.write(chunk)
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
            now = time.monotonic()
            if on_progress and now - last_report >= 2.0:
                on_progress(label, size, total, False)
                last_report = now
    if on_progress:
        on_progress(label, size, total or size, True)
    return size, sha.hexdigest(), md5.hexdigest().lower()


def _stream_download_urllib(
    url: str,
    writer,
    label: str,
    on_progress: ProgressCallback | None,
) -> tuple[int, str, str]:
    """Fallback streaming path using ``urllib.request``."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    last_report = time.monotonic()
    with urllib.request.urlopen(_request(url), timeout=READ_TIMEOUT_SECONDS) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read1(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
            now = time.monotonic()
            if on_progress and now - last_report >= 2.0:
                on_progress(label, size, total, False)
                last_report = now
    if on_progress:
        on_progress(label, size, total or size, True)
    return size, sha.hexdigest(), md5.hexdigest().lower()


def is_region_cached(
    region: str, *, storage: Storage | None = None
) -> bool:
    """Quick check whether a region is cached and its file still exists.

    Does NOT contact Geofabrik. For freshness checks use ``download_region``
    and inspect ``was_cached``.
    """
    storage = storage or LocalStorage()
    rel_path, _ = region_to_paths(region)
    return sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, rel_path, storage)


def sidecar_entry_for(
    region: str, *, storage: Storage | None = None
) -> dict[str, Any] | None:
    """Return the sidecar dict for ``region`` if present, else ``None``."""
    storage = storage or LocalStorage()
    rel_path, _ = region_to_paths(region)
    return sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel_path, storage)


# Back-compat alias; older handler code may still call manifest_entry_for.
manifest_entry_for = sidecar_entry_for


def cached_path(
    region: str, *, storage: Storage | None = None
) -> str:
    """Return the absolute cache path for ``region`` (whether or not it exists)."""
    storage = storage or LocalStorage()
    rel_path, _ = region_to_paths(region)
    return sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel_path, storage)


def download_region(
    region: str,
    *,
    storage: Storage | None = None,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    """Download a Geofabrik PBF for ``region`` into the OSM cache.

    Thread-safe: concurrent calls for the same region serialize on a
    per-region lock so only one download happens.
    """
    storage = storage or LocalStorage()
    rel_path, url = region_to_paths(region)
    cache_file = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel_path, storage)

    with _region_lock(region):
        storage.mkdir_p(Storage.dirname(cache_file))

        expected_md5 = fetch_md5(url)
        source_ts = head_last_modified(url)

        if not force:
            side = _already_cached(rel_path, expected_md5, storage)
            if side is not None:
                source = side.get("source", {})
                return DownloadResult(
                    region=region,
                    path=cache_file,
                    relative_path=rel_path,
                    source_url=source.get("url", url),
                    size_bytes=side.get("size_bytes", storage.size(cache_file)),
                    sha256=side.get("sha256", ""),
                    md5=source.get("source_checksum", {}).get("value", expected_md5),
                    source_timestamp=source.get("source_timestamp"),
                    downloaded_at=source.get("downloaded_at", ""),
                    was_cached=True,
                    sidecar=side,
                )

        # Stage onto local disk first so socket-read rate is decoupled from
        # destination write rate (matters when dst is a slow network volume).
        staged = staging_path(region)
        if os.path.exists(staged):
            os.unlink(staged)

        try:
            with open(staged, "wb") as writer:
                size, sha256_hex, md5_hex = _stream_download(
                    url, writer, region, on_progress
                )
        except BaseException:
            if os.path.exists(staged):
                os.unlink(staged)
            raise

        if md5_hex != expected_md5:
            os.unlink(staged)
            raise DownloadError(
                f"MD5 mismatch for {region}: upstream={expected_md5} computed={md5_hex}"
            )

        storage.finalize_from_local(staged, cache_file)

        downloaded_at = sidecar.utcnow_iso()
        source = {
            "url": url,
            "source_checksum": {
                "algo": "md5",
                "value": md5_hex,
                "url": url + ".md5",
            },
            "source_timestamp": source_ts,
            "downloaded_at": downloaded_at,
        }
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            rel_path,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source=source,
            extra={"region": region},
            generated_at=downloaded_at,
            storage=storage,
        )

        return DownloadResult(
            region=region,
            path=cache_file,
            relative_path=rel_path,
            source_url=url,
            size_bytes=size,
            sha256=sha256_hex,
            md5=md5_hex,
            source_timestamp=source_ts,
            downloaded_at=downloaded_at,
            was_cached=False,
            sidecar=side,
        )


def regions_from_pbf_cache(
    under: str | None = None,
    *,
    storage: Storage | None = None,
    use_index: bool = True,
) -> list[str]:
    """Return all regions currently cached under ``cache/osm/pbf/``.

    Optionally filtered by path prefix (e.g. ``europe/germany`` selects
    Germany and all its sub-regions). Sorted alphabetically.

    When ``use_index`` is True (default), consults the lazy cache index
    at ``_indexes/osm/pbf.index.json`` for a one-file read instead of
    walking the sidecar tree. The index is rebuilt automatically if
    missing or if any sidecar under the cache_type subtree has a newer
    mtime. Pass ``use_index=False`` to force a fresh walk (e.g. for
    diagnostics).
    """
    storage = storage or LocalStorage()
    paths: list[str]
    if use_index:
        # Soft-import cache_index so unit tests that stub storage
        # don't have to wire the index module up.
        from _lib import cache_index

        try:
            idx = cache_index.read_index(NAMESPACE, CACHE_TYPE, storage=storage)
            paths = list((idx.get("entries") or {}).keys())
        except Exception:
            paths = sidecar.list_relative_paths(
                NAMESPACE, CACHE_TYPE, under=None, storage=storage
            )
    else:
        paths = sidecar.list_relative_paths(
            NAMESPACE, CACHE_TYPE, under=None, storage=storage
        )
    regions = [r for r in (relative_path_to_region(p) for p in paths) if r]
    if under:
        u = under.strip().strip("/")
        pref = u + "/"
        regions = [r for r in regions if r == u or r.startswith(pref)]
    regions.sort()
    return regions


# Back-compat alias.
regions_from_pbf_manifest = regions_from_pbf_cache


def filter_leaves(regions: list[str]) -> list[str]:
    """Drop regions that have a descendant in the set."""
    selected_set = set(regions)
    non_leaves: set[str] = set()
    for r in regions:
        parts = r.split("/")
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if ancestor in selected_set:
                non_leaves.add(ancestor)
    return [r for r in regions if r not in non_leaves]


def to_osm_cache(result: DownloadResult) -> dict[str, Any]:
    """Convert a ``DownloadResult`` into the ``OSMCache`` dict shape.

    The FFL ``osm.types.OSMCache`` schema is ``{url, path, date, size,
    wasInCache}``; handlers also include a ``source`` field.
    """
    return {
        "url": result.source_url,
        "path": result.path,
        "date": result.downloaded_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "download",
    }
