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
    pd._OSMFR_COVERAGE.clear()
    yield
    pd.EXTRACT_PROVIDER = saved
    pd._OSMFR_COVERAGE.clear()


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


# --- osmfr-with-Geofabrik-fallback: coverage-aware provider resolution --------
#
# OSM France carries a SUBSET of Geofabrik's regions (e.g. it splits the US into
# four macro-regions, not 50 states). `resolve_extract_url` probes coverage with
# a HEAD and falls back to Geofabrik where OSM France has no extract, so the
# baseline stays internally consistent (extract <-> embedded replication header
# <-> diffs, one clip polygon) and delta updates skip Geofabrik.

class _FakeHeadResp:
    """Minimal context-manager stand-in for a urlopen(HEAD) response."""
    def __init__(self, code):
        self._code = code
    def getcode(self):
        return self._code
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


def _mock_head(monkeypatch, *, code=None, raises=None):
    """Patch the HEAD probe used by `_osmfr_covers`."""
    def fake_urlopen(req, timeout=None):
        if raises is not None:
            raise raises
        return _FakeHeadResp(code)
    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)


def test_resolve_default_provider_is_geofabrik(restore_provider):
    pd.EXTRACT_PROVIDER = "geofabrik"
    url, provider = pd.resolve_extract_url("north-america/us/california")
    assert provider == "geofabrik"
    assert url == f"{pd.GEOFABRIK_BASE}/north-america/us/california-latest.osm.pbf"


def test_resolve_osmfr_covered_uses_osmfr(restore_provider, monkeypatch):
    pd.EXTRACT_PROVIDER = "osmfr"
    _mock_head(monkeypatch, code=200)
    url, provider = pd.resolve_extract_url("europe/germany")
    assert provider == "osmfr"
    assert url == f"{pd.OSMFR_BASE}/extracts/europe/germany-latest.osm.pbf"


def test_resolve_osmfr_uncovered_falls_back_to_geofabrik(restore_provider, monkeypatch):
    # A region OSM France lacks (404) must fall back to Geofabrik, not 404-fail.
    pd.EXTRACT_PROVIDER = "osmfr"
    _mock_head(monkeypatch, raises=pd.urllib.error.HTTPError(
        "u", 404, "Not Found", None, None))
    url, provider = pd.resolve_extract_url("north-america/us/california")
    assert provider == "geofabrik"
    assert url == f"{pd.GEOFABRIK_BASE}/north-america/us/california-latest.osm.pbf"


def test_resolve_osmfr_transient_error_prefers_osmfr(restore_provider, monkeypatch):
    # A transient failure (503/network) must NOT silently defect to Geofabrik.
    pd.EXTRACT_PROVIDER = "osmfr"
    _mock_head(monkeypatch, raises=pd.urllib.error.HTTPError(
        "u", 503, "Service Unavailable", None, None))
    url, provider = pd.resolve_extract_url("europe/france")
    assert provider == "osmfr"
    assert "openstreetmap.fr" in url


def test_osmfr_coverage_is_memoized(restore_provider, monkeypatch):
    pd.EXTRACT_PROVIDER = "osmfr"
    calls = {"n": 0}
    def counting_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeHeadResp(200)
    monkeypatch.setattr(pd.urllib.request, "urlopen", counting_urlopen)
    pd.resolve_extract_url("europe/germany")
    pd.resolve_extract_url("europe/germany")
    assert calls["n"] == 1  # second call served from the coverage memo


def test_geofabrik_fallback_url_still_fetches_md5(restore_provider, monkeypatch):
    # md5 fetch keys off the URL host, so a per-region Geofabrik fallback (under
    # provider=osmfr) still fetches Geofabrik's .md5 rather than skipping it.
    pd.EXTRACT_PROVIDER = "osmfr"
    monkeypatch.setattr(pd, "fetch_md5", lambda url: "a" * 32)
    got = pd.fetch_md5_or_none(f"{pd.GEOFABRIK_BASE}/north-america/us/california-latest.osm.pbf")
    assert got == "a" * 32
