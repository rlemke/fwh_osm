"""Host-local source cache: one download per host per object version.

2026-09-30: the same 21 GB north-america was being pulled twice at once (plus
by a reclaimed execution) while seven continents saturated the object store's
disk to ~3 MB/s per stream. These pin the cache's contract.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.tools._osm_tools import download_gate  # noqa: E402
from osm_geocoder.tools._osm_tools import source_cache as sc  # noqa: E402


@pytest.fixture(autouse=True)
def _no_gate(monkeypatch):
    """No Mongo: the fleet-wide gate is a pass-through."""
    monkeypatch.delenv("FW_MONGODB_URL", raising=False)
    monkeypatch.setattr(download_gate, "_collection_cache", None)


class FakeS3:
    def __init__(self, data: bytes = b"x" * 4096, etag: str = "v1"):
        self.data, self.etag, self.downloads = data, etag, 0
        self.gate = None  # optional threading.Event a download waits on

    def head_object(self, Bucket, Key):
        return {"ETag": f'"{self.etag}"', "ContentLength": len(self.data)}

    def download_file(self, bucket, key, dst, Callback=None):
        self.downloads += 1
        if self.gate is not None:
            self.gate.wait(5)
        with open(dst, "wb") as fh:
            fh.write(self.data)
        if Callback:
            Callback(len(self.data))


def _fetch(s3, tmp_path, **kw):
    return sc.fetch(s3, "osm-extracts", "north-america-latest.osm.pbf",
                    scratch_base=str(tmp_path), **kw)


def test_second_fetch_of_the_same_version_reuses_the_copy(tmp_path):
    s3 = FakeS3()
    a = _fetch(s3, tmp_path)
    b = _fetch(s3, tmp_path)
    assert a == b and Path(a).read_bytes() == s3.data
    assert s3.downloads == 1


def test_a_new_version_is_downloaded_and_the_old_one_evicted(tmp_path):
    s3 = FakeS3(etag="v1")
    old = _fetch(s3, tmp_path)
    s3.etag, s3.data = "v2", b"y" * 4096
    new = _fetch(s3, tmp_path)
    assert new != old and Path(new).read_bytes() == b"y" * 4096
    assert not Path(old).exists(), "a superseded version must not linger"
    assert s3.downloads == 2


def test_a_second_task_waits_for_the_first_download_instead_of_repeating_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_WAIT_S", 0.05)
    s3 = FakeS3()
    s3.gate = threading.Event()
    out: list[str] = []
    first = threading.Thread(target=lambda: out.append(_fetch(s3, tmp_path)))
    first.start()
    deadline = time.time() + 5
    while not list(sc.cache_root(str(tmp_path)).glob("*.lock")) and time.time() < deadline:
        time.sleep(0.01)
    second = threading.Thread(target=lambda: out.append(_fetch(s3, tmp_path)))
    second.start()
    time.sleep(0.2)
    s3.gate.set()
    first.join(5)
    second.join(5)
    assert len(out) == 2 and out[0] == out[1]
    assert s3.downloads == 1


def test_a_stale_lock_from_a_killed_task_is_taken_over(tmp_path):
    s3 = FakeS3()
    root = sc.cache_root(str(tmp_path))
    root.mkdir(parents=True)
    lock = root / "north-america-latest.osm.pbf.v1.osm.pbf.lock"
    lock.write_text("dead")
    old = time.time() - sc.STALE_S - 5
    os.utime(lock, (old, old))
    assert _fetch(s3, tmp_path) is not None and s3.downloads == 1


def test_no_room_means_step_aside_not_fail(tmp_path, monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr(sc.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    assert _fetch(s3, tmp_path) is None
    assert not list(sc.cache_root(str(tmp_path)).glob("*.lock")), "lock must be released"


def test_cancellation_leaves_no_partial_or_lock(tmp_path):
    s3 = FakeS3()

    class Stop(Exception):
        pass

    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        if calls["n"] > 1:  # let the lock be taken, then cancel mid-transfer
            raise Stop()

    with pytest.raises(Stop):
        _fetch(s3, tmp_path, check_cancel=cancel)
    root = sc.cache_root(str(tmp_path))
    assert not list(root.glob("*.part")) and not list(root.glob("*.lock"))
    assert not list(root.glob("*.osm.pbf"))


def test_a_short_download_is_rejected_not_cached(tmp_path):
    s3 = FakeS3()
    real = s3.download_file

    def short(bucket, key, dst, Callback=None):
        real(bucket, key, dst, Callback)
        with open(dst, "r+b") as fh:
            fh.truncate(10)

    s3.download_file = short
    with pytest.raises(OSError, match="bytes of"):
        _fetch(s3, tmp_path)
    assert not list(sc.cache_root(str(tmp_path)).glob("*.osm.pbf"))


def test_lru_eviction_keeps_the_cache_under_its_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("FW_OSM_SOURCE_CACHE_GB", str(6000 / 1024**3))  # ~1.5 entries
    a = FakeS3(etag="a")
    sc.fetch(a, "b", "africa-latest.osm.pbf", scratch_base=str(tmp_path))
    b = FakeS3(etag="b")
    sc.fetch(b, "b", "asia-latest.osm.pbf", scratch_base=str(tmp_path))
    names = sorted(p.name for p in sc.cache_root(str(tmp_path)).glob("*.osm.pbf"))
    assert names == ["asia-latest.osm.pbf.b.osm.pbf"]
