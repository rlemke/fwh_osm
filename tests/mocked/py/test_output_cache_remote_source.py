"""A remote source's cache key must change when the object changes.

``os.stat`` cannot see ``s3://``, and the old ``(0, 0)`` fallback keyed a
remote source by its PATH alone: once zoom layers moved to the shared store
(2026-10-13), a rebuild writing the same keys would have been answered with
the previous build's tiles.
"""

from __future__ import annotations

import io
import json

import facetwork.runtime.storage as st
import pytest

from osm_geocoder.handlers.shared import output_cache as C


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.mtime: dict[str, float] = {}
        self.fail = False

    def exists(self, path):
        if self.fail and path.startswith("s3://"):
            raise ConnectionError("store unreachable")
        return path in self.objects

    def getsize(self, path):
        return len(self.objects[path])

    def getmtime(self, path):
        return self.mtime[path]

    def makedirs(self, path, exist_ok=True):
        pass

    def open(self, path, mode="r"):
        if "r" in mode:
            data = self.objects[path]
            return io.StringIO(data.decode()) if "b" not in mode else io.BytesIO(data)
        buf = io.StringIO() if "b" not in mode else io.BytesIO()
        store = self

        class _W:
            def __enter__(self):
                return buf

            def __exit__(self, *exc):
                v = buf.getvalue()
                store.objects[path] = v.encode() if isinstance(v, str) else v

        return _W()


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = _Store()
    monkeypatch.setattr(st, "get_storage_backend", lambda p=None: s)
    monkeypatch.setattr(C, "get_storage_backend", lambda p=None: s, raising=False)
    monkeypatch.setenv("FW_OSM_OUTPUT_BASE", "mem://meta")
    return s


SRC = "s3://b/osm-output/lz-country/us-z2-z4/roads_z2.geojson"


def _save(store, tiles: str) -> None:
    store.objects[tiles] = b"pmtiles"
    C.save_result_meta(
        "osm.Tiles.BuildVectorTiles", {"path": SRC}, {"z": 2}, {"result": {"output_path": tiles}}
    )


def test_same_object_hits(store):
    store.objects[SRC], store.mtime[SRC] = b"build-1", 1000.0
    _save(store, "s3://b/tiles/a.pmtiles")
    hit = C.cached_result("osm.Tiles.BuildVectorTiles", {"path": SRC}, {"z": 2})
    assert hit == {"result": {"output_path": "s3://b/tiles/a.pmtiles"}}


def test_a_rebuilt_object_at_the_same_path_misses(store):
    store.objects[SRC], store.mtime[SRC] = b"build-1", 1000.0
    _save(store, "s3://b/tiles/a.pmtiles")
    store.objects[SRC], store.mtime[SRC] = b"build-2", 2000.0
    assert C.cached_result("osm.Tiles.BuildVectorTiles", {"path": SRC}, {"z": 2}) is None


def test_an_unreadable_remote_source_skips_the_cache(store):
    store.objects[SRC], store.mtime[SRC] = b"build-1", 1000.0
    _save(store, "s3://b/tiles/a.pmtiles")
    store.fail = True
    assert C.cached_result("osm.Tiles.BuildVectorTiles", {"path": SRC}, {"z": 2}) is None
    n = len(store.objects)
    C.save_result_meta(
        "osm.Tiles.BuildVectorTiles", {"path": SRC}, {"z": 2}, {"result": {"output_path": "x"}}
    )
    assert len(store.objects) == n, "no meta written under a path-only key"


def test_stat_remote_reads_the_store(store):
    store.objects[SRC], store.mtime[SRC] = b"12345", 1.5
    assert C._stat_source(SRC) == (5, 1_500_000_000)
    assert C._stat_source("s3://b/absent") == (0, 0)
    assert json.dumps(C._stat_source("/no/such/local/file")) == "[0, 0]"
