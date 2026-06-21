"""Tests for the output_cache module."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from osm_geocoder.handlers.shared.output_cache import (
    _version_key,
    cached_result,
    save_result_meta,
    with_output_cache,
)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Set AFL_LOCAL_OUTPUT_DIR to a temp directory."""
    monkeypatch.setenv("AFL_LOCAL_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("AFL_OSM_OUTPUT_BASE", raising=False)
    return tmp_path


class TestVersionKey:
    def test_deterministic(self):
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {"level": 4})
        k2 = _version_key("handler.A", "/data/file.pbf", 1000, {"level": 4})
        assert k1 == k2

    def test_differs_on_handler(self):
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {})
        k2 = _version_key("handler.B", "/data/file.pbf", 1000, {})
        assert k1 != k2

    def test_differs_on_size(self):
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {})
        k2 = _version_key("handler.A", "/data/file.pbf", 2000, {})
        assert k1 != k2

    def test_differs_on_params(self):
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {"level": 4})
        k2 = _version_key("handler.A", "/data/file.pbf", 1000, {"level": 8})
        assert k1 != k2

    def test_differs_on_path(self):
        k1 = _version_key("handler.A", "/data/a.pbf", 1000, {})
        k2 = _version_key("handler.A", "/data/b.pbf", 1000, {})
        assert k1 != k2

    def test_differs_on_mtime_same_size(self):
        # Same-size daily rebuild: identical byte count, different mtime ->
        # the key MUST change so we don't hit a stale cache entry.
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {}, 111)
        k2 = _version_key("handler.A", "/data/file.pbf", 1000, {}, 222)
        assert k1 != k2

    def test_stable_on_identical_mtime(self):
        # Idempotent: identical inputs (incl. mtime) -> identical key.
        k1 = _version_key("handler.A", "/data/file.pbf", 1000, {}, 111)
        k2 = _version_key("handler.A", "/data/file.pbf", 1000, {}, 111)
        assert k1 == k2

    def test_mtime_default_backcompat(self):
        # Omitting mtime defaults to 0 (stable, no crash).
        assert _version_key("handler.A", "/data/file.pbf", 1000, {}) == _version_key(
            "handler.A", "/data/file.pbf", 1000, {}, 0
        )


class TestCachedResult:
    def test_miss_no_meta(self, cache_dir):
        cache = {"path": "/data/file.pbf", "size": 1000}
        assert cached_result("handler.A", cache, {}) is None

    def test_miss_empty_path(self, cache_dir):
        cache = {"path": "", "size": 0}
        assert cached_result("handler.A", cache, {}) is None

    def test_hit_after_save(self, cache_dir):
        cache = {"path": "/data/file.pbf", "size": 1000}
        params = {"level": 4}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache, params, result)
        hit = cached_result("handler.A", cache, params)
        assert hit is not None
        assert hit == result

    def test_miss_different_params(self, cache_dir):
        cache = {"path": "/data/file.pbf", "size": 1000}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache, {"level": 4}, result)
        hit = cached_result("handler.A", cache, {"level": 8})
        assert hit is None

    def test_miss_different_size(self, cache_dir):
        cache1 = {"path": "/data/file.pbf", "size": 1000}
        cache2 = {"path": "/data/file.pbf", "size": 2000}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache1, {}, result)
        hit = cached_result("handler.A", cache2, {})
        assert hit is None

    def test_miss_output_file_deleted(self, cache_dir, tmp_path):
        output = str(tmp_path / "output.geojson")
        with open(output, "w") as f:
            f.write("{}")

        cache = {"path": "/data/file.pbf", "size": 1000}
        result = {"result": {"output_path": output, "feature_count": 5}}

        save_result_meta("handler.A", cache, {}, result)

        # Verify hit while file exists
        assert cached_result("handler.A", cache, {}) is not None

        # Delete output file
        os.remove(output)
        assert cached_result("handler.A", cache, {}) is None

    def test_miss_same_size_different_mtime(self, cache_dir, tmp_path):
        # Real on-disk source: a same-size rebuild (identical byte count,
        # newer mtime) must invalidate the cache via the folded mtime.
        pbf = tmp_path / "region.pbf"
        pbf.write_bytes(b"AAAA")  # 4 bytes
        cache = {"path": str(pbf), "size": 4}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache, {}, result)
        assert cached_result("handler.A", cache, {}) is not None

        # Overwrite with same-size but different content + bump mtime.
        pbf.write_bytes(b"BBBB")  # still 4 bytes
        future = pbf.stat().st_mtime_ns + 1_000_000_000
        os.utime(str(pbf), ns=(future, future))

        assert cached_result("handler.A", cache, {}) is None

    def test_hit_identical_file(self, cache_dir, tmp_path):
        # Idempotent: unchanged on-disk source -> stable hit.
        pbf = tmp_path / "region.pbf"
        pbf.write_bytes(b"AAAA")
        cache = {"path": str(pbf), "size": 4}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache, {}, result)
        assert cached_result("handler.A", cache, {}) == result
        # Re-checking without touching the file still hits.
        assert cached_result("handler.A", cache, {}) == result

    def test_missing_source_file_does_not_crash(self, cache_dir):
        # Absent source path -> graceful fallback (no crash); behaves like
        # the original size-only key.
        cache = {"path": "/data/does-not-exist.pbf", "size": 1000}
        result = {"result": {"output_path": "", "feature_count": 5}}
        save_result_meta("handler.A", cache, {}, result)
        assert cached_result("handler.A", cache, {}) == result

    def test_step_log_on_hit(self, cache_dir):
        cache = {"path": "/data/file.pbf", "size": 1000}
        result = {"result": {"output_path": "", "feature_count": 5}}

        save_result_meta("handler.A", cache, {}, result)

        logs = []
        hit = cached_result("handler.A", cache, {}, step_log=lambda msg, **kw: logs.append(msg))
        assert hit is not None
        assert len(logs) == 1
        assert "cache hit" in logs[0]


class TestWithOutputCache:
    def test_wraps_handler(self, cache_dir):
        call_count = 0

        def handler(payload):
            nonlocal call_count
            call_count += 1
            return {"result": {"output_path": "", "feature_count": 3}}

        wrapped = with_output_cache(handler, "test.Handler", {"type": "a"})

        cache = {"path": "/data/file.pbf", "size": 1000}
        r1 = wrapped({"cache": cache})
        assert r1["result"]["feature_count"] == 3
        assert call_count == 1

        # Second call should hit cache
        r2 = wrapped({"cache": cache})
        assert r2 == r1
        assert call_count == 1  # handler not called again

    def test_cache_miss_calls_handler(self, cache_dir):
        call_count = 0

        def handler(payload):
            nonlocal call_count
            call_count += 1
            return {"result": {"output_path": "", "feature_count": call_count}}

        wrapped = with_output_cache(handler, "test.Handler", {"type": "a"})

        # Different PBF sizes -> different cache keys
        r1 = wrapped({"cache": {"path": "/data/a.pbf", "size": 100}})
        r2 = wrapped({"cache": {"path": "/data/b.pbf", "size": 200}})
        assert call_count == 2
        assert r1["result"]["feature_count"] == 1
        assert r2["result"]["feature_count"] == 2
