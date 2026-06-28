"""Fleet-wide Geofabrik download concurrency gate (Mongo-backed semaphore).

The whole runner fleet shares one egress IP. Geofabrik rate-limits / bans an IP
that opens too many parallel full-extract downloads at once (a fleet-wide
refresh fanning 255 regions out across the fleet = 255 simultaneous multi-GB
GETs from one IP → the IP gets blocked). Per-runner caps don't help: each
runner only sees its own downloads, never the fleet total.

This module caps the **total** number of concurrent Geofabrik downloads across
the entire fleet with a distributed semaphore stored in MongoDB:

- Collection ``osm_download_gate`` holds ``N`` fixed slot docs
  ``{_id: "slot-<i>", holder: <token|None>, lease_expires: <epoch_ms>}``.
- ``acquire_slot`` atomically claims a FREE *or* LEASE-EXPIRED slot via a single
  ``find_one_and_update`` (so two runners can't grab the same slot), retrying
  with backoff + jitter until a slot frees or a deadline passes.
- A slot is held under a *lease*: a long (multi-GB) download must ``renew_slot``
  periodically (a heartbeat thread) so it keeps its slot; if the holding runner
  crashes, its lease simply expires and another runner reclaims the slot. No
  cleanup daemon, no leader.

**Graceful degradation.** If ``FW_MONGODB_URL`` is unset, or pymongo isn't
importable, the gate is a *no-op*: ``acquire_slot`` returns a sentinel token and
``release_slot``/``renew_slot`` do nothing, and ``download_slot`` is a plain
pass-through. Local dev and the offline test suite never block on Mongo.

Config (env):
    FW_MONGODB_URL                 Mongo connection string (gate active iff set)
    FW_OSM_DOWNLOAD_CONCURRENCY    max concurrent fleet downloads (default 3)
    FW_OSM_DOWNLOAD_LEASE_MS       per-slot lease, ms (default 14400000 = 4h,
                                    matching the OSM task execution timeout)
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

COLLECTION = "osm_download_gate"

# A slot id of None means "the gate is disabled" (no Mongo). It's still a valid
# token so callers can release/renew it unconditionally.
_DISABLED_TOKEN = "gate-disabled"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_concurrency() -> int:
    try:
        n = int(os.environ.get("FW_OSM_DOWNLOAD_CONCURRENCY") or 3)
    except ValueError:
        n = 3
    return max(1, n)


def _default_lease_ms() -> int:
    try:
        # Default to the OSM task execution timeout (4h) so a slow multi-GB
        # download that *isn't* heart-beating still doesn't lose its slot before
        # the task itself is reaped.
        return int(os.environ.get("FW_OSM_DOWNLOAD_LEASE_MS") or 14_400_000)
    except ValueError:
        return 14_400_000


@dataclass
class SlotToken:
    """Handle returned by :func:`acquire_slot`; pass to release/renew."""

    slot_id: str | None        # None when the gate is disabled (no-op token)
    holder: str                # the unique holder id we stamped on the slot

    @property
    def active(self) -> bool:
        return self.slot_id is not None


# A no-op token used whenever the gate is disabled.
_NOOP_TOKEN = SlotToken(slot_id=None, holder=_DISABLED_TOKEN)


# Cache the resolved collection per process so we don't reconnect on every
# acquire. None => not yet resolved; False => resolved-as-disabled.
_collection_cache: object = None
_collection_lock = threading.Lock()


def _resolve_collection():
    """Return the Mongo collection, or ``None`` when the gate is disabled.

    Disabled when ``FW_MONGODB_URL`` is unset or pymongo can't be imported /
    connected. The result is cached for the life of the process.
    """
    global _collection_cache
    if _collection_cache is not None:
        return None if _collection_cache is False else _collection_cache

    with _collection_lock:
        if _collection_cache is not None:
            return None if _collection_cache is False else _collection_cache

        url = os.environ.get("FW_MONGODB_URL")
        if not url:
            logger.debug("Download gate disabled: FW_MONGODB_URL unset (no-op).")
            _collection_cache = False
            return None
        try:
            from pymongo import MongoClient
        except Exception as exc:  # pragma: no cover - pymongo always present in test env
            logger.warning("Download gate disabled: pymongo unimportable (%s).", exc)
            _collection_cache = False
            return None
        try:
            db_name = os.environ.get("FW_MONGODB_DB") or "facetwork"
            client = MongoClient(url, serverSelectionTimeoutMS=5000)
            coll = client[db_name][COLLECTION]
        except Exception as exc:
            logger.warning("Download gate disabled: Mongo connect failed (%s).", exc)
            _collection_cache = False
            return None
        _collection_cache = coll
        return coll


def _ensure_slots(coll, max_concurrency: int) -> None:
    """Idempotently create slot docs ``slot-0 .. slot-(N-1)``.

    Uses upsert-on-insert so concurrent runners racing to seed the collection
    don't clobber an existing holder: ``$setOnInsert`` only writes when the doc
    is created. Shrinking N later leaves orphan high-index slots unused (they
    just never get claimed) — harmless.
    """
    for i in range(max_concurrency):
        coll.update_one(
            {"_id": f"slot-{i}"},
            {"$setOnInsert": {"holder": None, "lease_expires": 0}},
            upsert=True,
        )


def acquire_slot(
    *,
    max_concurrency: int | None = None,
    lease_ms: int | None = None,
    deadline_s: float | None = None,
    poll_min_s: float = 1.0,
    poll_max_s: float = 15.0,
) -> SlotToken:
    """Claim one of ``max_concurrency`` fleet-wide download slots.

    Atomically grabs a slot that is FREE (``holder=None``) or whose lease has
    EXPIRED (``lease_expires < now``), stamping our unique holder id and a fresh
    lease. Retries with exponential backoff + jitter until a slot frees.

    Returns a :class:`SlotToken`. When the gate is disabled (no Mongo) returns a
    no-op token immediately. When ``deadline_s`` is exceeded without a free slot,
    returns the no-op token too (fail-open: never wedge a download forever — the
    cap is a courtesy throttle, not a hard correctness barrier).
    """
    coll = _resolve_collection()
    if coll is None:
        return _NOOP_TOKEN

    n = max_concurrency if max_concurrency is not None else _default_concurrency()
    lease = lease_ms if lease_ms is not None else _default_lease_ms()
    n = max(1, n)

    try:
        _ensure_slots(coll, n)
    except Exception as exc:
        logger.warning("Download gate: slot-seeding failed, proceeding ungated (%s).", exc)
        return _NOOP_TOKEN

    holder = uuid.uuid4().hex
    start = time.monotonic()
    backoff = poll_min_s
    while True:
        now = _now_ms()
        try:
            doc = coll.find_one_and_update(
                {
                    "_id": {"$in": [f"slot-{i}" for i in range(n)]},
                    "$or": [{"holder": None}, {"lease_expires": {"$lt": now}}],
                },
                {"$set": {"holder": holder, "lease_expires": now + lease}},
            )
        except Exception as exc:
            logger.warning("Download gate: claim query failed, proceeding ungated (%s).", exc)
            return _NOOP_TOKEN

        if doc is not None:
            slot_id = doc["_id"]
            logger.info("Download gate: acquired %s (holder=%s, cap=%d).", slot_id, holder, n)
            return SlotToken(slot_id=slot_id, holder=holder)

        if deadline_s is not None and (time.monotonic() - start) >= deadline_s:
            logger.warning(
                "Download gate: no slot within %.0fs (cap=%d); proceeding ungated.",
                deadline_s, n,
            )
            return _NOOP_TOKEN

        sleep_for = min(poll_max_s, backoff) * (0.5 + random.random())
        time.sleep(sleep_for)
        backoff = min(poll_max_s, backoff * 2)


def renew_slot(token: SlotToken, *, lease_ms: int | None = None) -> bool:
    """Extend ``token``'s lease (heartbeat for long downloads).

    Only renews if WE still hold the slot (``holder`` matches) — if our lease
    already expired and another runner reclaimed it, this is a no-op returning
    ``False``. Disabled/no-op tokens return ``True`` trivially.
    """
    if not token.active:
        return True
    coll = _resolve_collection()
    if coll is None:
        return True
    lease = lease_ms if lease_ms is not None else _default_lease_ms()
    now = _now_ms()
    try:
        res = coll.update_one(
            {"_id": token.slot_id, "holder": token.holder},
            {"$set": {"lease_expires": now + lease}},
        )
    except Exception as exc:
        logger.warning("Download gate: renew failed for %s (%s).", token.slot_id, exc)
        return False
    return res.modified_count > 0 or res.matched_count > 0


def release_slot(token: SlotToken) -> None:
    """Free ``token``'s slot so another runner can claim it.

    Only frees the slot if WE still hold it (guards against releasing a slot a
    crashed-then-reclaimed lease already handed to someone else). No-op for
    disabled tokens.
    """
    if not token.active:
        return
    coll = _resolve_collection()
    if coll is None:
        return
    try:
        coll.update_one(
            {"_id": token.slot_id, "holder": token.holder},
            {"$set": {"holder": None, "lease_expires": 0}},
        )
        logger.info("Download gate: released %s (holder=%s).", token.slot_id, token.holder)
    except Exception as exc:
        logger.warning("Download gate: release failed for %s (%s).", token.slot_id, exc)


@contextlib.contextmanager
def download_slot(
    *,
    max_concurrency: int | None = None,
    lease_ms: int | None = None,
    deadline_s: float | None = None,
    renew_interval_s: float | None = None,
):
    """Context manager: hold a fleet-wide download slot for the wrapped block.

    Acquires a slot on enter, spins a daemon heartbeat thread that renews the
    lease at ``renew_interval_s`` (default: a third of the lease) so a long
    multi-GB download keeps its slot, and releases on exit (and stops the
    heartbeat). A disabled gate makes this a transparent pass-through.

    Usage::

        with download_slot(max_concurrency=3):
            _download_resumable(...)
    """
    lease = lease_ms if lease_ms is not None else _default_lease_ms()
    token = acquire_slot(
        max_concurrency=max_concurrency,
        lease_ms=lease,
        deadline_s=deadline_s,
    )
    stop = threading.Event()
    hb: threading.Thread | None = None

    if token.active:
        interval = renew_interval_s if renew_interval_s is not None else max(30.0, lease / 1000.0 / 3.0)

        def _heartbeat() -> None:
            while not stop.wait(interval):
                renew_slot(token, lease_ms=lease)

        hb = threading.Thread(target=_heartbeat, name="osm-download-gate-renew", daemon=True)
        hb.start()

    try:
        yield token
    finally:
        stop.set()
        if hb is not None:
            hb.join(timeout=5.0)
        release_slot(token)
