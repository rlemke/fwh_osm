"""Offline guard must block third parties and permit the self-hosted mirror."""
import importlib, os, pytest

def _load(monkeypatch, base=None, provider=None, offline="1"):
    for k, v in (("FW_GEOFABRIK_BASE_URL", base), ("FW_OSM_EXTRACT_PROVIDER", provider),
                 ("FW_OSM_OFFLINE", offline)):
        if v is None: monkeypatch.delenv(k, raising=False)
        else: monkeypatch.setenv(k, v)
    import osm_geocoder.tools._osm_tools.pbf_download as m
    return importlib.reload(m)

@pytest.mark.parametrize("base,expect_third_party", [
    ("https://download.geofabrik.de", True),
    ("http://afl-minio:9000/osm-extracts", False),
    ("http://server3.local:8088", False),
    ("https://download.openstreetmap.fr", True),
])
def test_base_classification(monkeypatch, base, expect_third_party):
    m = _load(monkeypatch, base=base)
    assert m._base_is_third_party() is expect_third_party, base

def test_osmfr_provider_is_third_party_whatever_the_base(monkeypatch):
    m = _load(monkeypatch, base="http://afl-minio:9000/osm-extracts", provider="osmfr")
    assert m._base_is_third_party() is True

def test_offline_flag_itself_still_read(monkeypatch):
    m = _load(monkeypatch, base="http://afl-minio:9000/osm-extracts", offline="1")
    assert m._offline_mode() is True
    m = _load(monkeypatch, base="http://afl-minio:9000/osm-extracts", offline="")
    assert m._offline_mode() is False


# ---------------------------------------------------------------------------
# Behaviour: what a cache MISS does under offline mode
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    """Raised in place of a real fetch, to prove the guard let us through."""


def _miss(monkeypatch, m):
    """Force a cache miss and stub the fetch so we can observe reaching it."""
    monkeypatch.setattr(m, "is_region_cached", lambda *a, **k: False)
    monkeypatch.setattr(m, "_region_lock", lambda region: __import__("contextlib").nullcontext())

    def _boom(region):
        raise _Sentinel("reached the fetch path")

    monkeypatch.setattr(m, "resolve_extract_url", _boom)

    from osm_geocoder.tools._osm_tools.storage import LocalStorage
    return LocalStorage()


def test_offline_permits_a_miss_against_the_self_hosted_mirror(monkeypatch):
    """The regression: a region absent from cache but present on OUR mirror.

    This is the shape that dead-lettered for 15 days — australia-oceania sat in
    our own object store while the guard refused to fetch it, because the guard
    could not tell our mirror from Geofabrik.
    """
    m = _load(monkeypatch, base="http://afl-minio:9000/osm-extracts", offline="1")
    storage = _miss(monkeypatch, m)
    with pytest.raises(_Sentinel):          # got past the guard, tried to fetch
        m.download_region("australia-oceania/australia", storage=storage)


def test_offline_still_refuses_a_miss_against_a_third_party(monkeypatch):
    """The guard's real job is intact: no egress to the host that banned us."""
    m = _load(monkeypatch, base="https://download.geofabrik.de", offline="1")
    storage = _miss(monkeypatch, m)
    with pytest.raises(m.DownloadError, match="THIRD PARTY"):
        m.download_region("australia-oceania/australia", storage=storage)


def test_offline_off_permits_a_miss_anywhere(monkeypatch):
    m = _load(monkeypatch, base="https://download.geofabrik.de", offline="")
    storage = _miss(monkeypatch, m)
    with pytest.raises(_Sentinel):
        m.download_region("australia-oceania/australia", storage=storage)
