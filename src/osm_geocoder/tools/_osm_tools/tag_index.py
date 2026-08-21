# SPDX-License-Identifier: Apache-2.0
"""An incrementally-maintained index of tag-matching nodes.

``tag_query`` answers an arbitrary question by scanning an extract. That is the
right shape for a one-off, and the wrong shape for a question asked repeatedly:
the worldwide ALPR set is ~336k nodes, and finding them means reading 93 GB of
extracts — about 45 minutes on this deployment's disk. Overpass answers the same
question in seconds because it is INDEXED, which is the only reason it stayed
the primary source after the extracts were made current.

This closes that gap. Build the index once by scanning, then keep it current
from the replication diffs the nightly publish already downloads — a day's diff
is 83 MB, so the update is seconds rather than a rescan of the world.

**Why the index is keyed by node id, and why every changed node is inspected.**
A change file carries creates, modifies AND deletes, and an OSM delete carries
NO TAGS — so you cannot tell from the diff whether the node being deleted was
one of yours. The same applies to a node whose tag is simply removed: it appears
as an ordinary modify that no longer matches. If updates only added matching
nodes, the index would accumulate cameras that no longer exist and never lose
them, drifting further from reality every night while looking healthy. So the
rule is per-id and total:

    deleted            -> remove the id
    visible + matches  -> upsert the id
    visible + no match -> remove the id (it may have matched before)

The last line is the one that is easy to omit and impossible to notice.

Sequence tracking mirrors the replication design: the index records the
sequence it has consumed, refuses to apply a diff out of order, and refuses to
skip one — an index silently missing a day is worse than one that stops.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

INDEX_ROOT_ENV = "FW_OSM_INDEX_ROOT"
DEFAULT_INDEX_ROOT = "/tmp/fw_osm_indexes"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
    id   INTEGER PRIMARY KEY,
    lon  REAL NOT NULL,
    lat  REAL NOT NULL,
    tags TEXT NOT NULL
);
"""


class IndexError_(RuntimeError):
    """Raised when an index cannot be built or advanced."""


@dataclass
class IndexStats:
    name: str
    sequence: int | None
    count: int
    expression: str
    updated_at: str = ""


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_root() -> Path:
    return Path(os.environ.get(INDEX_ROOT_ENV) or DEFAULT_INDEX_ROOT)


def index_path(name: str) -> Path:
    return index_root() / f"{name}.sqlite"


def _connect(name: str) -> sqlite3.Connection:
    p = index_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    return con


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------------------
# The match predicate.
# ---------------------------------------------------------------------------


def parse_spec(spec: str) -> list[tuple[str, str | None]]:
    """``"surveillance:type=ALPR"`` -> ``[("surveillance:type", "ALPR")]``.

    Comma-separated terms are OR-ed, and a bare key matches any value — the
    same shape as osmium's ``tags-filter``, restricted to what an index can
    answer without ambiguity. Deliberately NOT the full expression language:
    the same spec has to drive both the initial osmium scan and the per-node
    predicate used on updates, and two implementations of a rich grammar would
    drift, indexing one set and maintaining another.
    """
    terms: list[tuple[str, str | None]] = []
    for raw in spec.split(","):
        t = raw.strip()
        if not t:
            continue
        if "=" in t:
            k, v = t.split("=", 1)
            terms.append((k.strip(), v.strip()))
        else:
            terms.append((t, None))
    if not terms:
        raise IndexError_(f"empty tag spec: {spec!r}")
    return terms


def matches(tags: dict[str, Any], terms: list[tuple[str, str | None]]) -> bool:
    for key, val in terms:
        got = tags.get(key)
        if got is None:
            continue
        if val is None or str(got) == val:
            return True
    return False


def osmium_filter(terms: list[tuple[str, str | None]]) -> list[str]:
    """The same predicate as an osmium ``tags-filter`` argument list."""
    return [f"n/{k}={v}" if v is not None else f"n/{k}" for k, v in terms]


# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------


def build(
    name: str,
    spec: str,
    sources: list[Path],
    *,
    sequence: int | None = None,
    osmium_bin: str | None = None,
) -> IndexStats:
    """Scan extracts once and populate the index.

    The scan runs in osmium (C++), not by iterating in Python: the input is
    tens of GB and only the tiny matching subset is worth materialising here.

    ``sequence`` is the replication sequence the SOURCES are at. It is required
    for the index to be maintainable — without it there is no way to know which
    diff to apply next — and it is not inferred, for the same reason the
    replication anchor is not: a wrong baseline silently skips or repeats days.
    """
    from .replication_publish import osmium_bin_resolve

    ob = osmium_bin or osmium_bin_resolve()
    terms = parse_spec(spec)
    con = _connect(name)
    try:
        con.execute("DELETE FROM nodes")
        total = 0
        for src in sources:
            if not src.exists():
                raise IndexError_(f"no such extract: {src}")
            feats = _export_matching(src, terms, ob)
            rows = []
            for f in feats:
                geom = f.get("geometry") or {}
                if geom.get("type") != "Point":
                    continue
                raw_id = str(f.get("id") or "")
                osm_id = raw_id[1:] if raw_id[:1] in "nwr" else raw_id
                if not osm_id.isdigit():
                    continue
                lon, lat = geom["coordinates"][0], geom["coordinates"][1]
                rows.append((int(osm_id), lon, lat, json.dumps(f.get("properties") or {})))
            # INSERT OR REPLACE, because regional extracts are cut with a buffer
            # past their polygon: a node near a seam is genuinely present in two
            # of them, and a plain INSERT would abort the build on the duplicate.
            con.executemany(
                "INSERT OR REPLACE INTO nodes(id, lon, lat, tags) VALUES(?,?,?,?)", rows)
            total += len(rows)
            log.info("indexed %d from %s", len(rows), src.name)
        _meta_set(con, "expression", spec)
        _meta_set(con, "sources", json.dumps([str(p) for p in sources]))
        _meta_set(con, "updated_at", _now_iso())
        if sequence is not None:
            _meta_set(con, "sequence", str(sequence))
        con.commit()
        count = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        return IndexStats(name, sequence, count, spec)
    finally:
        con.close()


def update_from_diff(name: str, diff: Path, sequence: int) -> tuple[int, int]:
    """Apply one replication diff to the index. Returns (upserted, removed).

    Every changed node is inspected, not just the matching ones — see the module
    docstring. An OSM delete carries no tags, and an untagged node arrives as an
    ordinary modify, so the only way to keep the index honest is to decide the
    fate of each id that appears.
    """
    import osmium

    con = _connect(name)
    try:
        spec = _meta_get(con, "expression")
        if not spec:
            raise IndexError_(f"index {name!r} has no expression — build it first")
        terms = parse_spec(spec)

        have = _meta_get(con, "sequence")
        if have is None:
            raise IndexError_(f"index {name!r} has no sequence — rebuild with one")
        have_i = int(have)
        if sequence <= have_i:
            return 0, 0  # already applied; re-applying is a no-op, not an error
        if sequence != have_i + 1:
            # Refuse to skip. An index quietly missing a day keeps serving,
            # looks healthy, and is wrong — the failure this project keeps
            # meeting in other forms.
            raise IndexError_(
                f"index {name!r} is at {have_i}; refusing to jump to {sequence} "
                f"and skip {have_i + 1}..{sequence - 1}"
            )

        # LAST operation per id wins, in file order.
        #
        # A change file may carry SEVERAL versions of the same node — created
        # then deleted, or edited twice, within one day. Collecting all removals
        # and all upserts into two batches and running removals-then-upserts
        # reorders those: a node upserted early and DELETED later would have the
        # delete applied first and the insert second, leaving a camera in the
        # index that no longer exists. The real planet diff showed the symptom —
        # 1325 upserts producing 1256 rows — which is what prompted looking.
        #
        # A dict keyed by id, written in file order, keeps the last word and is
        # also fewer statements than the two-batch form.
        ops: dict[int, tuple | None] = {}

        class _H(osmium.SimpleHandler):
            def node(self, n):
                if not n.visible:
                    ops[n.id] = None  # remove
                    return
                tags = {t.k: t.v for t in n.tags}
                if matches(tags, terms):
                    ops[n.id] = (n.id, n.location.lon, n.location.lat, json.dumps(tags))
                else:
                    # May have matched before this edit. Removing an id that is
                    # not present is a no-op, so this is safe and necessary.
                    ops[n.id] = None

        _H().apply_file(str(diff))

        upserts = [v for v in ops.values() if v is not None]
        removals = [(k,) for k, v in ops.items() if v is None]
        con.executemany("DELETE FROM nodes WHERE id = ?", removals)
        con.executemany(
            "INSERT OR REPLACE INTO nodes(id, lon, lat, tags) VALUES(?,?,?,?)", upserts)
        _meta_set(con, "sequence", str(sequence))
        # Wall clock, not the diff's timestamp. A consumer's real question is
        # "has the nightly been running?", and a sequence alone cannot answer
        # it: an index stuck three weeks ago still reports a plausible-looking
        # number. Together they say both how far it got and when it last moved.
        _meta_set(con, "updated_at", _now_iso())
        con.commit()
        return len(upserts), len(removals)
    finally:
        con.close()


def stats(name: str) -> IndexStats:
    con = _connect(name)
    try:
        seq = _meta_get(con, "sequence")
        return IndexStats(
            name=name,
            sequence=int(seq) if seq is not None else None,
            count=con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            expression=_meta_get(con, "expression") or "",
            updated_at=_meta_get(con, "updated_at") or "",
        )
    finally:
        con.close()


def export_geojson(name: str, out: Path) -> int:
    """Write the index as a GeoJSON FeatureCollection. Returns the count."""
    con = _connect(name)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".part")
        n = 0
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write('{"type":"FeatureCollection","features":[')
            first = True
            for oid, lon, lat, tags in con.execute(
                    "SELECT id, lon, lat, tags FROM nodes ORDER BY id"):
                if not first:
                    fh.write(",")
                first = False
                fh.write(json.dumps({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {**json.loads(tags), "osm_id": oid, "osm_type": "node"},
                }, separators=(",", ":")))
                n += 1
            fh.write("]}")
        tmp.replace(out)
        return n
    finally:
        con.close()


def _export_matching(src: Path, terms, osmium_bin: str) -> list[dict]:
    """Filter one extract to matching nodes and return them as GeoJSON dicts."""
    staging = Path(tempfile.mkdtemp(prefix="tagidx-"))
    try:
        filtered = staging / "m.osm.pbf"
        seq_out = staging / "m.geojsonseq"
        subprocess.run(
            [osmium_bin, "tags-filter", "--overwrite", "-o", str(filtered),
             str(src), *osmium_filter(terms)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [osmium_bin, "export", "-f", "geojsonseq", "--geometry-types=point",
             "-o", str(seq_out), "--overwrite", "--add-unique-id=type_id", str(filtered)],
            check=True, capture_output=True, text=True,
        )
        out = []
        for line in seq_out.read_text(encoding="utf-8").splitlines():
            line = line.strip("\x1e \t\r\n")
            if line:
                out.append(json.loads(line))
        return out
    finally:
        shutil.rmtree(staging, ignore_errors=True)
