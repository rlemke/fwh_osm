"""``region_from_cache``: the region a cache-consuming handler builds against.

Regression for the 2026-09-22 California GraphHopper build: with the fleet's
extract base pointed at its own mirror, the Geofabrik-only URL regex fell
through, the whole URL became the "region" and then an S3 key, and MinIO
answered HeadObject 400 until the task dead-lettered.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from osm_geocoder.handlers.shared.cache_region import (  # noqa: E402
    region_from_cache,
    region_from_url,
)

MIRROR = "http://afl-minio:9000/osm-extracts"
CA = "north-america/us/california"


def test_typed_region_wins_over_url(monkeypatch):
    monkeypatch.setenv("FW_GEOFABRIK_BASE_URL", MIRROR)
    cache = {"url": f"{MIRROR}/{CA}-latest.osm.pbf", "region": {"canonical": CA}}
    assert region_from_cache(cache) == CA


def test_self_hosted_mirror_url_parses_when_record_has_no_region(monkeypatch):
    monkeypatch.setenv("FW_GEOFABRIK_BASE_URL", MIRROR)
    assert region_from_cache({"url": f"{MIRROR}/{CA}-latest.osm.pbf"}) == CA


def test_geofabrik_url_still_parses_under_a_mirror_base(monkeypatch):
    monkeypatch.setenv("FW_GEOFABRIK_BASE_URL", MIRROR)
    url = "https://download.geofabrik.de/africa/algeria-latest.osm.pbf"
    assert region_from_url(url) == "africa/algeria"


def test_default_base_is_geofabrik(monkeypatch):
    monkeypatch.delenv("FW_GEOFABRIK_BASE_URL", raising=False)
    assert region_from_url("https://download.geofabrik.de/europe/monaco-latest.osm.pbf") == "europe/monaco"


def test_geofabrik_path_is_accepted_when_canonical_absent():
    assert region_from_cache({"region": {"geofabrik_path": "europe/monaco"}, "url": ""}) == "europe/monaco"


@pytest.mark.parametrize("cache", [None, {}, {"url": ""}, {"region": {}, "url": ""}])
def test_nothing_to_go_on_returns_empty(cache):
    assert region_from_cache(cache) == ""


def test_unrecognised_url_is_returned_verbatim_so_the_library_can_name_it(monkeypatch):
    monkeypatch.setenv("FW_GEOFABRIK_BASE_URL", MIRROR)
    odd = "https://example.org/somewhere/file.pbf"
    assert region_from_cache({"url": odd}) == odd
