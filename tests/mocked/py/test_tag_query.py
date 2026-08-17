"""Ad-hoc tag queries against the local extracts (``osm.query.TagQuery``).

The pure parts — normalisation, the expression digest that IS the cache key,
and the refusals — need no data and always run. The end-to-end test builds a
tiny PBF and runs the real osmium two-pass, and skips itself without the binary.

What is being pinned is mostly the cache identity. ``pbf_extract``'s categories
are invalidated by a human bumping ``filter_version``; forget, and stale output
is served silently. Here a changed question is a different cache path by
construction, and an equivalent one (reordered terms, extra whitespace) is the
same path — otherwise every caller would re-scan a continent to learn what the
last one already knew.
"""
import shutil
import subprocess

import pytest

tq = pytest.importorskip("osm_geocoder.tools._osm_tools.tag_query")

# Bind through the module under test, NOT by a second import path. The tools are
# reachable as both `_osm_tools.x` (the dir is on sys.path) and
# `osm_geocoder.tools._osm_tools.x`, which are DISTINCT module objects — so an
# ExtractionError imported the other way is a different class and never matches,
# and a `sidecar` patched the other way is never seen by the code under test.
ExtractionError = tq.ExtractionError
sidecar = tq.sidecar


# --- expression handling (no data needed) ----------------------------------


def test_term_order_and_whitespace_do_not_change_the_question():
    assert tq.normalise("nwr/amenity=cafe   nwr/amenity=bar") == "nwr/amenity=bar nwr/amenity=cafe"
    assert tq.digest_of("nwr/a=b nwr/c=d") == tq.digest_of(" nwr/c=d  nwr/a=b ")


def test_a_different_question_is_a_different_cache_key():
    assert tq.digest_of("nwr/amenity=cafe") != tq.digest_of("nwr/amenity=bar")
    a = tq.query_rel_path("europe/germany", tq.digest_of("nwr/amenity=cafe"))
    b = tq.query_rel_path("europe/germany", tq.digest_of("nwr/amenity=bar"))
    assert a != b, "two questions must not share a cache entry"


@pytest.mark.parametrize("expr", ["nwr", "n/", "", "   ", "amenity=pharmacy", "x/foo=bar"])
def test_a_filter_that_is_not_a_filter_is_refused(expr):
    """A bare object-type term matches EVERYTHING — on a continent that is a
    multi-hour rewrite of the whole extract wearing a query's clothes. It is a
    mistake, not a request."""
    with pytest.raises(ExtractionError):
        tq.validate(expr)


@pytest.mark.parametrize("expr", [
    "nwr/man_made=surveillance",
    "r/boundary=protected_area",
    "nwr/amenity=pharmacy,doctors",
    "nwr/man_made",
    "w/highway=motorway n/amenity=fuel",
])
def test_real_filter_shapes_are_accepted(expr):
    assert tq.validate(expr)


def test_a_missing_source_names_both_places_it_looked(tmp_path, monkeypatch):
    """Neither the cache nor the local trees have it — the message has to say so,
    because "which of the two do I need to fix?" is the whole question."""
    monkeypatch.setattr(sidecar, "read_sidecar", lambda *a, **k: None)
    with pytest.raises(ExtractionError, match="download cache.*FW_OSM_LOCAL_EXTRACTS"):
        tq.query_region("nowhere/at-all", "nwr/amenity=cafe")


def test_a_local_tree_is_searched_when_the_cache_misses(tmp_path, monkeypatch):
    """The self-hosted planet and continents are NOT in `cache/osm/pbf/` — if the
    resolver only looked there, the biggest sources on the machine would be the
    ones a query cannot reach."""
    root = tmp_path / "selfhost"
    (root / "north-america" / "us").mkdir(parents=True)
    flat = root / "europe-latest.osm.pbf"
    flat.write_bytes(b"x" * 10)
    nested = root / "north-america" / "us" / "utah-latest.osm.pbf"
    nested.write_bytes(b"y" * 20)

    monkeypatch.setattr(sidecar, "read_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(tq, "LOCAL_EXTRACT_ROOTS", str(root))
    monkeypatch.setattr(tq, "_local_roots", lambda: [root])

    # a continent, sitting flat in the root the way planet_bootstrap writes it
    path, side = tq.resolve_source("europe")
    assert path == flat and side["size_bytes"] == 10
    assert side["sha256"].startswith("mtime:"), "no content hash for an 87 GB file"

    # and a nested region key
    path2, _ = tq.resolve_source("north-america/us/utah")
    assert path2 == nested


def test_the_download_cache_wins_over_a_local_tree(tmp_path, monkeypatch):
    """The cached copy carries a real digest, which is what freshness is judged
    on; a local file only has size and mtime."""
    root = tmp_path / "selfhost"
    root.mkdir()
    (root / "europe-latest.osm.pbf").write_bytes(b"local")
    cached = tmp_path / "cached-europe.osm.pbf"
    cached.write_bytes(b"cached")

    monkeypatch.setattr(sidecar, "read_sidecar",
                        lambda ns, ct, rel, s=None: {"sha256": "real", "size_bytes": 6})
    monkeypatch.setattr(tq, "pbf_abs_path", lambda region, s=None: cached)
    monkeypatch.setattr(tq, "_local_roots", lambda: [root])

    path, side = tq.resolve_source("europe")
    assert path == cached and side["sha256"] == "real"


# --- end to end with the real binary ---------------------------------------

SOURCE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
 <node id="1" version="1" lat="50.0" lon="8.0">
  <tag k="man_made" v="surveillance"/>
 </node>
 <node id="2" version="1" lat="50.1" lon="8.1">
  <tag k="amenity" v="cafe"/>
 </node>
 <node id="3" version="1" lat="50.2" lon="8.2">
  <tag k="man_made" v="surveillance"/>
  <tag k="surveillance:type" v="ALPR"/>
 </node>
</osm>
"""


@pytest.mark.skipif(shutil.which("osmium") is None, reason="osmium-tool not installed")
def test_end_to_end_query_and_cache(tmp_path, monkeypatch):
    """A question no curated category covers, answered from a local file."""
    src_xml = tmp_path / "src.osm"
    src_xml.write_text(SOURCE_XML)
    src_pbf = tmp_path / "region-latest.osm.pbf"
    subprocess.run(["osmium", "cat", "-o", str(src_pbf), str(src_xml)], check=True,
                   capture_output=True)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    written: dict = {}

    class _Storage:
        def finalize_from_local(self, local, dest):
            shutil.copyfile(local, dest)

    monkeypatch.setattr(tq, "get_storage", lambda: _Storage())
    monkeypatch.setattr(tq, "pbf_abs_path", lambda region, s=None: src_pbf)
    monkeypatch.setattr(sidecar, "read_sidecar",
                        lambda ns, ct, rel, s=None: (
                            {"sha256": "srcsha", "size_bytes": 1}
                            if ct == "pbf" else written.get(rel)))
    monkeypatch.setattr(sidecar, "cache_path",
                        lambda ns, ct, rel, s=None: str(out_dir / rel))
    monkeypatch.setattr(sidecar, "write_sidecar",
                        lambda ns, ct, rel, **kw: written.setdefault(
                            rel, {"source": {"sha256": "srcsha"},
                                  "size_bytes": kw["size_bytes"],
                                  "extra": kw["extra"]}))

    cold = tq.query_region("region", "nwr/man_made=surveillance")
    assert cold.feature_count == 2, "both surveillance nodes, and not the cafe"
    assert cold.was_cached is False
    assert cold.digest in cold.relative_path

    warm = tq.query_region("region", "  nwr/man_made=surveillance  ")
    assert warm.was_cached is True and warm.feature_count == 2
    assert warm.duration_seconds == 0.0

    other = tq.query_region("region", "nwr/amenity=cafe")
    assert other.was_cached is False, "a different question must not hit that cache"
    assert other.feature_count == 1


# --- the CLI ---------------------------------------------------------------


def test_cli_where_expresses_the_and_osmium_cannot(tmp_path, monkeypatch):
    """`man_made=surveillance` AND `surveillance:type=ALPR` is not expressible in
    one osmium filter, so the CLI post-filters the produced GeoJSON — one pass
    over the matches, not a second pass over the extract."""
    import importlib, sys as _sys
    _sys.path.insert(0, str(__import__("pathlib").Path(tq.__file__).resolve().parents[1]))
    cli = importlib.import_module("query_osm")

    seq = tmp_path / "out.geojsonseq"
    seq.write_text(
        '{"type":"Feature","properties":{"man_made":"surveillance","surveillance:type":"ALPR"}}\n'
        '{"type":"Feature","properties":{"man_made":"surveillance"}}\n'
        '{"type":"Feature","properties":{"man_made":"surveillance","surveillance:type":"alpr"}}\n'
    )
    n, hits = cli._post_filter(str(seq), "surveillance:type=ALPR")
    assert n == 2, "case-insensitive on the value, and the untagged one excluded"
    assert len(hits) == 2


def test_cli_rejects_a_malformed_where(tmp_path):
    import importlib
    cli = importlib.import_module("query_osm")
    seq = tmp_path / "out.geojsonseq"
    seq.write_text("")
    with pytest.raises(ExtractionError, match="key=value"):
        cli._post_filter(str(seq), "surveillance:type")
