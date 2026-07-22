"""Tests for the Phase 2 maintenance loop (Strategy A).

update_master degrades gracefully when replication is unreachable, and maintain()
still re-extracts the regions at the master's current sequence (stamping our
replication header). Skipped without the osmium binary / pyosmium.
"""
import json
import shutil
import subprocess

import pytest

pm = pytest.importorskip("osm_geocoder.tools._osm_tools.planet_maintain")
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
</osm>
"""

BASE_URL = "http://server3.local:8080/osm"
MASTER_SEQ = 500
# RFC 2606 reserved TLD — guaranteed not to resolve, so update_master fails fast.
UNREACHABLE = "http://replication.invalid/planet"


def _make_master(tmp_path):
    xml = tmp_path / "src.osm"
    xml.write_text(SOURCE_XML)
    master = tmp_path / "master.osm.pbf"
    subprocess.run(
        ["osmium", "cat", str(xml), "-o", str(master), "--overwrite",
         f"--output-header=osmosis_replication_base_url={UNREACHABLE}",
         f"--output-header=osmosis_replication_sequence_number={MASTER_SEQ}",
         "--output-header=osmosis_replication_timestamp=2026-01-01T00:00:00Z"],
        check=True,
    )
    return str(master)


def test_update_master_unreachable_is_graceful(tmp_path):
    master = _make_master(tmp_path)
    before = __import__("os").path.getsize(master)

    upd = pm.update_master(master, max_diff_mb=1)

    assert upd.advanced is False
    assert upd.status.startswith("unreachable")
    assert upd.old_sequence == MASTER_SEQ and upd.new_sequence == MASTER_SEQ
    # master left untouched when replication can't be reached
    assert __import__("os").path.getsize(master) == before


def test_maintain_reextracts_at_master_seq(tmp_path):
    master = _make_master(tmp_path)
    out = tmp_path / "out"
    regions = [
        {"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]},
        {"key": "demo/east", "bbox": [0.5, 0.0, 1.0, 1.0]},
    ]

    res = pm.maintain(master=master, out=str(out), regions=regions,
                      base_url=BASE_URL, strategy="simple")

    # master update degraded (unreachable) but the cycle still re-extracts
    assert res.master.advanced is False
    assert len(res.regions) == 2
    by_key = {r.key: r for r in res.regions}
    for key in ("demo/west", "demo/east"):
        r = by_key[key]
        assert r.sequence == MASTER_SEQ            # regions inherit the master's seq
        assert r.header_ok is True
        assert r.replication_url == f"{BASE_URL}/replication/{key}"
        assert (out / f"{key}-latest.osm.pbf").exists()
        assert f"sequenceNumber={MASTER_SEQ}" in (out / f"{key}-updates" / "state.txt").read_text()


def test_cli_smoke(tmp_path, capsys):
    from osm_geocoder.tools import planet_maintain as cli

    master = _make_master(tmp_path)
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps([{"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]}]))
    rc = cli.main([
        "--master", master, "--out", str(tmp_path / "out"),
        "--regions", str(regions), "--base-url", BASE_URL, "--strategy", "simple",
        "--max-diff-mb", "1",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["master"]["advanced"] is False
    assert payload["regions"][0]["sequence"] == MASTER_SEQ
