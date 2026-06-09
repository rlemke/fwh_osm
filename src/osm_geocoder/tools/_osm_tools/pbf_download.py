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
import logging
import os
import http.client
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - optional, only needed for bulk streaming
    requests = None

from _osm_tools import sidecar
from _osm_tools.storage import LocalStorage, Storage, get_storage, local_staging_subdir

NAMESPACE = "osm"
CACHE_TYPE = "pbf"
GEOFABRIK_BASE = "https://download.geofabrik.de"
USER_AGENT = "facetwork-osm-geocoder/1.0 (OSM PBF downloader)"

logger = logging.getLogger(__name__)

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


def _use_cache_if_present() -> bool:
    """True when ``AFL_OSM_USE_CACHE_IF_PRESENT`` opts into pinned-cache mode."""
    return os.environ.get("AFL_OSM_USE_CACHE_IF_PRESENT", "").lower() in ("1", "true", "yes")


def fetch_md5(url: str) -> str:
    """Fetch Geofabrik's ``.md5`` file for ``url``; return the hex digest."""
    md5_url = url + ".md5"
    with urllib.request.urlopen(_request(md5_url), timeout=30) as resp:
        body = resp.read().decode("utf-8").strip()
    parts = body.split()
    if not parts or len(parts[0]) != 32:
        raise DownloadError(f"Unexpected .md5 body from {md5_url}: {body!r}")
    return parts[0].lower()


def fetch_md5_or_none(url: str) -> str | None:
    """Like :func:`fetch_md5`, but return ``None`` when Geofabrik publishes
    no ``.md5`` for this extract (HTTP 404).

    Some extracts (e.g. combined regions and certain small areas) ship a
    ``.pbf`` with no companion checksum file. A *missing* checksum is not
    evidence of corruption — unlike a *mismatched* one — so callers may
    proceed and anchor integrity on the locally-computed sha256 instead of
    treating the region as permanently un-cacheable.
    """
    try:
        return fetch_md5(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
# Per-read (between-chunks) socket timeout. Continent extracts (europe ~30GB,
# north-america ~15GB) often redirect to slow mirrors (e.g. ftp5.gwdg.de) that
# stall for long stretches; the 120s default trips a read timeout mid-transfer
# and — because there's no HTTP resume — the whole multi-GB download restarts
# from zero, easily exhausting retries. Tunable via AFL_OSM_READ_TIMEOUT_SECONDS;
# default raised to 600s so a slow-but-alive mirror isn't killed.
READ_TIMEOUT_SECONDS = int(os.environ.get("AFL_OSM_READ_TIMEOUT_SECONDS") or 600)
CONNECT_TIMEOUT_SECONDS = 30
# Max times a single download will reconnect-and-resume (HTTP Range) after a
# mid-stream drop before giving up. Geofabrik's mirrors drop connections on
# big extracts (europe ~30GB, north-america ~19GB) — without resume each drop
# restarts from zero and the file never lands. 50 resumes comfortably covers a
# 30GB transfer that drops every couple GB. Tunable via AFL_OSM_RESUME_MAX_ATTEMPTS.
RESUME_MAX_ATTEMPTS = int(os.environ.get("AFL_OSM_RESUME_MAX_ATTEMPTS") or 50)

# Transient transport errors that warrant a Range-resume rather than a hard fail.
_RESUMABLE_ERRORS: tuple[type[BaseException], ...] = (
    http.client.IncompleteRead,
    http.client.HTTPException,
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
    OSError,
)
if requests is not None:  # requests wraps the above in its own exception tree
    _RESUMABLE_ERRORS = _RESUMABLE_ERRORS + (requests.exceptions.RequestException,)


def _expected_total(headers: Any, have: int) -> int:
    """Full expected file size from a (possibly ranged) response, else 0.

    For a 206 the ``Content-Range`` tail (``bytes start-end/TOTAL``) is the full
    size; ``Content-Length`` is only the remaining bytes, so add ``have``. For a
    fresh 200 ``have`` is 0 and Content-Length is the full size.
    """
    cr = headers.get("Content-Range")
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    cl = headers.get("Content-Length")
    if cl is not None and str(cl).isdigit():
        return have + int(cl)
    return 0


def _download_resumable(
    url: str,
    staged_path: str,
    label: str,
    on_progress: ProgressCallback | None,
) -> tuple[int, str, str]:
    """Stream ``url`` to ``staged_path``, resuming via HTTP Range across
    mid-stream connection drops; compute SHA-256 + MD5 over the whole file.

    Geofabrik mirrors routinely drop multi-GB transfers part-way. Without resume
    each drop restarts from zero, so a 30GB extract never lands. Here a single
    open file + running hashes are carried across an internal reconnect loop: on
    a transport error (or a clean short read) we re-request ``bytes=<have>-`` and
    append. Handles a mirror that ignores Range (200 with bytes already on disk →
    truncate + restart) and 416 (range past EOF → already complete).

    Each call starts a fresh file (no partial carried across task retries) so we
    never resume onto a different upstream revision; the loop below absorbs the
    drops within one attempt.
    """
    use_requests = requests is not None
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    have = 0
    total = 0
    last_report = time.monotonic()
    attempt = 0
    writer = open(staged_path, "wb")
    try:
        while True:
            headers = {"User-Agent": USER_AGENT}
            if have > 0:
                headers["Range"] = f"bytes={have}-"
            try:
                if use_requests:
                    with requests.get(
                        url,
                        stream=True,
                        headers=headers,
                        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    ) as resp:
                        if resp.status_code == 416:
                            break
                        if have > 0 and resp.status_code == 200:
                            # Mirror ignored Range — body is the whole file. Reset.
                            writer.seek(0)
                            writer.truncate()
                            sha, md5, have = hashlib.sha256(), hashlib.md5(), 0
                        resp.raise_for_status()
                        total = _expected_total(resp.headers, have)
                        for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                            if not chunk:
                                continue
                            writer.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            have += len(chunk)
                            now = time.monotonic()
                            if on_progress and now - last_report >= 2.0:
                                on_progress(label, have, total or have, False)
                                last_report = now
                else:
                    with urllib.request.urlopen(
                        urllib.request.Request(url, headers=headers),
                        timeout=READ_TIMEOUT_SECONDS,
                    ) as resp:
                        if have > 0 and getattr(resp, "status", 200) == 200:
                            writer.seek(0)
                            writer.truncate()
                            sha, md5, have = hashlib.sha256(), hashlib.md5(), 0
                        total = _expected_total(resp.headers, have)
                        while True:
                            chunk = resp.read1(STREAM_CHUNK_SIZE)
                            if not chunk:
                                break
                            writer.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            have += len(chunk)
                            now = time.monotonic()
                            if on_progress and now - last_report >= 2.0:
                                on_progress(label, have, total or have, False)
                                last_report = now
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    break
                raise
            except _RESUMABLE_ERRORS as exc:
                attempt += 1
                writer.flush()
                try:
                    os.fsync(writer.fileno())
                except OSError:
                    pass
                if attempt > RESUME_MAX_ATTEMPTS:
                    raise DownloadError(
                        f"{label}: gave up after {attempt} resume attempts at "
                        f"{have} bytes: {exc}"
                    ) from exc
                logger.warning(
                    "Resuming %s from %d bytes after transport error "
                    "(attempt %d/%d): %s",
                    label, have, attempt, RESUME_MAX_ATTEMPTS, exc,
                )
                time.sleep(min(30, 2 ** min(attempt, 5)))
                continue
            # Stream ended without raising. If the server closed early (clean
            # short read), resume the remainder; otherwise we're done.
            if total and have < total:
                attempt += 1
                if attempt > RESUME_MAX_ATTEMPTS:
                    raise DownloadError(
                        f"{label}: short read {have}/{total} after {attempt} attempts"
                    )
                logger.warning(
                    "Resuming %s from %d/%d bytes (clean short read, attempt %d/%d)",
                    label, have, total, attempt, RESUME_MAX_ATTEMPTS,
                )
                continue
            break
        if on_progress:
            on_progress(label, have, total or have, True)
    finally:
        writer.close()
    return have, sha.hexdigest(), md5.hexdigest().lower()


def list_cached_regions(*, storage: Storage | None = None) -> list[str]:
    """Return the Geofabrik region keys currently present in the PBF cache.

    Walks the ``osm/pbf`` cache sidecars and reverse-maps each to its region
    key (``north-america/us/california``, ...), sorted. Does NOT contact
    Geofabrik. Use to drive a deliberate cache-refresh over exactly what's
    cached.
    """
    storage = storage or get_storage()
    out: list[str] = []
    for entry in sidecar.list_entries(NAMESPACE, CACHE_TYPE, storage):
        reg = relative_path_to_region(entry.get("relative_path", ""))
        if reg:
            out.append(reg)
    return sorted(set(out))


def is_region_cached(
    region: str, *, storage: Storage | None = None
) -> bool:
    """Quick check whether a region is cached and its file still exists.

    Does NOT contact Geofabrik. For freshness checks use ``download_region``
    and inspect ``was_cached``.
    """
    storage = storage or get_storage()
    rel_path, _ = region_to_paths(region)
    return sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, rel_path, storage)


def sidecar_entry_for(
    region: str, *, storage: Storage | None = None
) -> dict[str, Any] | None:
    """Return the sidecar dict for ``region`` if present, else ``None``."""
    storage = storage or get_storage()
    rel_path, _ = region_to_paths(region)
    return sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel_path, storage)


# Back-compat alias; older handler code may still call manifest_entry_for.
manifest_entry_for = sidecar_entry_for


def cached_path(
    region: str, *, storage: Storage | None = None
) -> str:
    """Return the absolute cache path for ``region`` (whether or not it exists)."""
    storage = storage or get_storage()
    rel_path, _ = region_to_paths(region)
    return sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel_path, storage)


def download_region(
    region: str,
    *,
    storage: Storage | None = None,
    force: bool = False,
    use_cache_if_present: bool | None = None,
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    """Download a Geofabrik PBF for ``region`` into the OSM cache.

    Thread-safe: concurrent calls for the same region serialize on a
    per-region lock so only one download happens.

    ``use_cache_if_present`` controls pinned-cache mode per call:
    ``None`` (default) defers to the ``AFL_OSM_USE_CACHE_IF_PRESENT`` env,
    ``True`` forces it on, ``False`` forces normal revalidation. ``force=True``
    always re-downloads and overrides both.
    """
    storage = storage or get_storage()
    rel_path, url = region_to_paths(region)
    cache_file = sidecar.cache_path(NAMESPACE, CACHE_TYPE, rel_path, storage)

    # Offline / pinned-cache mode: if the region is already cached, use it AS-IS
    # without contacting Geofabrik. Geofabrik rebuilds every PBF daily with a new
    # md5, so the normal freshness check treats an older-but-valid cache as stale
    # and re-downloads gigabytes — undesirable when an existing extract is fine
    # (reproducing a map, working offline, or when upstream is slow/flaky). Opt-in
    # per call (use_cache_if_present) or globally (AFL_OSM_USE_CACHE_IF_PRESENT);
    # `force=True` still bypasses it.
    prefer_cache = _use_cache_if_present() if use_cache_if_present is None else use_cache_if_present
    if not force and prefer_cache and is_region_cached(region, storage=storage):
        side = sidecar_entry_for(region, storage=storage) or {}
        source = side.get("source", {})
        return DownloadResult(
            region=region,
            path=cache_file,
            relative_path=rel_path,
            source_url=source.get("url", url),
            size_bytes=side.get("size_bytes", storage.size(cache_file)),
            sha256=side.get("sha256", ""),
            md5=source.get("source_checksum", {}).get("value", ""),
            source_timestamp=source.get("source_timestamp"),
            downloaded_at=source.get("downloaded_at", ""),
            was_cached=True,
            sidecar=side,
        )

    with _region_lock(region):
        storage.mkdir_p(Storage.dirname(cache_file))

        expected_md5 = fetch_md5_or_none(url)
        had_upstream_md5 = expected_md5 is not None
        source_ts = head_last_modified(url)

        if not force and had_upstream_md5:
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
        # The download resumes via HTTP Range across mid-stream drops rather
        # than restarting from zero — essential for multi-GB extracts on
        # Geofabrik's flaky mirrors.
        staged = staging_path(region)
        if os.path.exists(staged):
            os.unlink(staged)

        try:
            size, sha256_hex, md5_hex = _download_resumable(
                url, staged, region, on_progress
            )
        except BaseException:
            if os.path.exists(staged):
                os.unlink(staged)
            raise

        if not had_upstream_md5:
            # Geofabrik publishes no .md5 for this extract (404). A missing
            # checksum is not corruption evidence — accept the COMPLETE
            # download, anchored by the locally-computed sha256.
            logger.warning(
                "No upstream .md5 for %s (Geofabrik publishes none); accepting "
                "%d-byte download verified by sha256=%s.", region, size, sha256_hex
            )
            expected_md5 = md5_hex
        elif md5_hex != expected_md5:
            # Geofabrik's .md5 and .pbf are briefly inconsistent during its daily
            # rebuild; for multi-GB files we fetch the .md5 before the long
            # download finishes. Re-fetch the current .md5 (the one we read may be
            # stale) and re-compare before treating it as corruption.
            fresh = fetch_md5(url)
            if md5_hex == fresh:
                expected_md5 = fresh
            elif os.environ.get("AFL_OSM_TOLERATE_MD5", "").lower() in ("1", "true", "yes"):
                # Still mismatched, but accept the COMPLETE download with a warning
                # (a stale upstream md5 during the rebuild window, not a corrupt
                # file). Opt-in via AFL_OSM_TOLERATE_MD5.
                logger.warning(
                    "MD5 mismatch for %s (upstream=%s computed=%s); accepting %d-byte "
                    "download (AFL_OSM_TOLERATE_MD5).", region, fresh, md5_hex, size
                )
                expected_md5 = md5_hex
            else:
                os.unlink(staged)
                raise DownloadError(
                    f"MD5 mismatch for {region}: upstream={fresh} computed={md5_hex}"
                )

        storage.finalize_from_local(staged, cache_file)

        downloaded_at = sidecar.utcnow_iso()
        source = {
            "url": url,
            "source_checksum": {
                "algo": "md5",
                "value": md5_hex,
                "url": (url + ".md5") if had_upstream_md5 else None,
                "computed": not had_upstream_md5,
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
    storage = storage or get_storage()
    paths: list[str]
    if use_index:
        # Soft-import cache_index so unit tests that stub storage
        # don't have to wire the index module up.
        from _osm_tools import cache_index

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


def to_osm_cache(
    result: DownloadResult, region: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convert a ``DownloadResult`` into the ``OSMCache`` dict shape.

    The FFL ``osm.types.OSMCache`` schema is ``{region, url, path, date,
    size, wasInCache}``; handlers also include a ``source`` field.

    Args:
        result: The download outcome.
        region: A ``osm.types.Region`` dict identifying the extract. New
            code should always pass this — the typed Region carries
            ``name`` / ``level`` / ``continent`` / etc. that downstream
            handlers consume for logging and grouping. When ``None``,
            a minimal placeholder Region is constructed from the
            ``DownloadResult.region`` path string (``canonical`` and
            ``geofabrik_path`` are populated; other fields are empty).
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
        "date": result.downloaded_at,
        "size": result.size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "download",
    }
