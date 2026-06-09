"""Unit tests for missing-.md5 tolerance in the Geofabrik downloader.

Some extracts (combined regions like haiti-and-domrep, certain small areas)
ship a ``.pbf`` with NO companion ``.md5`` (Geofabrik returns 404). A missing
checksum is not corruption evidence — the downloader should proceed and anchor
integrity on the locally-computed sha256, rather than treating the region as
permanently un-cacheable. A *mismatched* (present but wrong) md5 stays an error.
"""

import urllib.error

import pytest

pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_download")


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/.md5", code, "boom", {}, None)


def test_fetch_md5_or_none_returns_none_on_404(monkeypatch):
    def _raise(url):
        raise _http_error(404)

    monkeypatch.setattr(pd, "fetch_md5", _raise)
    assert pd.fetch_md5_or_none("http://x") is None


def test_fetch_md5_or_none_reraises_non_404(monkeypatch):
    def _raise(url):
        raise _http_error(503)

    monkeypatch.setattr(pd, "fetch_md5", _raise)
    with pytest.raises(urllib.error.HTTPError):
        pd.fetch_md5_or_none("http://x")


def test_fetch_md5_or_none_passes_through_digest(monkeypatch):
    monkeypatch.setattr(pd, "fetch_md5", lambda url: "a" * 32)
    assert pd.fetch_md5_or_none("http://x") == "a" * 32
