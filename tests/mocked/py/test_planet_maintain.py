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


def _seq_of(pbf):
    """Replication sequence in a PBF header, read independently of the module."""
    import osmium.replication as _r
    return _r.get_replication_header(str(pbf)).sequence


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
        assert r.replication_url == f"{BASE_URL}/{key}-updates"
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


def test_maintain_refuses_to_move_regions_backwards(tmp_path):
    """A stale master must not overwrite newer published extracts.

    The regression this guards: on 2026-08-23 the master sat at sequence 5051
    with NO replication header while the served tree was at 5090, kept current
    by the per-region diff publisher. `update_master` cannot advance a
    headerless master and deliberately never raises, so the nightly run would
    have re-extracted every region 39 days BACKWARDS and stranded the published
    5091+ diffs off a base they no longer apply to.
    """
    master = _make_master(tmp_path)          # sequence 500
    out = tmp_path / "out"
    regions = [{"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]}]

    # First cycle publishes at the master's sequence.
    pm.maintain(master=master, out=str(out), regions=regions,
                base_url=BASE_URL, strategy="simple")
    assert _seq_of(out / "demo/west-latest.osm.pbf") == MASTER_SEQ

    # Now the served tree moves AHEAD of the master, as the diff publisher does.
    ahead = MASTER_SEQ + 40
    subprocess.run(
        ["osmium", "cat", str(out / "demo/west-latest.osm.pbf"),
         "-o", str(out / "demo/west-latest.osm.pbf"), "--overwrite",
         f"--output-header=osmosis_replication_base_url={BASE_URL}/demo/west-updates",
         f"--output-header=osmosis_replication_sequence_number={ahead}",
         "--output-header=osmosis_replication_timestamp=2026-02-10T00:00:00Z"],
        check=True,
    )
    assert _seq_of(out / "demo/west-latest.osm.pbf") == ahead

    with pytest.raises(pm.BootstrapError) as e:
        pm.maintain(master=master, out=str(out), regions=regions,
                    base_url=BASE_URL, strategy="simple")
    msg = str(e.value)
    assert "refusing to re-extract" in msg
    assert str(ahead) in msg and str(MASTER_SEQ) in msg

    # And the published extract is untouched — the point of refusing.
    assert _seq_of(out / "demo/west-latest.osm.pbf") == ahead


def test_maintain_refuses_when_master_has_no_replication_header(tmp_path):
    """The exact shape found in production: a raw planet dump, never stamped."""
    xml = tmp_path / "src.osm"
    xml.write_text(SOURCE_XML)
    bare = tmp_path / "bare.osm.pbf"
    subprocess.run(["osmium", "cat", str(xml), "-o", str(bare), "--overwrite"], check=True)

    out = tmp_path / "out"
    regions = [{"key": "demo/west", "bbox": [0.0, 0.0, 0.5, 1.0]}]
    # Seed a published tree from a properly-stamped master.
    pm.maintain(master=_make_master(tmp_path), out=str(out), regions=regions,
                base_url=BASE_URL, strategy="simple")

    with pytest.raises(pm.BootstrapError) as e:
        pm.maintain(master=str(bare), out=str(out), regions=regions,
                    base_url=BASE_URL, strategy="simple")
    assert "NO replication header" in str(e.value)


def test_advance_yields_an_int_sequence_not_a_tuple(tmp_path, monkeypatch):
    """pyosmium's apply_diffs_to_file returns (id, timestamp), not a bare id.

    Storing the tuple was invisible while `new_sequence` was only f-stringed
    into a log or JSON-dumped. The first code to COMPARE it — the
    behind-the-served-tree guard — died with "'<' not supported between
    instances of 'tuple' and 'int'", and only AFTER spending ~50 minutes
    applying the diff, so a whole run was lost to it.
    """
    from osmium.replication.server import ReplicationServer

    master = _make_master(tmp_path)          # header says sequence 500

    class _State:
        sequence = MASTER_SEQ + 5
        timestamp = None

    monkeypatch.setattr(ReplicationServer, "get_state_info", lambda self: _State())
    # Mimic the real return shape, and actually produce the output file so the
    # os.replace() succeeds.
    def _apply(self, infile, outfile, start_id, max_size=0):
        shutil.copyfile(infile, outfile)
        return (MASTER_SEQ + 5, "2026-02-01T00:00:00Z")
    monkeypatch.setattr(ReplicationServer, "apply_diffs_to_file", _apply)

    upd = pm.update_master(master, max_diff_mb=1)

    assert upd.advanced is True
    assert upd.new_sequence == MASTER_SEQ + 5
    assert isinstance(upd.new_sequence, int), f"tuple leaked out: {upd.new_sequence!r}"
    # The comparison that used to explode must now just work.
    assert upd.new_sequence > MASTER_SEQ
