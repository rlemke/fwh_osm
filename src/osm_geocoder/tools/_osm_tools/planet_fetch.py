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
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import osmium.replication as _repl
from osmium.replication.server import ReplicationServer

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

    The planet's header carries a timestamp but no sequence, so the start point is
    derived from the timestamp via ``timestamp_to_sequence`` against the planet
    replication server. Never raises on a flaky/unreachable replication host —
    returns a ``status`` and leaves the planet untouched, so a scheduled run
    degrades to "re-extract at the current snapshot" instead of failing.
    """
    log = on_log or (lambda _m: None)
    h = _repl.get_replication_header(planet_path)
    ts = h.timestamp
    if ts is None:
        return PlanetUpdate("no timestamp in planet header", None, None, False)
    ts_iso = ts.isoformat()

    server = ReplicationServer(replication)
    try:
        start = h.sequence if (h.url and h.sequence is not None) else server.timestamp_to_sequence(ts)
    except Exception as exc:
        log(f"planet update skipped — replication unreachable ({type(exc).__name__})")
        return PlanetUpdate(f"unreachable: {type(exc).__name__}", ts_iso, None, False)
    if start is None:
        return PlanetUpdate("no sequence for timestamp", ts_iso, None, False)

    # PER-CALL temp name. A fixed one collided when two UpdatePlanet executions
    # ran against the same tree — and the `finally: unlink(tmp)` below would then
    # delete the OTHER execution's in-flight output. The watchdog manufactures
    # exactly that overlap by reclaiming a task that is still running, so this is
    # not hypothetical. Same lesson `_scratch_dir()` already encodes with a uuid.
    tmp = str(Path(planet_path).with_name(f"_planet_update_tmp.{uuid.uuid4().hex}.osm.pbf"))
    try:
        newseq = server.apply_diffs_to_file(planet_path, tmp, start + 1, max_size=max_diff_mb * 1024)
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return PlanetUpdate(f"apply failed: {type(exc).__name__}", ts_iso, None, False)
    if newseq is None:
        return PlanetUpdate("already current", ts_iso, start, False)
    os.replace(tmp, planet_path)
    log(f"planet advanced to replication sequence {newseq}")
    return PlanetUpdate("updated", ts_iso, newseq, True)
