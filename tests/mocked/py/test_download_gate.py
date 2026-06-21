"""Unit tests for the fleet-wide download concurrency gate.

The whole runner fleet shares one egress IP; Geofabrik bans an IP that opens too
many parallel full-extract downloads. ``download_gate`` is a Mongo-backed
semaphore capping the TOTAL concurrent downloads across the fleet. These tests
run fully offline against ``mongomock`` (the gate's only external dependency is
a pymongo ``MongoClient`` reachable at ``AFL_MONGODB_URL``).

Covered:
  * N concurrent acquires succeed; the (N+1)th blocks, then succeeds after a
    release frees a slot.
  * A slot whose lease has EXPIRED is reclaimable by another holder.
  * With ``AFL_MONGODB_URL`` unset the gate is a transparent no-op (never blocks
    local/test runs).
"""

import importlib

import pytest

mongomock = pytest.importorskip("mongomock")
dg = pytest.importorskip("osm_geocoder.tools._osm_tools.download_gate")


@pytest.fixture
def gate(monkeypatch):
    """A download_gate whose Mongo client is a shared mongomock instance.

    Resets the per-process collection cache and pins a fake clock so lease
    expiry is deterministic. Yields the module plus a ``set_now`` helper.
    """
    client = mongomock.MongoClient()

    # AFL_MONGODB_URL must be set for the gate to engage; the value is irrelevant
    # because we stub MongoClient.
    monkeypatch.setenv("AFL_MONGODB_URL", "mongodb://fake/")
    monkeypatch.setenv("AFL_MONGODB_DB", "facetwork")
    # Fresh resolution each test.
    dg._collection_cache = None

    import pymongo

    monkeypatch.setattr(pymongo, "MongoClient", lambda *a, **k: client)
    # download_gate imports MongoClient lazily inside _resolve_collection via
    # `from pymongo import MongoClient`, which picks up the patched attribute.

    clock = {"now": 1_000_000_000_000}  # epoch ms
    monkeypatch.setattr(dg, "_now_ms", lambda: clock["now"])
    # No real sleeping during backoff.
    monkeypatch.setattr(dg.time, "sleep", lambda *_: None)

    def set_now(ms):
        clock["now"] = ms

    yield dg, set_now
    dg._collection_cache = None


def test_caps_concurrency_and_release_frees_a_slot(gate):
    mod, _ = gate
    cap = 2
    lease = 60_000

    t1 = mod.acquire_slot(max_concurrency=cap, lease_ms=lease)
    t2 = mod.acquire_slot(max_concurrency=cap, lease_ms=lease)
    assert t1.active and t2.active
    assert t1.slot_id != t2.slot_id  # distinct slots

    # The (N+1)th cannot get a slot before a deadline → fail-open no-op token.
    t3 = mod.acquire_slot(max_concurrency=cap, lease_ms=lease, deadline_s=0.0)
    assert not t3.active  # gate is full; returns disabled/no-op token

    # Release one → a real slot frees up.
    mod.release_slot(t1)
    t4 = mod.acquire_slot(max_concurrency=cap, lease_ms=lease, deadline_s=0.0)
    assert t4.active
    assert t4.slot_id == t1.slot_id  # reclaimed the freed slot


def test_expired_lease_is_reclaimable(gate):
    mod, set_now = gate
    lease = 10_000

    t1 = mod.acquire_slot(max_concurrency=1, lease_ms=lease)
    assert t1.active

    # Cap is 1 and it's held → immediate attempt fails.
    assert not mod.acquire_slot(max_concurrency=1, lease_ms=lease, deadline_s=0.0).active

    # Advance past the lease so it's expired; now another holder reclaims it
    # WITHOUT the first ever releasing (simulates a crashed runner).
    set_now(mod._now_ms() + lease + 1)
    t2 = mod.acquire_slot(max_concurrency=1, lease_ms=lease, deadline_s=0.0)
    assert t2.active
    assert t2.slot_id == t1.slot_id
    assert t2.holder != t1.holder

    # The stale holder can no longer renew or steal-release the reclaimed slot.
    assert mod.renew_slot(t1) is False
    mod.release_slot(t1)  # must NOT free t2's slot
    assert not mod.acquire_slot(max_concurrency=1, lease_ms=lease, deadline_s=0.0).active


def test_renew_extends_lease(gate):
    mod, set_now = gate
    lease = 10_000
    t1 = mod.acquire_slot(max_concurrency=1, lease_ms=lease)
    assert t1.active

    # Just before expiry, renew → lease pushed out.
    set_now(mod._now_ms() + lease - 1)
    assert mod.renew_slot(t1, lease_ms=lease) is True

    # Past the ORIGINAL expiry but within the renewed window → still held.
    set_now(mod._now_ms() + lease - 1)
    assert not mod.acquire_slot(max_concurrency=1, lease_ms=lease, deadline_s=0.0).active


def test_context_manager_acquires_and_releases(gate):
    mod, _ = gate
    with mod.download_slot(max_concurrency=1, lease_ms=60_000) as tok:
        assert tok.active
        # Slot is held inside the block.
        assert not mod.acquire_slot(max_concurrency=1, lease_ms=60_000, deadline_s=0.0).active
    # Released on exit.
    after = mod.acquire_slot(max_concurrency=1, lease_ms=60_000, deadline_s=0.0)
    assert after.active


def test_noop_when_mongo_url_unset(monkeypatch):
    monkeypatch.delenv("AFL_MONGODB_URL", raising=False)
    mod = importlib.reload(dg)
    mod._collection_cache = None
    try:
        tok = mod.acquire_slot(max_concurrency=1, lease_ms=1000)
        # Disabled gate hands out a no-op token; never blocks.
        assert not tok.active
        # Many acquires all succeed (no cap enforced).
        for _ in range(10):
            assert not mod.acquire_slot(max_concurrency=1).active
        # release/renew on a no-op token are harmless.
        mod.release_slot(tok)
        assert mod.renew_slot(tok) is True
        with mod.download_slot(max_concurrency=1) as t:
            assert not t.active
    finally:
        mod._collection_cache = None
