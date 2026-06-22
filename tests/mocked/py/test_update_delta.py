"""Tests for the delta-update path + the throttled update-delta CLI.

Covers the safety contract that matters for rate-limiting: ``diff_only=True``
NEVER falls back to a full PBF re-download (the heavy path that can trip
Geofabrik), while the default keeps the full-download fallback.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from osm_geocoder.tools._osm_tools import pbf_update as U

_TOOLS = Path(U.__file__).resolve().parents[1]  # .../osm_geocoder/tools


class _Storage:
    def localize(self, p):
        return "/tmp/local.pbf"

    def finalize_from_local(self, a, b):
        pass


def _no_download():
    return patch.object(U, "download_region",
                        side_effect=AssertionError("diff_only must NOT re-download"))


def test_diff_only_uncached_is_noop():
    with patch.object(U, "is_region_cached", return_value=False), \
         patch.object(U, "get_storage", return_value=_Storage()), _no_download():
        r = U.update_region("europe/germany", diff_only=True)
    assert r.method == "uncached" and r.applied_bytes == 0


def test_diff_only_stale_is_noop():
    with patch.object(U, "is_region_cached", return_value=True), \
         patch.object(U, "get_storage", return_value=_Storage()), \
         patch.object(U, "_apply_geofabrik_diffs", return_value=U.AppliedDiffs("stale", 0)), \
         patch.object(U, "_osm_cache_dict", return_value={}), _no_download():
        r = U.update_region("europe/germany", diff_only=True)
    assert r.method == "stale" and r.applied_bytes == 0


def test_diff_only_still_applies_real_diffs():
    with patch.object(U, "is_region_cached", return_value=True), \
         patch.object(U, "get_storage", return_value=_Storage()), \
         patch.object(U, "_apply_geofabrik_diffs", return_value=U.AppliedDiffs("updated", 1234)), \
         patch.object(U, "_refresh_sidecar"), \
         patch.object(U, "_osm_cache_dict", return_value={}), _no_download():
        r = U.update_region("europe/germany", diff_only=True)
    assert r.method == "diff" and r.applied_bytes == 1234


def test_default_keeps_full_fallback():
    # diff_only=False (default): an uncached region DOES full-download.
    class _Res:
        size = 999
    with patch.object(U, "is_region_cached", return_value=False), \
         patch.object(U, "get_storage", return_value=_Storage()), \
         patch.object(U, "download_region", return_value=_Res()) as dl, \
         patch.object(U, "to_osm_cache", return_value={}):
        r = U.update_region("europe/germany")
    assert r.method == "full" and dl.called


# --- the CLI ---------------------------------------------------------------

def _load_cli():
    loader = importlib.util.spec_from_file_location("update_delta", _TOOLS / "update_delta.py")
    mod = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(mod)
    return mod


def test_cli_reads_regions_file(tmp_path):
    cli = _load_cli()
    f = tmp_path / "regions.txt"
    f.write_text("# comment\neurope/germany\n\nnorth-america/us/california\n")
    assert cli._read_regions_file(f) == ["europe/germany", "north-america/us/california"]


def test_cli_dry_run_dedups(monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setattr("sys.argv",
                        ["update_delta", "europe/germany", "europe/germany",
                         "north-america/us/california", "--dry-run"])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("europe/germany") == 1            # de-duped
    assert "north-america/us/california" in out


def test_cli_requires_regions(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr("sys.argv", ["update_delta"])
    with pytest.raises(SystemExit):                    # argparse error → exit 2
        cli.main()
