"""Lazy, advisory indexes of cache-type contents.

Per ``agent-spec/cache-layout.agent-spec.yaml``, each ``(namespace,
cache_type)`` pair may have a cached index at::

    AFL_DATA_ROOT/_indexes/<namespace>/<cache_type>.index.json

The index is **never authoritative** — the sidecars under
``cache/<namespace>/<cache_type>/`` are. The index exists purely so that
listing 10k+ entries does not require 10k filesystem reads. If the index
is absent, stale, or corrupt, readers fall back to a full walk and write
a fresh index.

Staleness policy: if any sidecar under the cache_type subtree has an
mtime newer than the index, the index is stale and will be rebuilt on
next use. Concurrent rebuilds from multiple servers are safe — they
produce the same content (last-writer-wins via atomic write).
"""

from __future__ import annotations

import json
import os
from typing import Any

from _lib import sidecar
from _lib.storage import LocalStorage, Storage, indexes_root

INDEX_VERSION = 1
INDEX_SUFFIX = ".index.json"


def _storage(storage: Storage | None) -> Storage:
    return storage if storage is not None else LocalStorage()


def index_path(
    namespace: str,
    cache_type: str,
    storage: Storage | None = None,
) -> str:
    """Absolute path to the index file."""
    s = _storage(storage)
    return Storage.join(indexes_root(s.name), namespace, cache_type + INDEX_SUFFIX)


def _index_mtime(storage: Storage, path: str) -> float | None:
    """Return the mtime of the index, or None if missing / non-local."""
    if not isinstance(storage, LocalStorage):
        return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _newest_sidecar_mtime(storage: Storage, namespace: str, cache_type: str) -> float | None:
    """Return the mtime of the newest sidecar under the cache_type subtree.

    Returns None on non-local backends (where we don't have per-file mtime
    cheaply). Those backends always rebuild on read.
    """
    if not isinstance(storage, LocalStorage):
        return None
    root = sidecar.cache_dir(namespace, cache_type, storage)
    if not os.path.isdir(root):
        return None
    newest: float = 0.0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(sidecar.SIDECAR_SUFFIX):
                try:
                    m = os.path.getmtime(os.path.join(dirpath, fn))
                except OSError:
                    continue
                if m > newest:
                    newest = m
    return newest or None


def rebuild_index(
    namespace: str,
    cache_type: str,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Walk sidecars, build a fresh index, atomically write it, return it."""
    s = _storage(storage)
    entries = sidecar.list_entries(namespace, cache_type, s)
    summaries: dict[str, Any] = {}
    for e in entries:
        rel = e.get("relative_path")
        if not rel:
            continue
        summaries[rel] = {
            "relative_path": rel,
            "size_bytes": e.get("size_bytes"),
            "sha256": e.get("sha256"),
            "kind": e.get("kind"),
            "generated_at": e.get("generated_at"),
        }
    data = {
        "version": INDEX_VERSION,
        "generated_at": sidecar.utcnow_iso(),
        "namespace": namespace,
        "cache_type": cache_type,
        "entries": summaries,
    }
    path = index_path(namespace, cache_type, s)
    s.mkdir_p(Storage.dirname(path))
    s.write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def read_index(
    namespace: str,
    cache_type: str,
    *,
    rebuild_if_stale: bool = True,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Return the index, rebuilding it if missing or stale.

    When ``rebuild_if_stale`` is False, a stale-looking index is returned
    as-is (useful for diagnostics). In normal use, leave it True.
    """
    s = _storage(storage)
    path = index_path(namespace, cache_type, s)
    if not s.exists(path):
        return rebuild_index(namespace, cache_type, s)
    try:
        raw = json.loads(s.read_text(path))
    except (OSError, ValueError):
        return rebuild_index(namespace, cache_type, s)
    if not rebuild_if_stale:
        return raw
    idx_mt = _index_mtime(s, path)
    newest = _newest_sidecar_mtime(s, namespace, cache_type)
    if idx_mt is not None and newest is not None and newest > idx_mt:
        return rebuild_index(namespace, cache_type, s)
    if idx_mt is None:
        # Non-local backend: we can't tell staleness cheaply, so trust the
        # on-disk index. Callers that need a fresh view should call
        # rebuild_index explicitly.
        return raw
    return raw
