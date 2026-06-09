"""Resumable-download tests: a mid-stream connection drop must resume via HTTP
Range and produce the correct full file + checksums, not restart from zero."""

import hashlib

import pytest

pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_download")


class _FakeResp:
    def __init__(self, status_code, headers, chunks, raise_at=None, exc=None):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self._raise_at = raise_at
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=0):
        for i, c in enumerate(self._chunks):
            if self._raise_at is not None and i == self._raise_at:
                raise self._exc
            yield c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)


def _chunked(data, n=1000):
    return [data[i:i + n] for i in range(0, len(data), n)]


def test_resume_after_midstream_drop(tmp_path, monkeypatch):
    full = bytes((i * 7) % 256 for i in range(20_000))  # 20 KB deterministic
    calls = {"n": 0, "ranges": []}

    def fake_get(url, stream=True, headers=None, timeout=None):
        calls["n"] += 1
        rng = (headers or {}).get("Range")
        calls["ranges"].append(rng)
        if rng is None:
            # First attempt: stream 7 chunks (7000 bytes), then drop.
            return _FakeResp(
                200, {"Content-Length": str(len(full))}, _chunked(full),
                raise_at=7, exc=pd.requests.exceptions.ConnectionError("boom"),
            )
        start = int(rng.split("=")[1].split("-")[0])
        remaining = full[start:]
        return _FakeResp(
            206,
            {"Content-Range": f"bytes {start}-{len(full) - 1}/{len(full)}",
             "Content-Length": str(len(remaining))},
            _chunked(remaining),
        )

    monkeypatch.setattr(pd.requests, "get", fake_get)
    staged = str(tmp_path / "region.tmp")
    size, sha, md5 = pd._download_resumable("http://x/file.pbf", staged, "region", None)

    assert size == len(full)
    assert open(staged, "rb").read() == full
    assert sha == hashlib.sha256(full).hexdigest()
    assert md5 == hashlib.md5(full).hexdigest()
    assert calls["n"] >= 2                      # it reconnected
    assert calls["ranges"][1] == "bytes=7000-"  # resumed from the drop offset


def test_server_ignores_range_restarts_clean(tmp_path, monkeypatch):
    """If the mirror ignores Range and replies 200 with the whole body on the
    resume attempt, we must truncate + rehash so the file isn't doubled."""
    full = bytes((i * 3) % 256 for i in range(10_000))

    def fake_get(url, stream=True, headers=None, timeout=None):
        rng = (headers or {}).get("Range")
        if rng is None:
            return _FakeResp(
                200, {"Content-Length": str(len(full))}, _chunked(full),
                raise_at=3, exc=pd.requests.exceptions.ChunkedEncodingError("drop"),
            )
        # Mirror ignores Range -> 200 with the FULL body again.
        return _FakeResp(200, {"Content-Length": str(len(full))}, _chunked(full))

    monkeypatch.setattr(pd.requests, "get", fake_get)
    staged = str(tmp_path / "region.tmp")
    size, sha, md5 = pd._download_resumable("http://x/file.pbf", staged, "region", None)

    assert size == len(full)
    assert open(staged, "rb").read() == full          # not doubled
    assert sha == hashlib.sha256(full).hexdigest()


def test_clean_download_no_drop(tmp_path, monkeypatch):
    full = bytes((i * 11) % 256 for i in range(5_000))

    def fake_get(url, stream=True, headers=None, timeout=None):
        return _FakeResp(200, {"Content-Length": str(len(full))}, _chunked(full))

    monkeypatch.setattr(pd.requests, "get", fake_get)
    staged = str(tmp_path / "region.tmp")
    size, sha, md5 = pd._download_resumable("http://x/file.pbf", staged, "region", None)
    assert size == len(full)
    assert open(staged, "rb").read() == full
    assert md5 == hashlib.md5(full).hexdigest()
