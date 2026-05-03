"""GTFS transit-feed download library.

Downloads per-agency GTFS .zip files into
``cache/gtfs/feeds/<agency>-latest.zip`` with sibling ``.meta.json``
sidecars.

Namespace: ``gtfs``. Cache_type: ``feeds``.

Cache validity:
- HEAD the remote URL; compare ``Last-Modified`` / ``ETag`` against the
  sidecar record.
- If HEAD isn't available, fall back to downloading and comparing SHA-256.

Sidecar's extras also record parsed ``feed_info.txt`` fields (publisher,
feed_version, feed_start_date, feed_end_date) so callers can filter
feeds by validity period without unzipping.
"""

from __future__ import annotations

import csv
import hashlib
import io
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from _lib import sidecar
from _lib.storage import LocalStorage

NAMESPACE = "gtfs"
CACHE_TYPE = "feeds"
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "facetwork-osm-geocoder/1.0 (GTFS downloader)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

_agency_locks: dict[str, threading.Lock] = {}
_agency_locks_guard = threading.Lock()


def _agency_lock(agency: str) -> threading.Lock:
    with _agency_locks_guard:
        lock = _agency_locks.get(agency)
        if lock is None:
            lock = threading.Lock()
            _agency_locks[agency] = lock
        return lock


@dataclass
class DownloadResult:
    agency: str
    path: str
    relative_path: str
    source_url: str
    size_bytes: int
    sha256: str
    last_modified: str | None
    etag: str | None
    feed_info: dict[str, Any]
    downloaded_at: str
    duration_seconds: float
    was_cached: bool
    sidecar: dict[str, Any] = field(default_factory=dict)


class DownloadError(RuntimeError):
    pass


def feed_rel_path(agency: str) -> str:
    return f"{agency}-latest.zip"


def feed_abs_path(agency: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, CACHE_TYPE, feed_rel_path(agency), s))


def _staging_path(agency: str) -> Path:
    base = tempfile.gettempdir()
    safe = agency.replace("/", "_")
    return Path(base) / "facetwork-gtfs-staging" / f"{safe}-latest.zip"


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


def _http_head(url: str) -> tuple[str | None, str | None]:
    if requests is not None:
        try:
            r = requests.head(
                url,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
                timeout=(CONNECT_TIMEOUT, CONNECT_TIMEOUT),
            )
            if r.ok:
                return r.headers.get("Last-Modified"), r.headers.get("ETag")
        except requests.RequestException:
            pass
        return None, None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    req.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
            return resp.headers.get("Last-Modified"), resp.headers.get("ETag")
    except urllib.error.URLError:
        return None, None


def _http_download(url: str, dest: Path) -> tuple[str | None, str | None]:
    if requests is not None:
        with requests.get(
            url,
            stream=True,
            headers={"User-Agent": USER_AGENT},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            lm = resp.headers.get("Last-Modified")
            etag = resp.headers.get("ETag")
            with dest.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        out.write(chunk)
            return lm, etag
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
        lm = resp.headers.get("Last-Modified")
        etag = resp.headers.get("ETag")
        with dest.open("wb") as out:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
        return lm, etag


def _parse_feed_info(zip_path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "publisher_name": "",
        "publisher_url": "",
        "feed_version": "",
        "feed_start_date": "",
        "feed_end_date": "",
        "agency_names": [],
    }
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            if "feed_info.txt" in names:
                with zf.open("feed_info.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                    for row in reader:
                        info["publisher_name"] = row.get("feed_publisher_name", "") or ""
                        info["publisher_url"] = row.get("feed_publisher_url", "") or ""
                        info["feed_version"] = row.get("feed_version", "") or ""
                        info["feed_start_date"] = row.get("feed_start_date", "") or ""
                        info["feed_end_date"] = row.get("feed_end_date", "") or ""
                        break
            if "agency.txt" in names:
                with zf.open("agency.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                    info["agency_names"] = [
                        (row.get("agency_name") or "").strip() for row in reader
                    ]
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        pass
    return info


def is_up_to_date_cheap(agency: str, url: str, storage: Any = None) -> bool:
    """Local sidecar + one HEAD request for freshness."""
    s = storage or LocalStorage()
    rel = feed_rel_path(agency)
    existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
    if not existing:
        return False
    source = existing.get("source") or {}
    if source.get("url") != url:
        return False
    path = feed_abs_path(agency, s)
    if not path.exists():
        return False
    if path.stat().st_size != existing.get("size_bytes"):
        return False
    lm, etag = _http_head(url)
    extra = existing.get("extra") or {}
    rec_http = extra.get("http", {})
    if etag is not None and rec_http.get("etag") == etag:
        return True
    if lm is not None and rec_http.get("last_modified") == lm:
        return True
    if lm is None and etag is None:
        return False
    return False


def is_cached_locally(agency: str, storage: Any = None) -> bool:
    s = storage or LocalStorage()
    rel = feed_rel_path(agency)
    existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
    if not existing:
        return False
    path = feed_abs_path(agency, s)
    if not path.exists():
        return False
    return path.stat().st_size == existing.get("size_bytes")


def _to_iso_utc(http_date: str | None) -> str | None:
    if not http_date:
        return None
    try:
        dt = parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def download(
    agency: str,
    url: str,
    *,
    force: bool = False,
    storage: Any = None,
) -> DownloadResult:
    """Download a GTFS feed for ``agency`` from ``url``."""
    if not agency or "/" in agency:
        raise DownloadError(f"agency must be non-empty and contain no '/': {agency!r}")
    if not url:
        raise DownloadError("url is required")
    s = storage or LocalStorage()

    with _agency_lock(agency):
        out_abs = feed_abs_path(agency, s)
        rel = feed_rel_path(agency)

        if not force and is_up_to_date_cheap(agency, url, s):
            existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s) or {}
            extra = existing.get("extra") or {}
            http = extra.get("http", {})
            return DownloadResult(
                agency=agency,
                path=str(out_abs),
                relative_path=rel,
                source_url=url,
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                last_modified=http.get("last_modified"),
                etag=http.get("etag"),
                feed_info=extra.get("feed_info", {}),
                downloaded_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                sidecar=existing,
            )

        staging = _staging_path(agency)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()

        start = time.monotonic()
        try:
            last_modified, etag = _http_download(url, staging)
        except Exception as exc:  # noqa: BLE001
            if staging.exists():
                staging.unlink()
            raise DownloadError(f"download failed: {exc}") from exc
        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(staging)
        if size == 0:
            staging.unlink()
            raise DownloadError(f"downloaded feed is empty: {url}")
        try:
            zipfile.ZipFile(staging, "r").testzip()
        except zipfile.BadZipFile as exc:
            staging.unlink()
            raise DownloadError(f"downloaded file is not a valid zip: {url}") from exc

        feed_info = _parse_feed_info(staging)

        s.finalize_from_local(str(staging), str(out_abs))

        downloaded_at = sidecar.utcnow_iso()
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            rel,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source={"url": url},
            tool={
                "command": "urllib" if requests is None else "requests",
            },
            extra={
                "agency": agency,
                "http": {
                    "last_modified": last_modified,
                    "etag": etag,
                    "last_modified_iso": _to_iso_utc(last_modified),
                },
                "feed_info": feed_info,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=downloaded_at,
            storage=s,
        )

        return DownloadResult(
            agency=agency,
            path=str(out_abs),
            relative_path=rel,
            source_url=url,
            size_bytes=size,
            sha256=sha256_hex,
            last_modified=last_modified,
            etag=etag,
            feed_info=feed_info,
            downloaded_at=downloaded_at,
            duration_seconds=elapsed,
            was_cached=False,
            sidecar=side,
        )


def list_feeds(storage: Any = None) -> list[dict[str, Any]]:
    s = storage or LocalStorage()
    out = sidecar.list_entries(NAMESPACE, CACHE_TYPE, s)
    out.sort(key=lambda e: (e.get("extra") or {}).get("agency", ""))
    return out
