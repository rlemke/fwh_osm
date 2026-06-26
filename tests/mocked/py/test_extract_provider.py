"""Unit tests for the alternative extract provider (AFL_OSM_EXTRACT_PROVIDER).

`osmfr` routes downloads to OpenStreetMap France (download.openstreetmap.fr) for
when Geofabrik rate-limits/bans the egress IP. The cache layout stays
provider-agnostic; only the remote URL changes, OSM France's missing .md5 is
tolerated, and a small top-level region remap (australia-oceania -> oceania)
matches OSM France's tree. No network — pure URL construction.
"""

import pytest

pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_download")


@pytest.fixture
def restore_provider():
    saved = pd.EXTRACT_PROVIDER
    yield
    pd.EXTRACT_PROVIDER = saved


def test_geofabrik_is_default(restore_provider):
    pd.EXTRACT_PROVIDER = "geofabrik"
    rel, url = pd.region_to_paths("europe/germany")
    assert rel == "europe/germany-latest.osm.pbf"
    assert url == f"{pd.GEOFABRIK_BASE}/europe/germany-latest.osm.pbf"


def test_osmfr_url_and_provider_agnostic_cache_path(restore_provider):
    pd.EXTRACT_PROVIDER = "osmfr"
    rel, url = pd.region_to_paths("europe/germany")
    # cache path is unchanged (Geofabrik-style) so downstream tools don't care
    assert rel == "europe/germany-latest.osm.pbf"
    assert url == f"{pd.OSMFR_BASE}/extracts/europe/germany-latest.osm.pbf"


def test_osmfr_toplevel_remap(restore_provider):
    pd.EXTRACT_PROVIDER = "osmfr"
    _, url = pd.region_to_paths("australia-oceania/fiji")
    assert url == f"{pd.OSMFR_BASE}/extracts/oceania/fiji-latest.osm.pbf"
    assert pd._osmfr_region("australia-oceania") == "oceania"
    assert pd._osmfr_region("europe/france") == "europe/france"


def test_osmfr_skips_md5(restore_provider):
    pd.EXTRACT_PROVIDER = "osmfr"
    # OSM France publishes no .md5 -> short-circuit to None without a request
    assert pd.fetch_md5_or_none("https://download.openstreetmap.fr/extracts/x.osm.pbf") is None
