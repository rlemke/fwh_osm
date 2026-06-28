"""Tests for rate-limit-safe behaviour in the PBF downloader:

  * HTTP 429 with Retry-After during the stream → back off (sleep) + retry within
    the resume budget → success (not a hard fail, not a hammer).
  * ``fetch_md5`` retries a 429 then succeeds.
  * Conditional-GET revalidation: a cached Last-Modified + an upstream 304 →
    the (multi-GB) re-download is skipped and the cache is returned as current.

All offline: ``requests.get`` / ``urlopen`` are stubbed; the download gate is a
no-op because FW_MONGODB_URL is unset under the test env.
"""

import hashlib
import io
import urllib.error

import pytest

pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_download")


@pytest.fixture(autouse=True)
def _fast_and_ungated(monkeypatch):
    # No real sleeping; gate disabled so the network fetch isn't blocked.
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)
    monkeypatch.delenv("FW_MONGODB_URL", raising=False)
    pd.download_gate._collection_cache = None
    yield
    pd.download_gate._collection_cache = None


def _chunked(data, n=1000):
    return [data[i:i + n] for i in range(0, len(data), n)]


class _FakeResp:
    """Stands in for a requests streaming response."""

    def __init__(self, status_code, headers, chunks):
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=0):
        yield from self._chunks


# ---------------------------------------------------------------------------
# 1. 429 + Retry-After during the stream → backoff + retry → success
# ---------------------------------------------------------------------------

def test_stream_429_then_retry_success(tmp_path, monkeypatch):
    full = bytes((i * 5) % 256 for i in range(8_000))
    state = {"n": 0, "slept": []}
    monkeypatch.setattr(pd.time, "sleep", lambda s: state["slept"].append(s))

    def fake_get(url, stream=True, headers=None, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            # First call: rate-limited with an explicit Retry-After.
            return _FakeResp(429, {"Retry-After": "7"}, [])
        return _FakeResp(200, {"Content-Length": str(len(full))}, _chunked(full))

    monkeypatch.setattr(pd.requests, "get", fake_get)
    staged = str(tmp_path / "r.tmp")
    size, sha, md5 = pd._download_resumable("http://x/file.pbf", staged, "region", None)

    assert size == len(full)
    assert open(staged, "rb").read() == full
    assert sha == hashlib.sha256(full).hexdigest()
    assert state["n"] == 2                       # retried after the 429
    assert state["slept"], "should have backed off"
    # Retry-After=7 (+/-25% jitter) honoured, capped well under the cap.
    assert 5.0 <= state["slept"][0] <= 9.0


def test_fetch_md5_429_then_success(monkeypatch):
    digest = "a" * 32
    state = {"n": 0}
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    class _Md5Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return f"{digest}  file.pbf".encode()

    def fake_urlopen(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(
                "http://x/file.pbf.md5", 429, "Too Many Requests",
                {"Retry-After": "2"}, io.BytesIO(b""),
            )
        return _Md5Resp()

    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)
    got = pd.fetch_md5("http://x/file.pbf")
    assert got == digest
    assert state["n"] == 2


# ---------------------------------------------------------------------------
# 2. Conditional GET revalidation: cached Last-Modified + 304 → skip download
# ---------------------------------------------------------------------------

class _FakeStorage:
    name = "local"

    def size(self, path):
        return 4242

    def mkdir_p(self, p):
        pass


def test_revalidate_304_skips_download(monkeypatch):
    """force=False, region cached, upstream md5 differs (looks stale), but a
    conditional GET returns 304 → return the cache, never download."""
    cached_lm = "2026-05-01T00:00:00Z"
    side = {
        "size_bytes": 4242,
        "sha256": "abc",
        "source": {
            "url": "http://x/file.pbf",
            "source_checksum": {"value": "deadbeef"},
            "source_timestamp": cached_lm,
            "downloaded_at": "2026-05-01T00:00:00Z",
        },
    }
    monkeypatch.setenv("FW_OSM_USE_CACHE_IF_PRESENT", "")  # not pinned-cache mode
    monkeypatch.setattr(pd, "is_region_cached", lambda region, *, storage=None: True)
    monkeypatch.setattr(pd, "sidecar_entry_for", lambda region, *, storage=None: side)
    monkeypatch.setattr(pd, "_region_lock", lambda region: __import__("contextlib").nullcontext())
    # md5 differs from the cached one -> the cache "looks" stale.
    monkeypatch.setattr(pd, "fetch_md5_or_none", lambda url: "f" * 32)
    monkeypatch.setattr(pd, "head_last_modified", lambda url: cached_lm)
    # _already_cached returns None (md5 differs) so we'd normally download.
    monkeypatch.setattr(pd, "_already_cached", lambda *a, **k: None)

    downloaded = {"called": False}

    def _boom(*a, **k):
        downloaded["called"] = True
        raise AssertionError("must not download on a 304")

    monkeypatch.setattr(pd, "_download_resumable", _boom)

    # Conditional GET → 304.
    def fake_urlopen(req, timeout=None):
        assert req.headers.get("If-modified-since"), "should send If-Modified-Since"
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)

    res = pd.download_region("north-america/us/california", storage=_FakeStorage())

    assert downloaded["called"] is False
    assert res.was_cached is True
    assert res.size_bytes == 4242
    assert res.md5 == "deadbeef"


def test_revalidate_not_modified_helper():
    """The conditional-GET helper: 304 → True, 200 → False, no Last-Modified →
    False (can't revalidate)."""
    import contextlib

    cached_lm = "2026-05-01T00:00:00Z"

    # No cached Last-Modified -> cannot revalidate.
    assert pd.revalidate_not_modified("http://x/f.pbf", None) is False

    # 304 -> True.
    def lo_304(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

    with _patched_urlopen(lo_304):
        assert pd.revalidate_not_modified("http://x/f.pbf", cached_lm) is True

    # 200 (changed) -> False.
    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def lo_200(req, timeout=None):
        return _R()

    with _patched_urlopen(lo_200):
        assert pd.revalidate_not_modified("http://x/f.pbf", cached_lm) is False


import contextlib  # noqa: E402


@contextlib.contextmanager
def _patched_urlopen(fn):
    orig = pd.urllib.request.urlopen
    pd.urllib.request.urlopen = fn
    try:
        yield
    finally:
        pd.urllib.request.urlopen = orig
