"""HDFS integration tests for OSM handler storage patterns.

Verifies that OSM handlers can use HDFS for caching via the StorageBackend
abstraction and WebHDFS REST API.

Requires the HDFS Docker services to be running:

    docker compose --profile hdfs up -d

Run with:

    pytest examples/osm-geocoder/tests/real/py/test_osm_handlers_hdfs.py --hdfs -v
"""

import pytest

from facetwork.runtime.storage import (
    HDFSStorageBackend,
    LocalStorageBackend,
    get_storage_backend,
)
from tests.hdfs_helpers import WebHDFSClient, hdfs, workdir  # noqa: F401, F811

# Skip entire module unless --hdfs is passed
pytestmark = pytest.mark.skipif("not config.getoption('--hdfs')")


# ---------------------------------------------------------------------------
# StorageBackend selection with hdfs:// URIs
# ---------------------------------------------------------------------------


class TestStorageBackendHDFSSelection:
    """Verify get_storage_backend() dispatches correctly for HDFS URIs."""

    def setup_method(self):
        """Reset backend caches between tests."""
        import afl.runtime.storage as _mod

        _mod._hdfs_backends.clear()
        _mod._local_backend = None

    def test_local_path_returns_local_backend(self):
        backend = get_storage_backend("/tmp/osm-cache")
        assert isinstance(backend, LocalStorageBackend)

    def test_none_path_returns_local_backend(self):
        backend = get_storage_backend(None)
        assert isinstance(backend, LocalStorageBackend)

    def test_hdfs_uri_returns_hdfs_backend(self):
        try:
            backend = get_storage_backend("hdfs://namenode:8020/osm-cache")
            assert isinstance(backend, HDFSStorageBackend)
        except RuntimeError:
            pytest.skip("pyarrow not installed")

    def test_hdfs_backend_caching(self):
        """Same host:port returns the same backend instance."""
        try:
            b1 = get_storage_backend("hdfs://namenode:8020/osm-cache")
            b2 = get_storage_backend("hdfs://namenode:8020/other-path")
            assert b1 is b2
        except RuntimeError:
            pytest.skip("pyarrow not installed")


# ---------------------------------------------------------------------------
# WebHDFS cache file operations
# ---------------------------------------------------------------------------


class TestWebHDFSCacheOperations:
    """Test cache-related file operations against a live HDFS cluster."""

    def test_create_cache_file(self, hdfs, workdir):
        """Write a cache file and verify it exists."""
        path = f"{workdir}/cache/region.osm.pbf"
        hdfs.mkdirs(f"{workdir}/cache")
        hdfs.create(path, b"PBF-DATA-PLACEHOLDER")
        assert hdfs.exists(path) is True
        assert hdfs.isfile(path) is True

    def test_cache_file_size(self, hdfs, workdir):
        """Verify getsize on cached files."""
        path = f"{workdir}/sized-cache.bin"
        payload = b"x" * 4096
        hdfs.create(path, payload)
        assert hdfs.getsize(path) == 4096

    def test_cache_directory_listing(self, hdfs, workdir):
        """List files in a cache directory."""
        cache_dir = f"{workdir}/osm-cache"
        hdfs.mkdirs(cache_dir)
        for name in ("europe.osm.pbf", "asia.osm.pbf"):
            hdfs.create(f"{cache_dir}/{name}", b"data")
        entries = hdfs.listdir(cache_dir)
        names = sorted(e["pathSuffix"] for e in entries)
        assert names == ["asia.osm.pbf", "europe.osm.pbf"]

    def test_overwrite_cache_file(self, hdfs, workdir):
        """Overwrite a cached file with new data."""
        path = f"{workdir}/overwrite.osm.pbf"
        hdfs.create(path, b"version-1")
        hdfs.create(path, b"version-2")
        assert hdfs.read(path) == b"version-2"

    def test_cache_isdir(self, hdfs, workdir):
        """Distinguish files from directories in the cache."""
        cache_dir = f"{workdir}/cache-dir"
        hdfs.mkdirs(cache_dir)
        hdfs.create(f"{cache_dir}/file.txt", b"x")
        assert hdfs.isdir(cache_dir) is True
        assert hdfs.isdir(f"{cache_dir}/file.txt") is False
        assert hdfs.isfile(cache_dir) is False
        assert hdfs.isfile(f"{cache_dir}/file.txt") is True


# ---------------------------------------------------------------------------
# HDFS cache patterns used by OSM handlers
# ---------------------------------------------------------------------------


class TestHDFSCachePatterns:
    """Verify the nested directory cache patterns used by OSM handlers."""

    def test_osm_pbf_cache_pattern(self, hdfs, workdir):
        """Simulate the downloader's cache structure: region/file.osm.pbf."""
        cache_root = f"{workdir}/osm-cache"
        region_dir = f"{cache_root}/europe/germany/berlin"
        hdfs.mkdirs(region_dir)

        pbf_path = f"{region_dir}/berlin-latest.osm.pbf"
        pbf_data = b"\x00\x01\x02PBF-HEADER" + b"\xff" * 1024
        hdfs.create(pbf_path, pbf_data)

        assert hdfs.exists(pbf_path) is True
        assert hdfs.read(pbf_path) == pbf_data
        s = hdfs.status(pbf_path)
        assert s["length"] == len(pbf_data)

    def test_graphhopper_graph_cache_pattern(self, hdfs, workdir):
        """Simulate the GraphHopper graph directory structure."""
        graph_root = f"{workdir}/graphhopper"
        graph_dir = f"{graph_root}/berlin-latest/car"
        hdfs.mkdirs(graph_dir)

        # GraphHopper stores multiple files per graph
        for name in ("nodes", "edges", "geometry", "properties"):
            hdfs.create(f"{graph_dir}/{name}", f"{name}-data".encode())

        entries = hdfs.listdir(graph_dir)
        names = sorted(e["pathSuffix"] for e in entries)
        assert names == ["edges", "geometry", "nodes", "properties"]

    def test_gtfs_cache_pattern(self, hdfs, workdir):
        """Simulate the GTFS extractor cache structure."""
        gtfs_root = f"{workdir}/gtfs-cache"
        feed_dir = f"{gtfs_root}/vbb-berlin"
        hdfs.mkdirs(feed_dir)

        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            hdfs.create(f"{feed_dir}/{name}", f"header\n{name}-data".encode())

        entries = hdfs.listdir(feed_dir)
        names = sorted(e["pathSuffix"] for e in entries)
        assert names == ["routes.txt", "stop_times.txt", "stops.txt", "trips.txt"]

        # Verify round-trip
        data = hdfs.read(f"{feed_dir}/stops.txt")
        assert data == b"header\nstops.txt-data"
