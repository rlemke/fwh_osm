"""Offline tests for osm.cache.UpdateRegion (network seam + storage mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from osm_geocoder.handlers.cache import update_handlers as uh
from osm_geocoder.tools._osm_tools import pbf_update as pu


class _FakeStorage:
    def __init__(self, local):
        self.local = local
        self.finalized = []

    def localize(self, path):
        return self.local

    def finalize_from_local(self, local, dst):
        self.finalized.append((local, dst))


@pytest.fixture
def local_pbf(tmp_path):
    p = tmp_path / "germany-latest.osm.pbf"
    p.write_bytes(b"x" * 1000)
    return str(p)


@pytest.fixture
def storage(monkeypatch, local_pbf):
    st = _FakeStorage(local_pbf)
    monkeypatch.setattr(pu, "get_storage", lambda: st)
    monkeypatch.setattr(pu, "cached_path", lambda r, **k: f"s3://afl-cache/{r}-latest.osm.pbf")
    monkeypatch.setattr(pu, "region_to_paths",
                        lambda r: (f"{r}-latest.osm.pbf", f"https://download.geofabrik.de/{r}-latest.osm.pbf"))
    monkeypatch.setattr(pu, "is_region_cached", lambda r, **k: True)
    return st


REG = {"name": "Germany", "geofabrik_path": "europe/germany"}


def test_already_current_no_finalize(monkeypatch, storage):
    monkeypatch.setattr(pu, "_apply_geofabrik_diffs", lambda *a, **k: pu.AppliedDiffs("current", 0))
    r = pu.update_region("europe/germany", region=REG)
    assert r.method == "current" and r.applied_bytes == 0
    assert r.cache["wasInCache"] is True
    assert storage.finalized == []  # unchanged -> not rewritten


def test_diff_applied_finalizes(monkeypatch, storage):
    monkeypatch.setattr(pu, "_apply_geofabrik_diffs", lambda *a, **k: pu.AppliedDiffs("updated", 3_500_000))
    r = pu.update_region("europe/germany", region=REG)
    assert r.method == "diff" and r.applied_bytes == 3_500_000
    assert r.cache["wasInCache"] is False
    assert len(storage.finalized) == 1  # updated PBF written back to cache


@pytest.mark.parametrize("status", ["no_baseline", "stale"])
def test_falls_back_to_full(monkeypatch, storage, status):
    monkeypatch.setattr(pu, "_apply_geofabrik_diffs", lambda *a, **k: pu.AppliedDiffs(status, 0))
    monkeypatch.setattr(pu, "download_region", lambda r, **k: MagicMock(size=10_000_000))
    monkeypatch.setattr(pu, "to_osm_cache", lambda res, region=None: {"size": res.size, "wasInCache": False})
    r = pu.update_region("europe/germany", region=REG)
    assert r.method == "full" and r.applied_bytes == 10_000_000


def test_not_cached_full_download(monkeypatch, storage):
    monkeypatch.setattr(pu, "is_region_cached", lambda r, **k: False)
    monkeypatch.setattr(pu, "download_region", lambda r, **k: MagicMock(size=9))
    monkeypatch.setattr(pu, "to_osm_cache", lambda res, region=None: {"size": res.size})
    r = pu.update_region("antarctica", region=None)
    assert r.method == "full"


def test_handler_shape_and_validation(monkeypatch):
    monkeypatch.setattr(uh, "update_region",
                        lambda gp, **k: pu.UpdateResult("diff", 2_000_000, {"path": "p", "wasInCache": False}))
    out = uh.handle_update_region({"region": {"geofabrik_path": "europe/germany", "name": "Germany"}})
    assert out["method"] == "diff" and out["applied_mb"] == 2.0 and "cache" in out
    # missing geofabrik_path/canonical -> explicit error (never silent)
    with pytest.raises(ValueError):
        uh.handle_update_region({"region": {}})


def test_handler_dispatch_and_timeout0(monkeypatch):
    monkeypatch.setattr(uh, "update_region",
                        lambda gp, **k: pu.UpdateResult("current", 0, {"wasInCache": True}))
    assert uh.handle({"_facet_name": "osm.cache.UpdateRegion",
                      "region": {"canonical": "europe/germany"}})["method"] == "current"
    runner = MagicMock()
    uh.register_handlers(runner)
    assert runner.register_handler.call_args.kwargs["timeout_ms"] == 0
