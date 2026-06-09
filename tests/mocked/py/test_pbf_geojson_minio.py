"""ToGeoJson / convert_region must localize the cached PBF (download from the
object store) before handing it to osmium, then finalize the GeoJSON back —
i.e. work natively against MinIO/S3, not just local disk."""

import pytest

st = pytest.importorskip("osm_geocoder.tools._osm_tools.storage")
pd = pytest.importorskip("osm_geocoder.tools._osm_tools.pbf_geojson")


def test_storage_localize_defaults_to_passthrough():
    # Base/local storage: a local path is already local.
    assert st.LocalStorage().localize("/data/cache/x.pbf") == "/data/cache/x.pbf"


def test_convert_region_localizes_s3_source_before_osmium(tmp_path, monkeypatch):
    region = "europe/germany"
    s3_uri = "s3://afl-cache/cache/osm/pbf/europe/germany-latest.osm.pbf"
    local_pbf = tmp_path / "germany.pbf"
    local_pbf.write_bytes(b"\x00pbf\x00")
    calls = {"localized": [], "osmium_input": None, "finalized": None}

    class FakeS3:
        name = "s3"

        def exists(self, p):  # the s3 object is present
            return True

        def mkdir_p(self, p):  # object stores have no dirs
            return None

        def size(self, p):
            return 0

        def localize(self, p):  # <-- the behavior under test
            calls["localized"].append(p)
            return str(local_pbf)

        def finalize_from_local(self, lp, dp):
            import os
            import shutil
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.move(lp, dp)
            calls["finalized"] = dp

    fs = FakeS3()

    out_uri = "s3://afl-cache/cache/osm/geojson/europe/germany-latest.geojson"
    def _cache_path(namespace, cache_type, rel, storage=None):
        return s3_uri if cache_type == "pbf" else out_uri
    monkeypatch.setattr(pd.sidecar, "cache_path", _cache_path)
    monkeypatch.setattr(pd.sidecar, "read_sidecar",
                        lambda *a, **k: {"source": {"url": "http://x"}, "sha256": "deadbeef"})
    monkeypatch.setattr(pd, "is_up_to_date", lambda *a, **k: False)
    monkeypatch.setattr(pd, "geojson_rel_path", lambda r, f: "europe/germany-latest.geojson")
    monkeypatch.setattr(pd, "_staging_path", lambda r, f, s=None: pd.Path(str(tmp_path / "stage.geojson")))
    monkeypatch.setattr(pd.sidecar, "write_sidecar", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pd.sidecar, "utcnow_iso", lambda: "2026-06-09T00:00:00Z")
    monkeypatch.setattr(pd, "_osmium_version", lambda b: "1.16.0")

    def fake_osmium(cmd, **kw):
        # osmium export -f geojson -o <staging> --overwrite <input>
        calls["osmium_input"] = cmd[-1]
        out = cmd[cmd.index("-o") + 1]
        pd.Path(out).write_text('{"type":"FeatureCollection","features":[]}')

        class R:
            stderr = ""
            returncode = 0
        return R()

    monkeypatch.setattr(pd.subprocess, "run", fake_osmium)

    res = pd.convert_region(region, storage=fs)

    # 1) it localized the s3 object
    assert calls["localized"] == [s3_uri]
    # 2) osmium ran on the LOCALIZED local file, never the s3:// URI
    assert calls["osmium_input"] == str(local_pbf)
    assert not calls["osmium_input"].startswith("s3://")
    # 3) output finalized back to the (object-store) destination
    assert calls["finalized"] == out_uri
    assert calls["finalized"].startswith("s3://afl-cache/cache/osm/geojson/")
    assert res.region == region and res.was_cached is False
