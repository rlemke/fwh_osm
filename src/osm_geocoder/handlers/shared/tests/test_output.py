"""Tests for resolve_output_dir / _osm_output_base output resolution."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from osm_geocoder.handlers.shared import _output


@pytest.fixture(autouse=True)
def _clear_explicit_base(monkeypatch):
    """Default: no explicit AFL_OSM_OUTPUT_BASE (the module-level cache too)."""
    monkeypatch.setattr(_output, "_OUTPUT_BASE", "")
    monkeypatch.delenv("AFL_OSM_OUTPUT_BASE", raising=False)


def test_explicit_base_wins(monkeypatch):
    monkeypatch.setattr(_output, "_OUTPUT_BASE", "s3://custom/prefix")
    monkeypatch.setenv("AFL_DATA_ROOT", "s3://afl-cache")
    assert _output.resolve_output_dir("osm-combined") == "s3://custom/prefix/osm-combined"


def test_s3_data_root_defaults_to_object_store(monkeypatch):
    """The fix: a runner with only AFL_STORAGE=s3 + AFL_DATA_ROOT (no
    AFL_OSM_OUTPUT_BASE) still writes osm output to the object store, not a
    host-local path — so any runner that wins an osm claim is readable
    downstream."""
    monkeypatch.setenv("AFL_DATA_ROOT", "s3://afl-cache")
    assert _output.resolve_output_dir("osm-combined") == "s3://afl-cache/osm-output/osm-combined"


def test_hdfs_data_root_defaults_to_object_store(monkeypatch):
    monkeypatch.setenv("AFL_DATA_ROOT", "hdfs://afl-hadoop-hdfs:8020")
    assert _output.resolve_output_dir("pbf") == "hdfs://afl-hadoop-hdfs:8020/osm-output/pbf"


def test_trailing_slash_normalized(monkeypatch):
    monkeypatch.setenv("AFL_DATA_ROOT", "s3://afl-cache/")
    assert _output.resolve_output_dir("x") == "s3://afl-cache/osm-output/x"


def test_local_only_falls_back_to_host_path(monkeypatch):
    """No remote store configured → host-local path (unchanged behavior)."""
    monkeypatch.delenv("AFL_DATA_ROOT", raising=False)
    monkeypatch.setenv("AFL_OUTPUT_BASE", "/tmp/local")
    got = _output.resolve_output_dir("osm-combined")
    assert got.endswith("/osm/osm-combined")
    assert not got.startswith(("s3://", "hdfs://"))


def test_local_data_root_is_not_remote(monkeypatch):
    """A plain local AFL_DATA_ROOT (e.g. /Volumes/afl_data) is NOT a remote
    store, so output still derives from the local AFL_OUTPUT_BASE."""
    monkeypatch.setenv("AFL_DATA_ROOT", "/Volumes/afl_data")
    monkeypatch.setenv("AFL_OUTPUT_BASE", "/tmp/local")
    got = _output.resolve_output_dir("osm-combined")
    assert not got.startswith(("s3://", "hdfs://"))
