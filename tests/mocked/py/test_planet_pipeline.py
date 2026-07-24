"""Tests for the osm.planet pipeline: planet fetch/update, polygon fetch, batched
extract, and handler dispatch. Network-free (urllib mocked)."""
import io
import os

import pytest

pf = pytest.importorskip("osm_geocoder.tools._osm_tools.polygon_fetch")
plf = pytest.importorskip("osm_geocoder.tools._osm_tools.planet_fetch")
pb = pytest.importorskip("osm_geocoder.tools._osm_tools.planet_bootstrap")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- polygon key mapping (Geofabrik-style normalisation) ---

def test_geofabrik_key_mapping():
    assert pf._geofabrik_key("europe", "czech_republic") == "europe/czech-republic"
    assert pf._geofabrik_key("oceania", "fiji") == "australia-oceania/fiji"       # top-level remap
    assert pf._geofabrik_key("central-america", "costa_rica") == "central-america/costa-rica"
    assert pf._geofabrik_key("africa", None) == "africa"                          # continent


def test_fetch_polygons_countries_mocked(tmp_path, monkeypatch):
    def fake_urlopen(url, timeout=None):
        if url.endswith("/"):  # directory listing
            return _Resp(b'href="france.poly" href="czech_republic.poly"')
        return _Resp(b"poly-bytes")
    monkeypatch.setattr(pf.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pf, "CONTINENTS", ("europe",))  # restrict to one continent

    regions = pf.fetch_polygons(str(tmp_path), scope="countries")
    assert sorted(r.key for r in regions) == ["europe/czech-republic", "europe/france"]
    assert all(os.path.exists(r.poly) for r in regions)


def test_fetch_polygons_bad_scope(tmp_path):
    with pytest.raises(pf.PolygonError):
        pf.fetch_polygons(str(tmp_path), scope="planets")


# --- subnational (TIGER US states) ---

def test_subnational_scope_routes_to_tiger(tmp_path, monkeypatch):
    from osm_geocoder.tools._osm_tools import tiger_fetch as tf
    seen = {}

    def fake(dest, on_log=None):
        seen["dest"] = dest
        return [pf.Region("north-america/us/california", "/p/california.geojson")]
    monkeypatch.setattr(tf, "fetch_tiger_states", fake)

    out = pf.fetch_polygons(str(tmp_path), scope="subnational")
    assert seen["dest"] == str(tmp_path)
    assert out[0].key == "north-america/us/california"
    assert "subnational" in pf.SCOPES


def test_tiger_state_slug():
    from osm_geocoder.tools._osm_tools import tiger_fetch as tf
    assert tf._slug("California") == "california"
    assert tf._slug("New York") == "new-york"
    assert tf._slug("District of Columbia") == "district-of-columbia"


def test_bootstrap_poly_file_type():
    assert pb._poly_file_type("/x/california.geojson") == "geojson"   # TIGER state
    assert pb._poly_file_type("/x/Y.JSON") == "geojson"
    assert pb._poly_file_type("/x/france.poly") == "poly"             # osmfr


# --- batched extract splits into osmium passes to bound RAM ---

def test_bootstrap_batched_splits(monkeypatch):
    calls = []

    def fake_bootstrap(*, source, out, regions, base_url, strategy, on_log=None):
        calls.append(len(regions))
        return [pb.RegionResult(r["key"], "", 0, 0, "", None, True) for r in regions]
    monkeypatch.setattr(pb, "bootstrap", fake_bootstrap)

    regions = [{"key": f"r{i}", "poly": "p"} for i in range(10)]
    res = pb.bootstrap_batched(source="s", out="o", regions=regions, base_url="b",
                               strategy="simple", batch_size=3)
    assert len(res) == 10
    assert calls == [3, 3, 3, 1]  # 4 batches


def test_bootstrap_batched_single_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(pb, "bootstrap",
                        lambda **k: (calls.append(len(k["regions"])) or []))
    pb.bootstrap_batched(source="s", out="o", regions=[{"key": "a", "poly": "p"}],
                         base_url="b", batch_size=0)
    assert calls == [1]  # batch_size 0 => single pass


# --- planet fetch/update ---

def test_planet_md5(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"hello")
    assert plf._md5(str(p)) == "5d41402abc4b2a76b9719d911017c592"


def test_update_planet_no_timestamp_is_graceful(monkeypatch):
    class H:
        url = None
        sequence = None
        timestamp = None
    monkeypatch.setattr(plf._repl, "get_replication_header", lambda p: H())
    u = plf.update_planet("planet.pbf")
    assert u.advanced is False and "no timestamp" in u.status


# --- handler dispatch ---

def test_handler_dispatch_routes_and_rejects_unknown():
    from osm_geocoder.handlers.planet import planet_handlers as ph
    assert set(ph._DISPATCH) == {
        "osm.planet.DownloadPlanet", "osm.planet.UpdatePlanet",
        "osm.planet.DownloadPolygons", "osm.planet.ExtractRegions",
        "osm.planet.PublishExtracts",
    }
    with pytest.raises(ValueError):
        ph.handle({"_facet_name": "osm.planet.Nope"})
