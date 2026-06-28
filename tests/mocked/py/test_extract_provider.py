"""Unit tests for the alternative extract provider (FW_OSM_EXTRACT_PROVIDER).

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
    assert pd._osmfr_region("australia-oceania/fiji") == "oceania/fiji"
    assert pd._osmfr_region("europe/france") == "europe/france"


def test_osmfr_hyphen_to_underscore(restore_provider):
    # OSM France uses underscores in multi-word country/sub names (Geofabrik hyphens).
    assert pd._osmfr_region("africa/burkina-faso") == "africa/burkina_faso"
    assert pd._osmfr_region("africa/south-africa") == "africa/south_africa"
    # top-level remap + underscore together
    assert pd._osmfr_region("australia-oceania/papua-new-guinea") == "oceania/papua_new_guinea"
    # single-word names + the continent segment are unaffected (no false rewrites)
    assert pd._osmfr_region("europe/germany") == "europe/germany"
    assert pd._osmfr_region("central-america/belize") == "central-america/belize"
    pd.EXTRACT_PROVIDER = "osmfr"
    _, url = pd.region_to_paths("africa/burkina-faso")
    assert url == f"{pd.OSMFR_BASE}/extracts/africa/burkina_faso-latest.osm.pbf"


def test_osmfr_skips_md5(restore_provider):
    pd.EXTRACT_PROVIDER = "osmfr"
    # OSM France publishes no .md5 -> short-circuit to None without a request
    assert pd.fetch_md5_or_none("https://download.openstreetmap.fr/extracts/x.osm.pbf") is None
