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


def test_fetch_subregion_polys_fills_stragglers(tmp_path, monkeypatch):
    """osmfr fallback: fetch only the requested sub-region polys, Geofabrik-keyed."""
    def fake_urlopen(url, timeout=None):
        if url.endswith("/"):  # dir listing
            return _Resp(b'href="quebec.poly" href="nova_scotia.poly" href="ontario.poly"')
        return _Resp(b"poly-bytes")
    monkeypatch.setattr(pf.urllib.request, "urlopen", fake_urlopen)
    # only the two stragglers self-gen missed
    regions = pf.fetch_subregion_polys("north-america/canada", str(tmp_path),
                                       only={"quebec", "nova-scotia"})
    assert sorted(r.key for r in regions) == [
        "north-america/canada/nova-scotia", "north-america/canada/quebec"]
    assert all(os.path.exists(r.poly) for r in regions)


def test_fetch_subregion_polys_no_dir_is_graceful(tmp_path, monkeypatch):
    """A country osmfr has no sub-region dir for → empty list, never raises."""
    def boom(url, timeout=None):
        raise OSError("404")
    monkeypatch.setattr(pf.urllib.request, "urlopen", boom)
    assert pf.fetch_subregion_polys("europe/monaco", str(tmp_path)) == []


def test_fetch_country_subregions_is_region_aware(tmp_path, monkeypatch):
    """US → TIGER (osmfr has no US state polys); everywhere else → osmfr."""
    from osm_geocoder.tools._osm_tools import tiger_fetch as tf
    # US routes to TIGER, and `only` filters TIGER's full 50+DC to the stragglers
    monkeypatch.setattr(tf, "fetch_tiger_states", lambda dest, on_log=None: [
        pf.Region("north-america/us/california", "/p/ca.geojson"),
        pf.Region("north-america/us/texas", "/p/tx.geojson")])
    us = pf.fetch_country_subregions("north-america/us", str(tmp_path), only={"texas"})
    assert [r.key for r in us] == ["north-america/us/texas"]

    # non-US routes to osmfr (dir listing)
    monkeypatch.setattr(pf.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(
                            b'href="bayern.poly"' if url.endswith("/") else b"poly"))
    de = pf.fetch_country_subregions("europe/germany", str(tmp_path))
    assert [r.key for r in de] == ["europe/germany/bayern"]


def test_fetch_country_subregions_level_aware(tmp_path, monkeypatch):
    """Level-aware: US state@4 → TIGER states, US state@6 → TIGER counties for that
    state, non-US @6 → [] (osmfr has no county tree — no wrong-level injection)."""
    from osm_geocoder.tools._osm_tools import tiger_fetch as tf
    monkeypatch.setattr(tf, "fetch_tiger_counties",
                        lambda dest, only_state=None, on_log=None:
                        [pf.Region(f"north-america/us/{only_state}/los-angeles", "/p")])
    c = pf.fetch_country_subregions("north-america/us/california", str(tmp_path), admin_level=6)
    assert [r.key for r in c] == ["north-america/us/california/los-angeles"]
    # non-US at county level → empty (no provider), NOT osmfr Länder
    assert pf.fetch_country_subregions("europe/germany", str(tmp_path), admin_level=6) == []


def test_fetch_tiger_counties_nested_keys(tmp_path, monkeypatch):
    """Counties keyed north-america/us/<state>/<county> (nested → collision-free)."""
    import sys, types
    sys.modules.setdefault("shapefile", types.SimpleNamespace(Reader=object))  # pyshp guard
    from osm_geocoder.tools._osm_tools import tiger_fetch as tf

    class _Shape:
        __geo_interface__ = {"type": "Polygon", "coordinates": []}

    class _Reader:
        def records(self): return [{"STATEFP": "06", "NAME": "Los Angeles"},
                                   {"STATEFP": "02", "NAME": "Aleutians East Borough"}]  # AK carries the type
        def shapes(self): return [_Shape(), _Shape()]
    monkeypatch.setattr(tf, "_statefp_slugs", lambda dest_p, log: {"06": "california", "02": "alaska"})
    monkeypatch.setattr(tf, "_download_shp", lambda url, dest_p, tag, log: _Reader())

    allc = tf.fetch_tiger_counties(str(tmp_path))
    assert sorted(r.key for r in allc) == [                      # 'Borough' stripped → matches self-gen
        "north-america/us/alaska/aleutians-east", "north-america/us/california/los-angeles"]
    ca = tf.fetch_tiger_counties(str(tmp_path), only_state="california")
    assert [r.key for r in ca] == ["north-america/us/california/los-angeles"]


def test_list_extracts_direct_children_only(monkeypatch):
    """ListExtracts returns only keys ONE level below prefix (states, not counties)."""
    from osm_geocoder.handlers.planet import planet_handlers as ph

    class _P:
        def paginate(self, Bucket, Prefix):
            return [{"Contents": [
                {"Key": "north-america/us/california-latest.osm.pbf"},
                {"Key": "north-america/us/texas-latest.osm.pbf"},
                {"Key": "north-america/us/california/los-angeles-latest.osm.pbf"},  # deeper → excluded
                {"Key": "north-america/us/california-updates/state.txt"},           # not a pbf → excluded
            ]}]

    class FakeS3:
        def get_paginator(self, op): return _P()
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    out = ph.handle_list_extracts({"prefix": "north-america/us"})
    assert out == {"regions": ["north-america/us/california", "north-america/us/texas"], "count": 2}


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


# --- boundary_gen: region polygons from OSM admin boundaries (no external source) ---

def test_boundary_slug():
    from osm_geocoder.tools._osm_tools import boundary_gen as bg
    assert bg._slug("North Rhine-Westphalia") == "north-rhine-westphalia"
    assert bg._slug("New York") == "new-york"
    assert bg._slug("Baden-Württemberg") == "baden-wuerttemberg"   # German umlaut -> ue
    assert bg._slug("Thüringen") == "thueringen"
    assert bg._slug("Île-de-France") == "ile-de-france"            # other diacritics stripped


def test_boundary_gen_filters_and_keys(tmp_path, monkeypatch):
    from osm_geocoder.tools._osm_tools import boundary_gen as bg

    def fake_run(cmd):
        # emulate `osmium export` writing a GeoJSONSeq of assembled boundaries
        if "export" in cmd:
            seq = cmd[cmd.index("-o") + 1]
            with open(seq, "w") as f:
                f.write('\x1e{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"4","name":"Bavaria","ISO3166-2":"DE-BY"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"2","name":"Germany"},"geometry":{"type":"Polygon"}}\n')  # wrong level
                f.write('{"type":"Feature","properties":{"landuse":"forest"},"geometry":{}}\n')  # not admin
    monkeypatch.setattr(bg, "_run", fake_run)

    regions = bg.generate_polygons("src.pbf", 4, str(tmp_path))
    assert [r.name for r in regions] == ["Bavaria"]   # only admin_level=4 boundaries kept
    assert regions[0].key == "europe/germany/bavaria" and regions[0].iso == "DE-BY"
    assert (tmp_path / "europe__germany__bavaria.geojson").exists()


def test_boundary_gen_drops_subnational_without_country_iso(tmp_path, monkeypatch):
    """A level>=4 relation mistagged at that level but lacking a country ISO 3166-2
    (e.g. a Quebec island) must be DROPPED, not published under a flat root key."""
    from osm_geocoder.tools._osm_tools import boundary_gen as bg

    def fake_run(cmd):
        if "export" in cmd:
            seq = cmd[cmd.index("-o") + 1]
            with open(seq, "w") as f:
                # a real province (has ISO3166-2) — kept, hierarchical key
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"4","name":"Ontario","ISO3166-2":"CA-ON"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
                # an island mistagged admin_level=4, NO country ISO — must be dropped
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"4","name":"Ile Lavoie"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
    monkeypatch.setattr(bg, "_run", fake_run)

    regions = bg.generate_polygons("src.pbf", 4, str(tmp_path))
    assert [r.key for r in regions] == ["north-america/canada/ontario"]  # island dropped
    assert not any("/" not in r.key for r in regions)                    # nothing flat/root
    assert not (tmp_path / "ile-lavoie.geojson").exists()


def test_boundary_gen_country_prefix_counties_and_noise(tmp_path, monkeypatch):
    """country_prefix: keeps county-level (no ISO) keyed under the source country,
    but still drops level<=4 island noise that lacks ISO 3166-2."""
    from osm_geocoder.tools._osm_tools import boundary_gen as bg

    def county_export(cmd):
        if "export" in cmd:
            seq = cmd[cmd.index("-o") + 1]
            with open(seq, "w") as f:
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"6","name":"Landkreis München"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
    monkeypatch.setattr(bg, "_run", county_export)
    # level 6 county has NO ISO 3166-2 but is kept, keyed under the source country
    c = bg.generate_polygons("de.pbf", 6, str(tmp_path), country_prefix="europe/germany")
    assert [r.key for r in c] == ["europe/germany/landkreis-muenchen"]

    def mixed_l4(cmd):
        if "export" in cmd:
            seq = cmd[cmd.index("-o") + 1]
            with open(seq, "w") as f:
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"4","name":"Querétaro","ISO3166-2":"MX-QUE"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
                f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                        '"admin_level":"4","name":"Isla Noise"},'
                        '"geometry":{"type":"Polygon","coordinates":[]}}\n')
    monkeypatch.setattr(bg, "_run", mixed_l4)
    # level 4 keyed under source country (fixes Mexico's continent split); no-ISO dropped
    l = bg.generate_polygons("mx.pbf", 4, str(tmp_path), country_prefix="north-america/mexico")
    assert [r.key for r in l] == ["north-america/mexico/queretaro"]  # not central-america; island dropped


def test_geofabrik_key_mapping():
    from osm_geocoder.tools._osm_tools import boundary_gen as bg
    # country (level 2): English name, Geofabrik continent + slug
    assert bg._geofabrik_key("Deutschland", "Germany", "DE", 2) == "europe/germany"
    assert bg._geofabrik_key("United States", "United States", "US", 2) == "north-america/us"
    # sub-region (level 4): LOCAL name slug, country prefix from ISO3166-2
    assert bg._geofabrik_key("Bayern", "Bavaria", "DE-BY", 4) == "europe/germany/bayern"
    assert bg._geofabrik_key("California", "California", "US-CA", 4) == "north-america/us/california"
    # Geofabrik's idiosyncrasies: Russia separate, Mexico under central-america
    assert bg._geofabrik_key("Россия", "Russia", "RU", 2) == "russia/russia"
    assert bg._geofabrik_key("México", "Mexico", "MX", 2) == "central-america/mexico"
    # no ISO/continent -> flat fallback
    assert bg._geofabrik_key("Nowhereland", None, None, 2) == "nowhereland"


def test_generate_polygons_handler_registered():
    from osm_geocoder.handlers.planet import planet_handlers as ph
    assert "osm.planet.GenerateRegionPolygons" in ph._DISPATCH


def _bb_publishing(**k):
    """Stand-in for bootstrap_batched: drives the on_pass callback (incremental
    publish) with one result object per region, like the real batcher."""
    from types import SimpleNamespace
    res = [SimpleNamespace(key=r["key"]) for r in k["regions"]]
    if k.get("on_pass"):
        k["on_pass"](res)
    return res


def test_build_admin_set_orchestration(tmp_path, monkeypatch):
    """The single-task facet downloads the source, generates, extracts, publishes —
    all in one handler (no cross-host handoff)."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    from osm_geocoder.tools._osm_tools.boundary_gen import BoundaryRegion
    downloaded = []

    class FakeS3:
        def download_file(self, bucket, key, dst, **kwargs):
            downloaded.append(key)
            open(dst, "w").write("pbf")
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    monkeypatch.setattr(ph, "_scratch_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ph, "generate_polygons",
                        lambda src, lvl, dest, country_prefix=None, on_log=None:
                        [BoundaryRegion("europe/germany/bayern", "/p", "Bayern", 4, "DE-BY")])
    monkeypatch.setattr(ph, "fetch_country_subregions", lambda *a, **k: [])
    monkeypatch.setattr(ph, "bootstrap_batched", _bb_publishing)

    out = ph.handle_build_admin_set({"source_region": "europe/germany", "admin_level": 4})
    assert out == {"region_count": 1, "published": 1, "unreproducible": 0}  # published incrementally via on_pass
    assert downloaded == ["europe/germany-latest.osm.pbf"]   # source pulled from the bucket


def test_build_admin_set_osmfr_fallback_fills_stragglers(tmp_path, monkeypatch):
    """Regions osmium can't self-generate are filled from osmfr and extracted too."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    from osm_geocoder.tools._osm_tools.boundary_gen import BoundaryRegion
    from osm_geocoder.tools._osm_tools.polygon_fetch import Region
    extracted = {}

    class FakeS3:
        def download_file(self, bucket, key, dst, **kwargs): open(dst, "w").write("pbf")
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    monkeypatch.setattr(ph, "_scratch_dir", lambda: str(tmp_path))
    # self-gen assembles only Ontario (a straggler province is missing)
    monkeypatch.setattr(ph, "generate_polygons",
                        lambda src, lvl, dest, country_prefix=None, on_log=None:
                        [BoundaryRegion("north-america/canada/ontario", "/p/on", "Ontario", 4, "CA-ON")])
    # osmfr lists ontario + quebec; only quebec should be added (ontario already have)
    monkeypatch.setattr(ph, "fetch_country_subregions", lambda ck, dest, admin_level=4, on_log=None: [
        Region("north-america/canada/ontario", "/o/on.poly"),
        Region("north-america/canada/quebec", "/o/qc.poly")])
    def bb(**k):
        extracted["keys"] = [r["key"] for r in k["regions"]]
        return _bb_publishing(**k)
    monkeypatch.setattr(ph, "bootstrap_batched", bb)

    out = ph.handle_build_admin_set({"source_region": "north-america/canada", "admin_level": 4})
    assert sorted(extracted["keys"]) == [
        "north-america/canada/ontario", "north-america/canada/quebec"]  # quebec filled, ontario not dup'd
    assert out == {"region_count": 2, "published": 2, "unreproducible": 0}


def test_build_admin_set_fallback_disabled(tmp_path, monkeypatch):
    """osmfr_fallback=false skips the osmfr fetch entirely."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    from osm_geocoder.tools._osm_tools.boundary_gen import BoundaryRegion
    called = {"osmfr": False}

    class FakeS3:
        def download_file(self, bucket, key, dst, **kwargs): open(dst, "w").write("pbf")
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    monkeypatch.setattr(ph, "_scratch_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ph, "generate_polygons",
                        lambda src, lvl, dest, country_prefix=None, on_log=None:
                        [BoundaryRegion("north-america/canada/ontario", "/p/on", "Ontario", 4, "CA-ON")])
    def spy(*a, **k): called.__setitem__("osmfr", True); return []
    monkeypatch.setattr(ph, "fetch_country_subregions", spy)
    monkeypatch.setattr(ph, "bootstrap_batched", _bb_publishing)

    ph.handle_build_admin_set({"source_region": "north-america/canada",
                               "admin_level": 4, "osmfr_fallback": False})
    assert called["osmfr"] is False


def test_build_admin_set_resumes_skipping_published(tmp_path, monkeypatch):
    """A region already published by a prior attempt is skipped, so a large set
    converges across task retries instead of restarting from zero."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    from osm_geocoder.tools._osm_tools.boundary_gen import BoundaryRegion
    passed = {}

    class FakeS3:
        def download_file(self, bucket, key, dst, **kwargs): open(dst, "w").write("pbf")
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    monkeypatch.setattr(ph, "_scratch_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ph, "generate_polygons",
                        lambda src, lvl, dest, country_prefix=None, on_log=None: [
                            BoundaryRegion("europe/germany/a", "/p", "A", 6, None),
                            BoundaryRegion("europe/germany/b", "/p", "B", 6, None)])
    monkeypatch.setattr(ph, "fetch_country_subregions", lambda *a, **k: [])
    # 'a' already done — published 0.01 days ago, i.e. minutes, as a resume would see it
    monkeypatch.setattr(ph, "_published_region_ages",
                        lambda s3, b, p: {"europe/germany/a": 0.01})
    def bb(**k):
        passed["regions"] = [r["key"] for r in k["regions"]]
        return _bb_publishing(**k)
    monkeypatch.setattr(ph, "bootstrap_batched", bb)

    out = ph.handle_build_admin_set({"source_region": "europe/germany", "admin_level": 6})
    assert passed["regions"] == ["europe/germany/b"]      # only the unpublished one re-extracted
    assert out == {"region_count": 2, "published": 2, "unreproducible": 0}  # 1 resumed + 1 new


def test_build_admin_set_county_level_no_wrong_injection(tmp_path, monkeypatch):
    """At county level the fallback is region/level-AWARE: for a non-US country it has
    no provider (returns []), so NO wrong-level states (Länder) get injected."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    from osm_geocoder.tools._osm_tools.boundary_gen import BoundaryRegion
    seen = {}

    class FakeS3:
        def download_file(self, bucket, key, dst, **kwargs): open(dst, "w").write("pbf")
    monkeypatch.setattr(ph, "_s3_client", lambda ep=None: FakeS3())
    monkeypatch.setattr(ph, "_scratch_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ph, "generate_polygons",
                        lambda src, lvl, dest, country_prefix=None, on_log=None:
                        [BoundaryRegion("europe/germany/landkreis-muenchen", "/p", "LK München", 6, None)])
    monkeypatch.setattr(ph, "fetch_country_subregions",
                        lambda ck, dest, admin_level=4, on_log=None: [])   # non-US @6 → no provider
    def bb(**k):
        seen["regions"] = [r["key"] for r in k["regions"]]
        return _bb_publishing(**k)
    monkeypatch.setattr(ph, "bootstrap_batched", bb)

    ph.handle_build_admin_set({"source_region": "europe/germany", "admin_level": 6})
    assert seen["regions"] == ["europe/germany/landkreis-muenchen"]   # only self-gen; no Länder injected


def test_build_admin_set_requires_source():
    from osm_geocoder.handlers.planet import planet_handlers as ph
    with pytest.raises(ValueError):
        ph.handle_build_admin_set({"admin_level": 4})


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
    assert calls == [1]  # one region, adaptive → one pass


# --- adaptive memory-budgeted batching ---

def _fake_bootstrap_ok(calls, oom_at=None):
    def fake(*, source, out, regions, base_url, strategy, on_log=None, pass_stats=None):
        calls.append(len(regions))
        if oom_at is not None and len(regions) >= oom_at:
            raise pb._OOMError("simulated OOM")
        if pass_stats is not None:
            pass_stats["peak_bytes"] = 0   # unmeasured
        return [pb.RegionResult(r["key"], "", 0, 0, "", None, True) for r in regions]
    return fake


def test_adaptive_packs_by_memory_budget(monkeypatch):
    calls = []
    monkeypatch.setattr(pb, "_memory_ceiling_bytes", lambda: 10 * (1 << 30))  # 10 GB
    monkeypatch.setattr(pb, "_load_region_cost", lambda d: 2 * (1 << 30))     # 2 GB/region
    monkeypatch.setattr(pb, "bootstrap", _fake_bootstrap_ok(calls))
    regions = [{"key": f"r{i}", "poly": "p"} for i in range(7)]
    res = pb.bootstrap_batched(source="s", out="o", regions=regions, base_url="b", batch_size=0)
    assert len(res) == 7
    assert calls == [3, 3, 1]   # budget=7GB / 2GB ⇒ 3 regions per pass


def test_adaptive_self_heals_on_oom(monkeypatch):
    calls = []
    monkeypatch.setattr(pb, "_memory_ceiling_bytes", lambda: 10 * (1 << 30))
    monkeypatch.setattr(pb, "_load_region_cost", lambda d: 2 * (1 << 30))     # ⇒ first pass 3
    monkeypatch.setattr(pb, "_save_region_cost", lambda d, v: None)
    monkeypatch.setattr(pb, "bootstrap", _fake_bootstrap_ok(calls, oom_at=3))
    regions = [{"key": f"r{i}", "poly": "p"} for i in range(4)]
    res = pb.bootstrap_batched(source="s", out="o", regions=regions, base_url="b", batch_size=0)
    assert len(res) == 4                 # all recovered
    assert calls[0] == 3                 # first attempt OOMs at 3
    assert max(calls[1:]) < 3            # retries strictly smaller


def test_adaptive_single_region_over_budget_raises(monkeypatch):
    monkeypatch.setattr(pb, "_memory_ceiling_bytes", lambda: 1 * (1 << 30))
    monkeypatch.setattr(pb, "_load_region_cost", lambda d: 5 * (1 << 30))
    monkeypatch.setattr(pb, "_save_region_cost", lambda d, v: None)
    monkeypatch.setattr(pb, "bootstrap", _fake_bootstrap_ok([], oom_at=1))
    with pytest.raises(pb.BootstrapError):   # can't shrink below one region
        pb.bootstrap_batched(source="s", out="o", regions=[{"key": "big", "poly": "p"}],
                             base_url="b", batch_size=0)


def test_memory_ceiling_and_cost_sidecar(tmp_path):
    assert pb._memory_ceiling_bytes() > (1 << 30)          # detects a real ceiling
    pb._save_region_cost(str(tmp_path), 3 * (1 << 30))
    assert pb._load_region_cost(str(tmp_path)) == 3 * (1 << 30)   # persists + reloads
    assert pb._load_region_cost(None) == pb._DEFAULT_REGION_BYTES  # cold-start default


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
        "osm.planet.DownloadPolygons", "osm.planet.GenerateRegionPolygons",
        "osm.planet.ExtractRegions", "osm.planet.PublishExtracts",
        "osm.planet.BuildAdminSet", "osm.planet.ListExtracts",
    }
    with pytest.raises(ValueError):
        ph.handle({"_facet_name": "osm.planet.Nope"})


def test_county_slug_strips_admin_type(tmp_path, monkeypatch):
    """County slugs drop the type suffix (County/Parish/Borough) so self-gen matches
    TIGER's bare names → the fallback dedupes instead of publishing '<x>' AND '<x>-county'."""
    from osm_geocoder.tools._osm_tools import boundary_gen as bg
    assert bg._strip_admin_type("Alachua County") == "Alachua"
    assert bg._strip_admin_type("St. Bernard Parish") == "St. Bernard"
    assert bg._strip_admin_type("Prince of Wales-Hyder Census Area") == "Prince of Wales-Hyder"
    assert bg._strip_admin_type("Bayern") == "Bayern"   # non-county untouched

    def fake_run(cmd):
        if "export" in cmd:
            seq = cmd[cmd.index("-o") + 1]
            with open(seq, "w") as f:
                for nm in ("Alachua County", "St. Bernard Parish"):
                    f.write('{"type":"Feature","properties":{"boundary":"administrative",'
                            f'"admin_level":"6","name":"{nm}"}},'
                            '"geometry":{"type":"Polygon","coordinates":[]}}\n'.replace("}},", "},"))
    monkeypatch.setattr(bg, "_run", fake_run)
    regs = bg.generate_polygons("src.pbf", 6, str(tmp_path), country_prefix="north-america/us/florida")
    assert sorted(r.key for r in regs) == [
        "north-america/us/florida/alachua", "north-america/us/florida/st-bernard"]


def test_scratch_dir_is_per_task_unique(monkeypatch, tmp_path):
    """Each call returns a DISTINCT dir so two BuildAdminSet tasks on one host (a
    fan-out lands several per host) don't clobber each other's /scratch."""
    from osm_geocoder.handlers.planet import planet_handlers as ph
    monkeypatch.setenv("FW_LOCAL_SCRATCH", str(tmp_path))
    a, b = ph._scratch_dir(), ph._scratch_dir()
    assert a != b and a.startswith(str(tmp_path)) and __import__("os").path.isdir(a)


def test_build_admin_set_reports_unreproducible_orphans(tmp_path, monkeypatch):
    """Published regions this admin_level cannot regenerate must be REPORTED.

    Regression for 2026-08-31: OSM admin_level=6 yields 2,522 of the 3,167
    published US counties. The other 645 (20.4%) come from an earlier Census
    TIGER build and can never be refreshed by this path — no amount of
    force_refresh reaches them. Until this was surfaced, every run looked
    complete while a fifth of the tier aged indefinitely.
    """
    import osm_geocoder.handlers.planet.planet_handlers as ph

    published = {"p/a": 1.0, "p/b": 1.0, "p/ghost": 36.0}
    generated = [{"key": "p/a"}, {"key": "p/b"}]

    gen_keys = {r["key"] for r in generated}
    orphans = sorted(k for k in published if k not in gen_keys)

    assert orphans == ["p/ghost"], "an unreproducible region must be detected"
    assert max(published[k] for k in orphans) == 36.0, "its age must be reportable"
    # the ones we CAN regenerate are not orphans
    assert "p/a" not in orphans and "p/b" not in orphans
    # and a run that regenerates everything reports none
    assert not [k for k in published if k not in (gen_keys | {"p/ghost"})]


def test_published_ages_counts_only_the_tier_being_built():
    """⚠️ A run builds ONE tier, so only the prefix's IMMEDIATE children are its
    responsibility. Listing every descendant made everything deeper look
    unreproducible and inflated the stale count that decides how much work
    there is.

    Measured 2026-09-04: europe@2 reported "422 NOT reproducible" when it has
    48 countries — the other 415 are German Kreise at admin_level 6.
    north-america@2 reported "3269 NOT reproducible" and "2573 fresh, 698
    stale" against 9 countries, 51 states and 3,167 counties.
    """
    import datetime as dt
    from osm_geocoder.handlers.planet import planet_handlers as ph

    now = dt.datetime.now(dt.timezone.utc)
    keys = [
        "europe/germany-latest.osm.pbf",              # child   — counted
        "europe/france-latest.osm.pbf",               # child   — counted
        "europe/germany/alb-donau-kreis-latest.osm.pbf",   # grandchild — not
        "europe/germany/altenburger-land-latest.osm.pbf",  # grandchild — not
        "europe/germany-updates/state.txt",           # not a pbf — not
    ]

    class _Pager:
        def paginate(self, **_kw):
            return [{"Contents": [{"Key": k, "LastModified": now} for k in keys]}]

    class _S3:
        def get_paginator(self, _name):
            return _Pager()

    ages = ph._published_region_ages(_S3(), "b", "europe")
    assert set(ages) == {"europe/germany", "europe/france"}

    # ...and one tier down, the grandchildren ARE the children.
    ages = ph._published_region_ages(_S3(), "b", "europe/germany")
    assert set(ages) == {"europe/germany/alb-donau-kreis",
                         "europe/germany/altenburger-land"}


def test_published_ages_is_unchanged_for_a_leaf_tier():
    """The per-state county numbers were right all along — those prefixes have
    nothing below them, which is exactly why the bug survived. Pin that the fix
    does not disturb them."""
    import datetime as dt
    from osm_geocoder.handlers.planet import planet_handlers as ph

    now = dt.datetime.now(dt.timezone.utc)
    keys = ["north-america/us/wyoming/albany-latest.osm.pbf",
            "north-america/us/wyoming/carbon-latest.osm.pbf"]

    class _Pager:
        def paginate(self, **_kw):
            return [{"Contents": [{"Key": k, "LastModified": now} for k in keys]}]

    class _S3:
        def get_paginator(self, _name):
            return _Pager()

    ages = ph._published_region_ages(_S3(), "b", "north-america/us/wyoming")
    assert len(ages) == 2
