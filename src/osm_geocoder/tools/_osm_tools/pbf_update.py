"""Incremental PBF cache update via Geofabrik replication diffs (pyosmium 4.x).

Geofabrik publishes per-extract replication directories (``<region>-updates/``
with ``state.txt`` + sequenced ``.osc.gz`` diffs). Applying the day's diffs to a
cached extract transfers KB–MB instead of re-downloading the multi-GB PBF — the
recommended, rate-limit-friendly way to keep an extract current. Tool side of
the ``osm.cache.UpdateRegion`` facet.

Uses the pyosmium 4.x replication API (ships with the ``osmium`` dependency — no
extra install): ``osmium.replication.get_replication_header`` reads the cached
extract's replication ``(url, sequence, timestamp)`` baseline from the PBF
header, and ``ReplicationServer.apply_diffs_to_file`` downloads + merges the
diffs into an updated PBF.

Outcomes (``UpdateResult.method``):
  - ``"current"`` already at the latest replication sequence (no diffs).
  - ``"diff"``    diffs fetched + applied; updated PBF finalized to cache.
  - ``"full"``    fell back to a full ``download_region(force=True)`` — no
                  replication baseline in the header, or too far behind to catch
                  up within ``max_diff_mb`` (full re-pull is then cheaper).

``_apply_geofabrik_diffs`` is the network seam (mocked in tests); the decision
tree + fallbacks are unit-tested offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import osmium.replication as _replication
from osmium.replication.server import ReplicationServer

from .pbf_download import (
    cached_path,
    download_region,
    get_storage,
    is_region_cached,
    region_to_paths,
    to_osm_cache,
)
from .storage import Storage

# Default diff budget. Caps how much diff data we'll pull before deciding a
# region is too far behind for diffs to beat a full re-download. A normally-
# current cache (days–weeks old) needs far less; this bounds the waste on
# pathologically-stale extracts.
DEFAULT_MAX_DIFF_MB = 512


@dataclass
class AppliedDiffs:
    status: str         # "updated" | "current" | "stale" | "no_baseline"
    bytes_changed: int  # net |after-before| of the PBF (proxy for data moved)


@dataclass
class UpdateResult:
    method: str         # "current" | "diff" | "full"
    applied_bytes: int
    cache: dict         # OSMCache: {region, url, path, date, size, wasInCache}


def _osm_cache_dict(region_path: str, local_pbf: str, *, region: dict | None,
                    was_in_cache: bool) -> dict:
    rel, url = region_to_paths(region_path)
    size = os.path.getsize(local_pbf) if os.path.exists(local_pbf) else 0
    reg = region or {
        "query": "", "name": "", "canonical": region_path, "level": "",
        "level_label": "", "parent_canonical": "", "continent": "",
        "geofabrik_path": region_path,
    }
    return {"region": reg, "url": url, "path": cached_path(region_path),
            "date": datetime.now(timezone.utc).isoformat(), "size": size,
            "wasInCache": was_in_cache}


def _apply_geofabrik_diffs(local_pbf: str, region_path: str, max_diff_mb: int) -> AppliedDiffs:
    """Update ``local_pbf`` in place from Geofabrik replication, ≤ max_diff_mb.

    THE NETWORK SEAM (mocked in tests). Reads the replication baseline from the
    PBF header and applies forward diffs via pyosmium 4.x.
    """
    header = _replication.get_replication_header(local_pbf)  # (url, sequence, timestamp)
    if not header.url or header.sequence is None:
        return AppliedDiffs("no_baseline", 0)

    out = local_pbf + ".updated.pbf"          # apply_diffs_to_file requires a fresh path
    if os.path.exists(out):
        os.remove(out)
    before = os.path.getsize(local_pbf)
    server = ReplicationServer(header.url)
    try:
        # max_size is in kB; returns (last_applied_seq, newest_available_seq) or
        # None when there is nothing to apply (already current).
        result = server.apply_diffs_to_file(
            local_pbf, out, header.sequence + 1, max_size=max_diff_mb * 1024,
        )
    finally:
        server.close()

    if result is None:                         # already at the latest sequence
        if os.path.exists(out):
            os.remove(out)
        return AppliedDiffs("current", 0)

    os.replace(out, local_pbf)                 # atomically swap in the updated PBF
    changed = abs(os.path.getsize(local_pbf) - before)
    last_seq, newest_seq = result
    # last < newest => the diff budget was hit before catching up: the extract is
    # so far behind that a full re-download is cheaper. We've already paid up to
    # the (bounded) budget; signal "stale" so the caller does a clean full pull.
    return AppliedDiffs("stale" if last_seq < newest_seq else "updated", changed)


def update_region(
    region_path: str,
    *,
    max_diff_mb: int = DEFAULT_MAX_DIFF_MB,
    storage: Storage | None = None,
    region: dict | None = None,
) -> UpdateResult:
    """Update a region's cached PBF via replication diffs; full-download fallback."""
    storage = storage or get_storage()

    if not is_region_cached(region_path, storage=storage):       # no baseline to diff
        res = download_region(region_path, force=True, storage=storage)
        return UpdateResult("full", res.size, to_osm_cache(res, region=region))

    dst = cached_path(region_path)
    local_pbf = storage.localize(dst)          # no-op for LocalStorage; scratch copy for S3/HDFS
    applied = _apply_geofabrik_diffs(local_pbf, region_path, max_diff_mb)

    if applied.status == "current":
        return UpdateResult("current", 0,
                            _osm_cache_dict(region_path, local_pbf, region=region, was_in_cache=True))

    if applied.status == "updated":
        if local_pbf != dst:                   # S3/HDFS: upload the updated PBF back
            storage.finalize_from_local(local_pbf, dst)
        # Replication header (sequence/timestamp) is rewritten by apply_diffs_to_file;
        # refresh the .meta.json sidecar so revalidate/UpdateRegion see new size/ts.
        _refresh_sidecar(region_path, local_pbf, storage=storage)
        return UpdateResult("diff", applied.bytes_changed,
                            _osm_cache_dict(region_path, local_pbf, region=region, was_in_cache=False))

    # "stale" (beyond budget) or "no_baseline" -> clean full re-pull (overwrites
    # any partial diff progress). Rare; bounded by the diff budget above.
    res = download_region(region_path, force=True, storage=storage)
    return UpdateResult("full", res.size, to_osm_cache(res, region=region))


def _refresh_sidecar(region_path: str, local_pbf: str, *, storage: Storage) -> None:
    """Update the cache sidecar's size after a diff apply (best-effort metadata).

    Keeps the prior content hash rather than rehashing a multi-GB PBF on the
    light diff path — a later ``revalidate``/full download re-establishes the
    exact sha256. The replication state itself lives in the PBF header (rewritten
    by ``apply_diffs_to_file``), which is what the next ``UpdateRegion`` reads.
    """
    try:
        from . import sidecar  # sidecar protocol lives alongside pbf_download
        rel, _ = region_to_paths(region_path)
        old = sidecar.read_sidecar("osm", "pbf", rel, storage)
        if not old or not old.get("sha256"):
            return  # no prior baseline to preserve; skip rather than fabricate
        extra = dict(old.get("extra") or {})
        extra.update(updated_via="replication-diff",
                     updated_at=datetime.now(timezone.utc).isoformat())
        sidecar.write_sidecar(
            "osm", "pbf", rel,
            kind="file",
            size_bytes=os.path.getsize(local_pbf),
            sha256=old["sha256"],          # carried over; revalidate re-establishes exact value
            source=old.get("source"),
            extra=extra,
            storage=storage,
        )
    except Exception as exc:  # noqa: BLE001 — sidecar refresh is best-effort metadata
        import logging
        logging.getLogger(__name__).warning(
            "UpdateRegion: sidecar refresh failed for %s: %s", region_path, exc)
