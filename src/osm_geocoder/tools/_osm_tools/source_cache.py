"""Host-local cache of source extracts fetched from the object store.

A country or state tier is cut from a CONTINENT extract that lives in the object
store. Every task fetched its own copy into a per-task scratch directory and
deleted it afterwards, so the same 21 GB north-america was downloaded again by
every task that needed it -- measured 2026-09-30: twice concurrently (us-states
and na-countries) plus a third time by an execution that had been reclaimed,
while seven continents were being pulled at once and the store's disk had
turned seek-bound at ~12 MB/s total.

``fetch()`` keeps ONE copy per host per object VERSION:

- **Keyed by the object's ETag**, so a republished continent is a new entry and
  a stale copy is never served. Older versions of the same key are evicted when
  a new one lands.
- **One download per host.** The first task takes a lock file and downloads to a
  unique ``.part``, renamed into place when complete (atomic); other tasks on the
  host wait and then reuse it. A lock file -- not ``flock`` -- because the cache is
  shared by several containers over a bind mount, where advisory locks are not
  reliably shared. The holder touches the lock as bytes arrive; a lock untouched
  for ``STALE_S`` belonged to a killed task and is taken over.
- **Fleet-wide read cap.** A cache MISS takes a slot of the ``object-store``
  download gate, so a fan-out cannot open more concurrent multi-GB reads than
  the store's disk can serve.
- **Bounded.** Least-recently-used entries are evicted above
  ``FW_OSM_SOURCE_CACHE_GB`` (default 200), and when free space would not hold the
  object plus a reserve, the cache steps aside and returns None -- the caller
  downloads into its own scratch as before. A cache must never be why a task
  fails.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
import uuid
from pathlib import Path
from typing import Callable

STALE_S = 300
_TOUCH_S = 30
_WAIT_S = 10
_RESERVE_BYTES = 10 * 1024**3


def cache_root(scratch_base: str) -> Path:
    return Path(os.environ.get("FW_OSM_SOURCE_CACHE") or os.path.join(scratch_base, "source-cache"))


def _cap_bytes() -> int:
    try:
        return int(float(os.environ.get("FW_OSM_SOURCE_CACHE_GB") or 200) * 1024**3)
    except ValueError:
        return 200 * 1024**3


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "__", key)


def _entries(root: Path) -> list[Path]:
    return [p for p in root.glob("*.osm.pbf") if p.is_file()]


def _locked(entry: Path) -> bool:
    lock = entry.with_name(entry.name + ".lock")
    try:
        return time.time() - lock.stat().st_mtime < STALE_S
    except OSError:
        return False


def _evict(root: Path, keep: Path, safe_key: str, incoming: int, log: Callable[[str], None]) -> None:
    """Drop older versions of this key, then LRU entries until under the cap."""
    for p in _entries(root):
        if p != keep and p.name.startswith(safe_key + ".") and not _locked(p):
            log(f"source cache: evicting superseded {p.name}")
            p.unlink(missing_ok=True)
    cap = _cap_bytes()
    entries = sorted((p for p in _entries(root) if p != keep), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in entries) + incoming
    for p in entries:
        if total <= cap:
            break
        if _locked(p):
            continue
        total -= p.stat().st_size
        log(f"source cache: evicting least-recently-used {p.name}")
        p.unlink(missing_ok=True)


def fetch(
    s3,
    bucket: str,
    key: str,
    *,
    scratch_base: str,
    log: Callable[[str], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    gate_concurrency: int | None = None,
) -> str | None:
    """Local path to ``s3://bucket/key`` at its current version, or None when the
    cache cannot hold it (the caller then downloads into its own scratch)."""
    say = log or (lambda _m: None)
    cancel = check_cancel or (lambda: None)
    head = s3.head_object(Bucket=bucket, Key=key)
    etag = str(head.get("ETag", "")).strip('"').replace("-", "_")[:32] or "noetag"
    size = int(head.get("ContentLength") or 0)
    root = cache_root(scratch_base)
    root.mkdir(parents=True, exist_ok=True)
    safe_key = _safe(key)
    entry = root / f"{safe_key}.{etag}.osm.pbf"
    lock = entry.with_name(entry.name + ".lock")
    waited = False

    while True:
        cancel()
        if entry.exists() and entry.stat().st_size == size:
            os.utime(entry)  # LRU recency
            say(f"source cache: reusing {key} ({size / 1e9:.1f} GB, version {etag[:8]})"
                + (" after waiting for this host's download" if waited else ""))
            return str(entry)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # released between the two calls
            if age >= STALE_S:
                say(f"source cache: taking over a lock untouched for {age:.0f}s ({lock.name})")
                lock.unlink(missing_ok=True)
                continue
            if not waited:
                say(f"source cache: another task on this host is downloading {key} — waiting for it")
            waited = True
            time.sleep(_WAIT_S)
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{socket.gethostname()} {os.getpid()} {time.time():.0f}\n")
        break

    part = entry.with_name(f"{entry.name}.{uuid.uuid4().hex}.part")
    try:
        if shutil.disk_usage(root).free < size + _RESERVE_BYTES:
            say(f"source cache: not enough free space for {key} ({size / 1e9:.1f} GB) — "
                "downloading into task scratch instead")
            return None
        _evict(root, entry, safe_key, size, say)

        last_touch = [time.monotonic()]

        def _progress(_n: int) -> None:
            cancel()  # raising aborts the transfer
            now = time.monotonic()
            if now - last_touch[0] >= _TOUCH_S:
                os.utime(lock)
                last_touch[0] = now

        from .download_gate import download_slot

        def _while_queued() -> None:
            cancel()
            os.utime(lock)  # queued, not dead: keep other tasks waiting on us

        n = gate_concurrency
        if n is None:
            try:
                n = int(os.environ.get("FW_OSM_SOURCE_READ_CONCURRENCY") or 3)
            except ValueError:
                n = 3
        say(f"source cache: downloading {key} ({size / 1e9:.1f} GB, version {etag[:8]})")
        with download_slot(gate="object-store", max_concurrency=n, while_waiting=_while_queued):
            os.utime(lock)
            s3.download_file(bucket, key, str(part), Callback=_progress)
        if part.stat().st_size != size:
            raise OSError(f"downloaded {part.stat().st_size} bytes of {size} for {key}")
        os.replace(part, entry)
        return str(entry)
    finally:
        part.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
