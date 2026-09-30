"""Download and delta-update the OSM planet (master for self-hosted extracts).

Shared library behind the ``download_planet.py`` tool and the ``osm.planet``
handlers. Fetches ``planet-latest.osm.pbf`` from a planet mirror (resumable,
md5-verified) and keeps it current by applying replication diffs.

The planet dump exposes a replication TIMESTAMP but — unlike Geofabrik extracts —
no base_url/sequence in its PBF header, so :func:`update_planet` derives the start
sequence from that timestamp against the planet replication server
(``planet.openstreetmap.org/replication/<granularity>/``). ``apply_diffs_to_file``
streams the merge (osmium under the hood), so it scales to planet size on modest
RAM — the cost is I/O (read+write the planet), not memory.

planet.openstreetmap.org is reachable from the fleet (unlike the IP-banned
Geofabrik host), so this is the durable master source.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import urllib.request
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import osmium.replication as _repl
from osmium.replication.server import ReplicationServer

from .cancellation import raise_if_cancelled, run_cancellable

PLANET_MIRROR = os.environ.get("FW_PLANET_MIRROR", "https://planet.openstreetmap.org/pbf").rstrip("/")
PLANET_FILE = "planet-latest.osm.pbf"
# Replication base for delta updates. "day" granularity keeps catch-up cheap
# (one diff/day) for a master re-extracted on a daily-ish schedule.
PLANET_REPLICATION = os.environ.get(
    "FW_PLANET_REPLICATION", "https://planet.openstreetmap.org/replication/day"
).rstrip("/")


class PlanetError(RuntimeError):
    """A planet fetch/update step failed (download, md5 mismatch, apply)."""


@dataclass
class PlanetFetch:
    path: str
    size_bytes: int
    md5: str | None
    was_cached: bool


@dataclass
class PlanetUpdate:
    status: str          # "updated" | "already current" | "unreachable: X" | "no timestamp" | ...
    old_timestamp: str | None
    new_sequence: int | None
    advanced: bool


def _md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise PlanetError(f"required binary not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise PlanetError(f"command failed ({exc.returncode}): {' '.join(cmd[:3])}…") from exc


def fetch_planet(dest: str, *, mirror: str = PLANET_MIRROR, verify: bool = True,
                 force: bool = False, on_log: Callable[[str], None] | None = None) -> PlanetFetch:
    """Download ``planet-latest.osm.pbf`` to ``dest`` (resumable + md5-verified).

    Uses ``curl -C -`` so an interrupted transfer resumes from its offset rather
    than restarting the ~80 GB download. Reuses an existing file whose md5 already
    matches (unless ``force``).
    """
    log = on_log or (lambda _m: None)
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    url = f"{mirror}/{PLANET_FILE}"

    expected = None
    if verify:
        try:
            expected = urllib.request.urlopen(url + ".md5", timeout=30).read().decode().split()[0]
        except Exception:  # md5 unavailable — anchor on size/download success
            expected = None

    if dest_p.exists() and not force and expected and _md5(str(dest_p)) == expected:
        log(f"planet already present + md5 OK ({dest_p.stat().st_size} bytes)")
        return PlanetFetch(str(dest_p), dest_p.stat().st_size, expected, True)

    log(f"downloading planet from {url} (resumable)")
    # Hold a fleet-wide download slot: the whole fleet shares one egress IP, and
    # this is the largest single GET the system makes. The gate is a no-op when
    # FW_MONGODB_URL is unset, so CLI and offline tests are unaffected.
    from .download_gate import download_slot
    with download_slot():
        _run(["curl", "-L", "-C", "-", "--retry", "5", "--retry-delay", "10",
              "-o", str(dest_p), url])

    actual = _md5(str(dest_p)) if verify else None
    if expected and actual and actual != expected:
        raise PlanetError(f"planet md5 mismatch: got {actual}, expected {expected}")
    log(f"planet downloaded ({dest_p.stat().st_size} bytes)" + (" md5 OK" if expected else ""))
    return PlanetFetch(str(dest_p), dest_p.stat().st_size, expected, False)


def update_planet(planet_path: str, *, replication: str = PLANET_REPLICATION,
                  max_diff_mb: int = 4096, on_log: Callable[[str], None] | None = None) -> PlanetUpdate:
    """Advance the planet in place by applying replication diffs.

    The start point is the header's replication SEQUENCE when it has one, else it
    is derived from the header's timestamp via ``timestamp_to_sequence``. Either
    is enough: a planet written by an earlier update carries a sequence and may
    carry NO timestamp (measured 2026-09-30: seq 5120, empty timestamp), and
    requiring the timestamp made this return "no timestamp" without advancing --
    as a normal status, so RefreshChain would have re-cut every continent from an
    11-day-old planet and reported success. Never raises on a flaky/unreachable
    replication host —
    returns a ``status`` and leaves the planet untouched, so a scheduled run
    degrades to "re-extract at the current snapshot" instead of failing.
    """
    log = on_log or (lambda _m: None)
    h = _repl.get_replication_header(planet_path)
    ts = h.timestamp
    has_seq = bool(h.url) and h.sequence is not None
    if ts is None and not has_seq:
        return PlanetUpdate("no replication position (sequence or timestamp) in planet header",
                            None, None, False)
    ts_iso = ts.isoformat() if ts is not None else None

    server = ReplicationServer(replication)
    try:
        start = h.sequence if has_seq else server.timestamp_to_sequence(ts)
    except Exception as exc:
        log(f"planet update skipped — replication unreachable ({type(exc).__name__})")
        return PlanetUpdate(f"unreachable: {type(exc).__name__}", ts_iso, None, False)
    if start is None:
        return PlanetUpdate("no sequence for timestamp", ts_iso, None, False)

    # ⚠️ REFUSE TO START A SECOND CONCURRENT UPDATE.
    #
    # The uuid temp name below makes an overlap non-destructive, but it does not
    # make it cheap: each execution copies the whole planet (92 GB here). Measured
    # 2026-09-14, that difference cost a night. A memory-starved host wedged its
    # runner, the 120s dead-server reaper reclaimed the task, the reclaimed
    # execution started a FRESH 92 GB copy, and the earlier one was never stopped.
    # Four ran at once, each at ~2.7 MB/s (~9.4h to finish), competing for exactly
    # the memory whose exhaustion caused the wedge. Every recovery attempt made it
    # worse, and the run reported progress throughout.
    #
    # Delivery is at-least-once with no fencing token, so a duplicate execution is
    # ALLOWED by contract and handler idempotency is required. This is that
    # idempotency: cheap to be correct, expensive to be redundant.
    #
    # A temp file still growing means a live writer: WAIT for it, then report
    # what it produced. One that has stopped growing is debris from a killed
    # execution: remove it, because nothing else ever did (6.6 GB of it was
    # recovered by hand).
    #
    # ⚠️ It used to RETURN "concurrent update in flight" -- a normal status, so the
    # step completed and the workflow moved on to cut extracts from the OLD
    # planet while the real update was still being written (2026-09-30: a reclaim
    # 6 min into a rewrite; ExtractRegions started at once on the stale file).
    # Refusing is right; succeeding is not. Waiting is both: the step completes
    # only once the planet it hands downstream is the updated one.
    other = _wait_for_inflight_update(planet_path, log)
    if other is not None:
        h2 = _repl.get_replication_header(planet_path)
        if h2.sequence is not None and h2.sequence >= start + 1:
            log(f"planet advanced to replication sequence {h2.sequence} by a concurrent execution")
            return PlanetUpdate("updated by a concurrent execution", ts_iso, h2.sequence, True)
        log("the concurrent update ended without advancing the planet — updating here")

    # PER-CALL temp name. A fixed one collided when two UpdatePlanet executions
    # ran against the same tree — and the `finally: unlink(tmp)` below would then
    # delete the OTHER execution's in-flight output. The watchdog manufactures
    # exactly that overlap by reclaiming a task that is still running, so this is
    # not hypothetical. Same lesson `_scratch_dir()` already encodes with a uuid.
    tmp = str(Path(planet_path).with_name(f"_planet_update_tmp.{uuid.uuid4().hex}.osm.pbf"))
    try:
        newseq = _apply_diffs(planet_path, tmp, start + 1, max_diff_mb * 1024, replication)
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return PlanetUpdate(f"apply failed: {type(exc).__name__}", ts_iso, None, False)
    if newseq is None:
        return PlanetUpdate("already current", ts_iso, start, False)
    os.replace(tmp, planet_path)
    log(f"planet advanced to replication sequence {newseq}")
    return PlanetUpdate("updated", ts_iso, newseq, True)


_INFLIGHT_POLL_S = 30
_INFLIGHT_STALE_S = 600


def _wait_for_inflight_update(planet_path: str, log: Callable[[str], None]) -> Path | None:
    """Block while another execution is rewriting the planet; return its temp path.

    Returns None when no update is in flight. Abandoned temps (not written for
    ``_INFLIGHT_STALE_S``) are removed. Polls at a pace that keeps the caller's
    heartbeat and cancellation live -- this thread sleeps, it does not hold the
    GIL -- so waiting out a 30-minute rewrite is safe."""
    waited_on: Path | None = None
    while True:
        live = None
        for old in Path(planet_path).parent.glob("_planet_update_tmp.*.osm.pbf"):
            try:
                age = time.time() - old.stat().st_mtime
            except OSError:
                continue  # finished (renamed into place) between glob and stat
            if age < _INFLIGHT_STALE_S:
                live = old
                continue
            log(f"removing abandoned planet temp {old.name} ({age / 3600:.1f}h old)")
            try:
                old.unlink()
            except OSError:
                pass
        if live is None:
            return waited_on
        if waited_on is None:
            log(f"another planet update is in flight ({live.name}) — waiting for it")
        waited_on = live
        raise_if_cancelled()
        time.sleep(_INFLIGHT_POLL_S)


def _apply_diffs(planet_path: str, tmp: str, start: int, max_kb: int, replication: str) -> int | None:
    """Apply replication diffs from ``start`` into ``tmp``, IN A CHILD PROCESS.

    Returns the new sequence (read back from ``tmp``'s header), or None when
    there was nothing to apply.

    ⚠️ Not in-process. pyosmium's merge ran at ~590% CPU inside the runner and
    starved its liveness signal: 6 minutes into a rewrite (2026-09-30) the
    dead-server reaper, 120 s on another host, reclaimed the task and a second
    execution started. A child process keeps the runner's own threads (server
    ping, task heartbeat, cancellation) running, and run_cancellable kills the
    child's whole process group if the run is terminated."""
    cmd = [sys.executable, "-m", __name__, "apply", planet_path, tmp,
           str(start), str(max_kb), replication]
    try:
        run_cancellable(cmd)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == _EXIT_NOTHING_TO_APPLY:
            return None
        raise
    return _repl.get_replication_header(tmp).sequence


_EXIT_NOTHING_TO_APPLY = 3


def _apply_main(argv: list[str]) -> int:
    src, dst, start, max_kb, replication = argv
    newseq = ReplicationServer(replication).apply_diffs_to_file(
        src, dst, int(start), max_size=int(max_kb))
    return 0 if newseq is not None else _EXIT_NOTHING_TO_APPLY


if __name__ == "__main__" and sys.argv[1:2] == ["apply"]:
    sys.exit(_apply_main(sys.argv[2:]))
