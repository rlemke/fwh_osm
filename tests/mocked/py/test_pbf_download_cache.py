"""Unit tests for download_region's pinned-cache (offline) mode.

Geofabrik rebuilds every PBF daily with a fresh md5, so the normal freshness
check treats an older-but-valid cached PBF as stale and re-downloads it. The
opt-in ``FW_OSM_USE_CACHE_IF_PRESENT`` flag short-circuits that: a cached
region is returned as-is with NO Geofabrik call. These tests assert the
no-network behaviour without touching the filesystem or network.
"""

import pytest

pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_download")


class _FakeStorage:
    """Minimal Storage stand-in: only ``name``/``size`` are exercised here."""

    name = "local"

    def size(self, path):  # noqa: D102
        return 4242


def _patch_cached(monkeypatch, *, cached: bool):
    """Make the region look (un)cached and record whether Geofabrik was hit."""
    calls = {"fetch_md5": 0, "head": 0}

    monkeypatch.setattr(pd, "is_region_cached", lambda region, *, storage=None: cached)
    monkeypatch.setattr(
        pd, "sidecar_entry_for",
        lambda region, *, storage=None: {
            "size_bytes": 4242, "sha256": "abc",
            "source": {"url": "u", "source_checksum": {"value": "deadbeef"},
                       "source_timestamp": "2026-05-01T00:00:00Z",
                       "downloaded_at": "2026-05-01T00:00:00Z"},
        },
    )

    def _md5(url):
        calls["fetch_md5"] += 1
        return "f" * 32

    def _head(url):
        calls["head"] += 1
        return None

    monkeypatch.setattr(pd, "fetch_md5", _md5)
    monkeypatch.setattr(pd, "head_last_modified", _head)
    return calls


def test_cache_if_present_skips_geofabrik(monkeypatch):
    monkeypatch.setenv("FW_OSM_USE_CACHE_IF_PRESENT", "1")
    calls = _patch_cached(monkeypatch, cached=True)

    res = pd.download_region("north-america/us/california", storage=_FakeStorage())

    assert res.was_cached is True
    assert res.size_bytes == 4242
    # The whole point: no Geofabrik round-trips for a cached region.
    assert calls["fetch_md5"] == 0
    assert calls["head"] == 0


def test_cache_if_present_but_not_cached_falls_through(monkeypatch):
    # Flag on but region NOT cached → must still revalidate (hits fetch_md5).
    monkeypatch.setenv("FW_OSM_USE_CACHE_IF_PRESENT", "1")
    calls = _patch_cached(monkeypatch, cached=False)
    # _stream_download would run next; stop after the md5 fetch to keep it offline.
    monkeypatch.setattr(pd, "_already_cached", lambda *a, **k: {"source": {}})
    monkeypatch.setattr(pd, "_region_lock", lambda region: __import__("contextlib").nullcontext())

    class _S(_FakeStorage):
        def mkdir_p(self, p): pass

    pd.download_region("north-america/us/california", storage=_S())
    assert calls["fetch_md5"] == 1  # revalidation happened


def test_flag_off_always_revalidates(monkeypatch):
    monkeypatch.delenv("FW_OSM_USE_CACHE_IF_PRESENT", raising=False)
    calls = _patch_cached(monkeypatch, cached=True)
    monkeypatch.setattr(pd, "_already_cached", lambda *a, **k: {"source": {}})
    monkeypatch.setattr(pd, "_region_lock", lambda region: __import__("contextlib").nullcontext())

    class _S(_FakeStorage):
        def mkdir_p(self, p): pass

    pd.download_region("north-america/us/california", storage=_S())
    assert calls["fetch_md5"] == 1  # flag off → cached fast-path NOT taken
