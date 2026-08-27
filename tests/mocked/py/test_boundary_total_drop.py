"""Keeping none of the candidates found is a defect, not an empty result.

2026-08-26: a scheduled `europe` @ admin_level=2 run downloaded 40 GB, built a
227 MB boundary extract, dropped all 94 countries it found, published nothing,
and reported success after 3h20m. It logged the reason — "generated 0
admin_level=2 polygons (dropped 94 without a country ISO 3166-2)" — but the
workflow went green, so a weekly job would have burned 3h20m every week forever
while reporting success.

The cause is a real parameter mismatch: under a country_prefix, levels <= 4 are
SUBDIVISIONS and must carry ISO 3166-2 ("DE-BY"); a COUNTRY carries ISO 3166-1
("DE") and is dropped. Asking a CONTINENT for admin_level=2 hits it every time.
"""
import json

import pytest

from osm_geocoder.tools._osm_tools import boundary_gen as bg


def _feature(name, iso, level):
    return {"type": "Feature",
            "properties": {"boundary": "administrative", "admin_level": str(level),
                           "name": name, "name:en": name, **({"ISO3166-1:alpha2": iso}
                                                             if "-" not in iso else
                                                             {"ISO3166-2": iso})},
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}


def _fake_run(monkeypatch, tmp_path, features, level):
    """Stand in for the two osmium calls: the filter and the export."""
    def fake(cmd):
        if cmd[1] == "export":
            out = cmd[cmd.index("-o") + 1]
            with open(out, "w") as f:
                for feat in features:
                    f.write(json.dumps(feat) + "\n")
        else:                                   # tags-filter -> just touch the pbf
            out = cmd[cmd.index("-o") + 1]
            open(out, "wb").close()
    monkeypatch.setattr(bg, "_run", fake)


def test_dropping_every_candidate_raises(tmp_path, monkeypatch):
    """The europe@2 case: countries carry ISO 3166-1, so all are dropped."""
    feats = [_feature("Germany", "DE", 2), _feature("France", "FR", 2)]
    _fake_run(monkeypatch, tmp_path, feats, 2)
    with pytest.raises(bg.BoundaryError) as e:
        bg.generate_polygons("src.pbf", 2, str(tmp_path), country_prefix="europe")
    msg = str(e.value)
    assert "kept NONE" in msg
    assert "admin_level >= 4" in msg, "the error must say what to do instead"


def test_a_genuinely_empty_level_is_not_an_error(tmp_path, monkeypatch):
    """dropped == 0 means the level simply has no units here — legitimate."""
    _fake_run(monkeypatch, tmp_path, [], 4)
    assert bg.generate_polygons("src.pbf", 4, str(tmp_path), country_prefix="europe/monaco") == []


def test_a_normal_subdivision_split_still_works(tmp_path, monkeypatch):
    """Levels <= 4 WITH ISO 3166-2 are kept — the case this path is built for."""
    feats = [_feature("Bayern", "DE-BY", 4), _feature("Hessen", "DE-HE", 4)]
    _fake_run(monkeypatch, tmp_path, feats, 4)
    got = bg.generate_polygons("src.pbf", 4, str(tmp_path), country_prefix="europe/germany")
    assert [r.key for r in got] == ["europe/germany/bayern", "europe/germany/hessen"]


def test_partial_drop_is_tolerated(tmp_path, monkeypatch):
    """Some island noise alongside real units must NOT fail the run."""
    feats = [_feature("Bayern", "DE-BY", 4), _feature("SomeIsland", "XX", 4)]
    _fake_run(monkeypatch, tmp_path, feats, 4)
    got = bg.generate_polygons("src.pbf", 4, str(tmp_path), country_prefix="europe/germany")
    assert len(got) == 1


def test_a_continent_split_keys_countries_under_the_continent(tmp_path, monkeypatch):
    """admin_level 2 must use the STANDALONE keying, not the country_prefix one.

    A country carries ISO 3166-1 ("DE"); the country_prefix branch demands
    ISO 3166-2 ("DE-BY") and drops everything without it. `europe` @ 2 assembled
    94 countries and kept zero (2026-08-26). The standalone path's
    _geofabrik_key is built for this and yields `<continent>/<country>`.
    """
    feats = [_feature("Germany", "DE", 2), _feature("France", "FR", 2)]
    _fake_run(monkeypatch, tmp_path, feats, 2)
    got = bg.generate_polygons("src.pbf", 2, str(tmp_path), country_prefix=None)
    assert sorted(r.key for r in got) == ["europe/france", "europe/germany"]


def test_the_handler_drops_country_prefix_at_level_2(monkeypatch):
    """The wiring, not just the primitive: BuildAdminSet must pass None at <= 2
    and the source_region at >= 4."""
    from osm_geocoder.handlers.planet import planet_handlers as ph

    seen = {}

    def fake_generate(src, lvl, dest, *, country_prefix=None, on_log=None):
        seen[lvl] = country_prefix
        raise RuntimeError("stop here — we only care about the argument")

    monkeypatch.setattr(ph, "generate_polygons", fake_generate)
    monkeypatch.setattr(ph, "_s3_client", lambda *a, **k: _NoopS3())
    monkeypatch.setattr(ph, "_ensure_public_bucket", lambda *a, **k: None)

    for level, expected in ((2, None), (6, "europe/germany")):
        try:
            ph.handle_build_admin_set({"source_region": "europe/germany",
                                       "admin_level": level})
        except Exception:
            pass
        assert seen.get(level) == expected, (
            f"level {level}: country_prefix should be {expected!r}, got {seen.get(level)!r}")


class _NoopS3:
    def download_file(self, *a, **k):
        pass

    def get_paginator(self, *a, **k):
        class P:
            def paginate(self, **kw):
                return []
        return P()
