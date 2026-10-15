"""Offline guard must block third parties and permit the self-hosted mirror."""
import importlib, os, pytest

@pytest.fixture(autouse=True)
def _restore_module(monkeypatch):
    """Each test reloads pbf_download under its own env; reload it again under
    the restored env afterwards, or the last test's base leaks into every
    later test module (its constants are read at import)."""
    yield
    monkeypatch.undo()
    import osm_geocoder.tools._osm_tools.pbf_download as m
    importlib.reload(m)


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
    ("http://afl-extracts:8088", False),       # the self-hosted mirror, by service alias
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


# ---------------------------------------------------------------------------
# Behaviour: what a cache HIT does. 2026-10-14: the US major-roads extract was
# rebuilt on our own store with car-ferry access, and CacheRegion kept handing
# out the old cached PBF -- offline mode and FW_OSM_USE_CACHE_IF_PRESENT both
# pinned it without asking our own server whether it had changed.
# ---------------------------------------------------------------------------

SELF = "http://afl-minio:9000/osm-extracts"


def _hit(monkeypatch, m, not_modified=None):
    """A cached region. Returns (storage, calls): calls records whether the
    source was consulted. With ``not_modified`` set, revalidation answers it."""
    calls = []
    monkeypatch.setattr(m, "is_region_cached", lambda *a, **k: True)
    monkeypatch.setattr(m, "sidecar_entry_for", lambda *a, **k: {
        "size_bytes": 3, "sha256": "x",
        "source": {"url": "u", "source_timestamp": "2026-10-14T18:06:18Z"}})
    monkeypatch.setattr(m, "_region_lock", lambda region: __import__("contextlib").nullcontext())

    def resolve(region):
        calls.append(region)
        if not_modified is None:
            raise _Sentinel("consulted the source")
        return f"{SELF}/{region}-latest.osm.pbf", "geofabrik"

    monkeypatch.setattr(m, "resolve_extract_url", resolve)
    monkeypatch.setattr(m, "fetch_md5_or_none", lambda url: None)
    monkeypatch.setattr(m, "head_last_modified", lambda url: None)
    monkeypatch.setattr(m, "revalidate_not_modified", lambda url, lm: not_modified)
    from osm_geocoder.tools._osm_tools.storage import LocalStorage

    class _Sized(LocalStorage):
        def size(self, path):  # the cached file itself is not needed here
            return 3

    return _Sized(), calls


@pytest.mark.parametrize("offline,pin_env", [("1", None), ("", "true"), ("1", "true")])
def test_a_hit_on_our_own_store_is_revalidated(monkeypatch, offline, pin_env):
    if pin_env is None:
        monkeypatch.delenv("FW_OSM_USE_CACHE_IF_PRESENT", raising=False)
    else:
        monkeypatch.setenv("FW_OSM_USE_CACHE_IF_PRESENT", pin_env)
    m = _load(monkeypatch, base=SELF, offline=offline)
    storage, _calls = _hit(monkeypatch, m)
    with pytest.raises(_Sentinel):
        m.download_region("north-america/us-major-roads", storage=storage)


def test_an_unchanged_source_keeps_the_cache(monkeypatch):
    m = _load(monkeypatch, base=SELF, offline="1")
    storage, calls = _hit(monkeypatch, m, not_modified=True)
    r = m.download_region("north-america/us-major-roads", storage=storage)
    assert r.was_cached and calls == ["north-america/us-major-roads"]


def test_a_hit_against_a_third_party_stays_pinned_offline(monkeypatch):
    m = _load(monkeypatch, base="https://download.geofabrik.de", offline="1")
    storage, calls = _hit(monkeypatch, m)
    assert m.download_region("europe/monaco", storage=storage).was_cached
    assert calls == []


def test_a_hit_against_a_third_party_stays_pinned_by_the_env(monkeypatch):
    monkeypatch.setenv("FW_OSM_USE_CACHE_IF_PRESENT", "true")
    m = _load(monkeypatch, base="https://download.geofabrik.de", offline="")
    storage, calls = _hit(monkeypatch, m)
    assert m.download_region("europe/monaco", storage=storage).was_cached
    assert calls == []


def test_an_explicit_pin_is_honoured_on_our_own_store(monkeypatch):
    m = _load(monkeypatch, base=SELF, offline="1")
    storage, calls = _hit(monkeypatch, m)
    r = m.download_region("north-america/us-major-roads", storage=storage,
                          use_cache_if_present=True)
    assert r.was_cached and calls == []
