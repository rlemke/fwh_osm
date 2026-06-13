"""Regression tests for the GraphHopper build library.

These cover the GraphHopper 8.x realities the mocked workflow tests miss:

- node/edge counts come from the import **log**, not the on-disk ``properties``
  file (GH8 writes it as binary, so the old ``read_graph_stats`` grep returned 0
  and falsely rejected a perfectly good graph);
- the flush-line decoy ``nodes:9,edges:21`` (no space after the colon) must NOT
  win the parse over the real ``GraphHopper: nodes: 134,737, edges: 163,794``;
- a ``build_graph`` local-backend round-trip (build -> cache hit) where the
  counts come from the log and are persisted in / re-read from the sidecar.
"""

import os
from pathlib import Path

from osm_geocoder.handlers.shared.pbf_convert import graphhopper as gb
from _osm_tools import sidecar
from _osm_tools.storage import get_storage

# A representative slice of real GraphHopper 8.0 ``import`` stdout, including the
# binary-flush decoy line that previously hijacked the parse.
GH8_LOG = """\
INFO  com.graphhopper.reader.osm.OSMReader: Finished reading OSM file: hawaii.pbf, nodes: 134,737, edges: 163,794, zero distance edges: 2,548
INFO  com.graphhopper.routing.subnetwork.PrepareRoutingSubnetworks: car - Marked 66550 subnetworks
INFO  com.graphhopper.GraphHopper: nodes: 134,737, edges: 163,794
INFO  com.graphhopper.GraphHopper: flushing graph car|RAM_STORE|2D|no_turn_cost|nodes:9,edges:21,geometry:6,location_index:5 details:edges: 163,794(6MB), nodes: 134,737(2MB)
INFO  com.graphhopper.GraphHopper: flushed graph totalMB:1024
"""


def test_counts_from_log_prefers_summary_over_flush_decoy():
    # Must pick the GraphHopper summary line, not the "nodes:9,edges:21" flush.
    assert gb._counts_from_log(GH8_LOG) == (134737, 163794)


def test_counts_from_log_absent():
    assert gb._counts_from_log("") == (0, 0)
    assert gb._counts_from_log("nothing parseable here") == (0, 0)
    # the bare flush decoy alone (no space after colon) must not be accepted
    assert gb._counts_from_log("...nodes:9,edges:21,geometry:6...") == (0, 0)


def test_build_graph_local_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("AFL_STORAGE", "local")
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
    s = get_storage()
    region, profile = "test/region", "car"

    # Stage a fake source PBF + sidecar, as osm.cache.Download would.
    pbf = gb.pbf_abs_path(region, s)
    Path(os.path.dirname(str(pbf))).mkdir(parents=True, exist_ok=True)
    Path(pbf).write_bytes(b"fake-pbf-bytes")
    sidecar.write_sidecar(
        "osm", "pbf", gb.pbf_rel_path(region),
        kind="file", size_bytes=14, sha256="deadbeef",
        source={"url": "http://example/region.osm.pbf"},
        generated_at=sidecar.utcnow_iso(), storage=s,
    )

    def fake_import(osm_path, graph_dir, profile, **kw):
        assert Path(osm_path).exists(), "java must receive a local pbf path"
        g = Path(graph_dir)
        g.mkdir(parents=True, exist_ok=True)
        (g / "nodes").write_bytes(b"N" * 64)
        (g / "edges").write_bytes(b"E" * 64)
        (g / "properties").write_bytes(b"\x00\x80binary-gh8-properties")  # unparseable
        return GH8_LOG

    monkeypatch.setattr(gb, "_run_import", fake_import)

    r = gb.build_graph(region, profile, force=True)
    assert not r.was_cached
    # counts come from the log, NOT the binary properties file
    assert (r.node_count, r.edge_count) == (134737, 163794)
    assert Path(gb.graph_abs_path(region, profile, s) / "nodes").exists()

    # cache hit reads the counts back from the sidecar (not the binary file)
    r2 = gb.build_graph(region, profile, force=False)
    assert r2.was_cached
    assert (r2.node_count, r2.edge_count) == (134737, 163794)


def test_validate_graph_handler_uses_carried_counts_and_storage(monkeypatch, tmp_path):
    # ValidateGraph must not depend on parsing the binary GH8 properties file:
    # it trusts the counts carried on the cache and confirms existence via storage.
    monkeypatch.setenv("AFL_STORAGE", "local")
    monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
    from osm_geocoder.handlers.graphhopper.graphhopper_handlers import (
        validate_graph_handler,
    )

    gdir = tmp_path / "graph" / "car"
    gdir.mkdir(parents=True)
    (gdir / "nodes").write_bytes(b"N" * 32)
    cache = {"graphDir": str(gdir), "nodeCount": 134737, "edgeCount": 163794}
    out = validate_graph_handler({"graph": cache})
    assert out == {"valid": True, "nodeCount": 134737, "edgeCount": 163794}

    missing = {"graphDir": str(tmp_path / "nope" / "car"), "nodeCount": 0, "edgeCount": 0}
    assert validate_graph_handler({"graph": missing})["valid"] is False
