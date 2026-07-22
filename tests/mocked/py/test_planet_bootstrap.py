"""Integration test for the Phase 1 planet-bootstrap tool (Strategy A).

Builds a tiny source PBF (stamped with a source replication header), splits it
into two bbox sub-regions with the real ``osmium extract`` pipeline, and asserts
each output carries OUR replication header so the delta path would follow our own
server. Skipped when the ``osmium`` binary or pyosmium isn't available.
"""
import json
import shutil
import subprocess

import pytest

pb = pytest.importorskip("osm_geocoder.tools._osm_tools.planet_bootstrap")
pytest.importorskip("osmium.replication")

pytestmark = pytest.mark.skipif(
    shutil.which("osmium") is None, reason="osmium-tool binary not installed"
)

SOURCE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
 <bounds minlat="0" minlon="0" maxlat="1" maxlon="1"/>
 <node id="1" lat="0.50" lon="0.25" version="1"/>
 <node id="2" lat="0.40" lon="0.20" version="1"/>
 <node id="3" lat="0.50" lon="0.75" version="1"/>
 <node id="4" lat="0.60" lon="0.80" version="1"/>
</osm>
"""

BASE_URL = "http://server3.local:8080/osm"
SRC_SEQ = 1000


def _make_source(tmp_path):
    """A tiny PBF carrying a source replication header (seq=SRC_SEQ)."""
    xml = tmp_path / "src.osm"
    xml.write_text(SOURCE_XML)
    pbf = tmp_path / "planet.osm.pbf"
    subprocess.run(
        ["osmium", "cat", str(xml), "-o", str(pbf), "--overwrite",
         "--output-header=osmosis_replication_base_url=http://upstream/replication",
         f"--output-header=osmosis_replication_sequence_number={SRC_SEQ}",
         "--output-header=osmosis_replication_timestamp=2026-01-01T00:00:00Z"],
        check=True,
    )
    return str(pbf)


def test_bootstrap_splits_and_stamps_our_header(tmp_path):
    from osmium.replication import get_replication_header

    source = _make_source(tmp_path)
    out = tmp_path / "out"
    regions = [
        {"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]},
        {"key": "demo/east", "bbox": [0.5, 0.0, 1.0, 1.0]},
    ]

    results = pb.bootstrap(
        source=source, out=str(out), regions=regions,
        base_url=BASE_URL, strategy="simple",
    )

    assert len(results) == 2
    by_key = {r.key: r for r in results}

    for key in ("demo/west", "demo/east"):
        r = by_key[key]
        # Geofabrik-style layout emitted
        assert (out / f"{key}-latest.osm.pbf").exists()
        state = (out / f"{key}-updates" / "state.txt").read_text()
        assert f"sequenceNumber={SRC_SEQ}" in state
        # each region inherits the source's baseline sequence
        assert r.sequence == SRC_SEQ
        assert r.header_ok is True
        assert r.replication_url == f"{BASE_URL}/replication/{key}"
        # round-trip through the SAME reader the delta path uses
        h = get_replication_header(r.path)
        assert h.url == f"{BASE_URL}/replication/{key}"
        assert h.sequence == SRC_SEQ
        assert r.nodes >= 2  # two nodes fall in each half

    # the split actually partitioned the nodes (not everything into one side)
    assert by_key["demo/west"].nodes == 2
    assert by_key["demo/east"].nodes == 2


def test_bad_region_spec_raises(tmp_path):
    source = _make_source(tmp_path)
    with pytest.raises(pb.BootstrapError):
        pb.bootstrap(
            source=source, out=str(tmp_path / "o"),
            regions=[{"key": "demo/x"}],  # neither bbox nor poly
            base_url=BASE_URL, strategy="simple",
        )


def test_cli_smoke(tmp_path, capsys):
    """The thin CLI wires args -> bootstrap and emits JSON on stdout."""
    from osm_geocoder.tools import planet_bootstrap as cli

    source = _make_source(tmp_path)
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps([{"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]}]))
    rc = cli.main([
        "--source", source, "--out", str(tmp_path / "out"),
        "--regions", str(regions), "--base-url", BASE_URL, "--strategy", "simple",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["regions"][0]["replication_url"] == f"{BASE_URL}/replication/demo/west"
