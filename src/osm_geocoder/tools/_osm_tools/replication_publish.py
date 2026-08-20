# SPDX-License-Identifier: Apache-2.0
"""Publish per-region replication diffs — the producer half of the split.

Phase 1 split the planet into regional extracts and stamped each one with
``osmosis_replication_base_url`` pointing at *our* server, so the delta path
would follow us rather than Geofabrik. It created the ``<region>-updates/``
directories too. What it never did was put anything in them: every ``state.txt``
held a bare ``timestamp=`` with **no ``sequenceNumber``**, and not one
``.osc.gz`` was ever published. The indirection was wired to an empty room, so
every extract has been a frozen snapshot — 39 days stale when this was written,
and silently so.

This module fills the room. For each new day of OSM replication it cuts the
planet diff down to each region's polygon and publishes the result as that
region's own sequenced diff, which is precisely what Geofabrik does and what
``pbf_update.update_region`` (already shipped) knows how to consume.

**Why cutting diffs beats updating the planet.** The obvious alternative —
`pyosmium-up-to-date` the 87 GB planet, then re-split — costs two full passes
over 87 GB. Measured on this deployment's disk (32 MB/s raw read), one pass is
~45 minutes and a full refresh cycle runs to hours. Cutting instead touches only
the day's diff: 83 MB in, ~27 s of CPU per region, 344 KB out for
central-america. The planet file never has to be read at all.

**Publish, do not apply.** This writes diffs; it does not roll the served
extracts forward. That is the Geofabrik contract and it is the cheap half:
applying a day to `europe-latest.osm.pbf` (37 GB) is a 37 GB read plus write,
while publishing its diff is seconds. Consumers apply when they want fresh data,
via the existing ``osm.cache.UpdateRegion`` — and because ``osmium
apply-changes`` accepts many change files in ONE pass, catching up 39 days costs
a consumer the same single pass as catching up one.

Sequence numbers are OSM's own day numbers, deliberately. Inventing a private
numbering would mean maintaining a mapping to debug against; sharing OSM's means
``<region>-updates/000/005/090.osc.gz`` is knowably the same day as
``planet.openstreetmap.org/replication/day/000/005/090.osc.gz``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_UPSTREAM = "https://planet.openstreetmap.org/replication/day"
UPSTREAM_ENV = "FW_OSM_REPLICATION_UPSTREAM"
#: Where the split's published tree lives (``<region>-latest.osm.pbf`` +
#: ``<region>-updates/``) and where the region polygons are.
WWW_ENV = "FW_OSM_SELFHOST_WWW"
POLYS_ENV = "FW_OSM_SELFHOST_POLYS"
#: Stamped into published extracts so consumers follow us. Left alone here —
#: this module only writes diffs — but recorded for the CLI to report.
BASE_URL_ENV = "FW_OSM_SELFHOST_BASE_URL"

USER_AGENT = "facetwork-osm-selfhost/1.0 (+https://github.com/rlemke/facetwork)"


class ReplicationError(RuntimeError):
    """Raised when diffs cannot be produced or published."""


@dataclass
class RegionResult:
    region: str
    published: list[int] = field(default_factory=list)
    bytes_written: int = 0
    skipped: bool = False
    reason: str = ""


@dataclass
class PublishResult:
    upstream_sequence: int
    from_sequence: int
    to_sequence: int
    days: int
    regions: list[RegionResult] = field(default_factory=list)
    planet_bytes: int = 0


# ---------------------------------------------------------------------------
# Sequence <-> path, and the Osmosis state.txt dialect.
# ---------------------------------------------------------------------------


def sequence_path(seq: int) -> str:
    """``5090`` -> ``000/005/090`` — Osmosis' 9-digit triplet layout."""
    if seq < 0:
        raise ValueError(f"sequence must be non-negative, got {seq}")
    p = f"{seq:09d}"
    return f"{p[0:3]}/{p[3:6]}/{p[6:9]}"


def format_state(seq: int, timestamp: str) -> str:
    """An Osmosis ``state.txt``.

    The colons in the timestamp are backslash-escaped because this is a Java
    properties file, which is what every replication client expects — the
    existing hand-written state.txt files in this tree already use that form.
    """
    escaped = timestamp.replace(":", "\\:")
    return f"sequenceNumber={seq}\ntimestamp={escaped}\n"


def parse_state(text: str) -> tuple[int | None, str]:
    """(sequenceNumber, timestamp) from a state.txt; sequence None if absent.

    A missing sequenceNumber is the exact state Phase 1 left behind, so it is a
    normal input here rather than an error — it means "never published".
    """
    seq: int | None = None
    ts = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("sequenceNumber="):
            raw = line.split("=", 1)[1].strip()
            seq = int(raw) if raw.isdigit() else None
        elif line.startswith("timestamp="):
            ts = line.split("=", 1)[1].strip().replace("\\:", ":")
    return seq, ts


# ---------------------------------------------------------------------------
# Upstream.
# ---------------------------------------------------------------------------


def _get(url: str, *, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def upstream_state(upstream: str | None = None) -> tuple[int, str]:
    """Latest (sequence, timestamp) published by the upstream server."""
    base = (upstream or os.environ.get(UPSTREAM_ENV) or DEFAULT_UPSTREAM).rstrip("/")
    try:
        seq, ts = parse_state(_get(f"{base}/state.txt").decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise ReplicationError(f"cannot read upstream state at {base}: {exc}") from exc
    if seq is None:
        raise ReplicationError(f"upstream {base} state.txt has no sequenceNumber")
    return seq, ts


def fetch_planet_diff(seq: int, dest: Path, *, upstream: str | None = None) -> Path:
    """Download one upstream diff, cached by sequence."""
    base = (upstream or os.environ.get(UPSTREAM_ENV) or DEFAULT_UPSTREAM).rstrip("/")
    out = dest / f"{seq:09d}.osc.gz"
    if out.exists() and out.stat().st_size > 0:
        return out
    url = f"{base}/{sequence_path(seq)}.osc.gz"
    log.info("fetching upstream diff %s", url)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    try:
        with urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=900
        ) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise ReplicationError(f"cannot fetch {url}: {exc}") from exc
    tmp.replace(out)
    return out


def diff_timestamp(seq: int, *, upstream: str | None = None) -> str:
    """The upstream's own timestamp for a sequence, from its ``.state.txt``."""
    base = (upstream or os.environ.get(UPSTREAM_ENV) or DEFAULT_UPSTREAM).rstrip("/")
    try:
        _s, ts = parse_state(
            _get(f"{base}/{sequence_path(seq)}.state.txt").decode("utf-8", "replace")
        )
        return ts
    except Exception:  # noqa: BLE001 — a missing per-diff state is not fatal
        return ""


# ---------------------------------------------------------------------------
# Regions.
# ---------------------------------------------------------------------------


def www_root() -> Path:
    root = os.environ.get(WWW_ENV, "").strip()
    if not root:
        raise ReplicationError(f"{WWW_ENV} is not set (the published split tree)")
    return Path(root)


def polys_root() -> Path:
    root = os.environ.get(POLYS_ENV, "").strip()
    if not root:
        raise ReplicationError(f"{POLYS_ENV} is not set (the region polygons)")
    return Path(root)


#: Regions may be listed explicitly, which is what a scheduled job should do —
#: see :func:`discover_regions`.
REGIONS_ENV = "FW_OSM_SELFHOST_REGIONS"


def discover_regions(www: Path | None = None) -> list[str]:
    """The regions to publish for.

    Prefers an explicit list (``FW_OSM_SELFHOST_REGIONS``, or a ``regions.json``
    beside the tree) and only ENUMERATES as a last resort, because enumeration
    is the operation that fails in the environment this runs in.

    On macOS a LaunchAgent may `stat` a path on an external volume while being
    denied `readdir` on it (TCC). ``test -d`` and ``test -r`` both pass — they
    lie — and `ls` returns "Operation not permitted". ``Path.glob`` swallows
    that OSError and yields nothing, so a denied directory is INDISTINGUISHABLE
    from a correctly configured tree that happens to contain no regions. The
    nightly job would then report success having published nothing, for as long
    as anyone left it running: exactly the silent staleness this whole phase
    exists to end.

    So the fallback path probes the directory explicitly and raises rather than
    returning an empty list.
    """
    w = www or www_root()

    explicit = os.environ.get(REGIONS_ENV, "").strip()
    if explicit:
        return sorted({r for r in re.split(r"[,\s]+", explicit) if r})

    manifest = w.parent / "regions.json"
    if manifest.exists():
        try:
            import json

            data = json.loads(manifest.read_text(encoding="utf-8"))
            # Only the KEYS are trusted: the manifest on this deployment still
            # carries paths from a previous volume name.
            keys = sorted({str(e["key"]) for e in data if isinstance(e, dict) and e.get("key")})
            if keys:
                return keys
        except (OSError, ValueError, KeyError, TypeError):
            pass  # fall through to enumeration

    try:
        entries = list(w.iterdir())
    except OSError as exc:
        raise ReplicationError(
            f"cannot list {w}: {exc}. On macOS a LaunchAgent is often denied "
            f"readdir on an external volume even though the path stats fine — "
            f"grant the agent Full Disk Access, or set {REGIONS_ENV} to the "
            f"region list so no enumeration is needed."
        ) from exc
    return sorted(p.name[: -len("-updates")] for p in entries
                  if p.is_dir() and p.name.endswith("-updates"))


#: Records the sequence the SERVED EXTRACT's data is at, which is NOT the
#: published head: this project publishes diffs without applying them, so the
#: head runs ahead of the extract by design. Conflating the two stamps an
#: extract as newer than it is, and every consumer then starts after the diffs
#: it still needs — a silent, permanent gap. Kept beside state.txt so the two
#: facts live together.
EXTRACT_STATE = "extract.state.txt"


def extract_state(region: str, www: Path | None = None) -> tuple[int | None, str]:
    """(sequence, timestamp) of the served extract's DATA, not the published head."""
    w = www or www_root()
    f = w / f"{region}-updates" / EXTRACT_STATE
    if not f.exists():
        return None, ""
    return parse_state(f.read_text(encoding="utf-8", errors="replace"))


def region_state(region: str, www: Path | None = None) -> tuple[int | None, str]:
    w = www or www_root()
    state = w / f"{region}-updates" / "state.txt"
    if not state.exists():
        return None, ""
    return parse_state(state.read_text(encoding="utf-8", errors="replace"))


def extract_timestamp(region: str, www: Path | None = None) -> str:
    """The served extract's replication timestamp, read from its PBF header.

    Header-only ``osmium fileinfo`` — the extended form rescans the whole file,
    which for europe means reading 37 GB to learn one string.
    """
    w = www or www_root()
    pbf = w / f"{region}-latest.osm.pbf"
    if not pbf.exists():
        return ""
    try:
        out = subprocess.run(
            ["osmium", "fileinfo", str(pbf)],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"osmosis_replication_timestamp=(\S+)", out)
    return m.group(1) if m else ""


def cut_diff(planet_diff: Path, poly: Path, out: Path, *, osmium_bin: str = "osmium") -> int:
    """Cut a planet diff down to one region's polygon.

    ``--with-history`` is REQUIRED: a change file carries several versions of
    the same object, and without it osmium rejects the input rather than
    silently keeping one — the flag is what makes this legal, not an option.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.osc.gz")
    try:
        subprocess.run(
            [osmium_bin, "extract", "--with-history", "-p", str(poly),
             "-o", str(tmp), "--overwrite", str(planet_diff)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        raise ReplicationError(
            f"osmium extract failed for {out.name}: "
            f"{(exc.stderr or '').strip()[:300]}"
        ) from exc
    except FileNotFoundError as exc:
        raise ReplicationError(f"osmium not found ({osmium_bin})") from exc
    tmp.replace(out)
    return out.stat().st_size


def cut_diff_multi(
    planet_diff: Path,
    regions: list[str],
    polys: Path,
    staging: Path,
    *,
    osmium_bin: str = "osmium",
) -> dict[str, Path]:
    """Cut one planet diff to MANY region polygons in a single osmium pass.

    The dominant cost is decoding the day's diff, not testing points against a
    polygon, so cutting N regions one at a time pays that cost N times for no
    reason. Measured: one region 27.3s, three regions 29.3s — the marginal
    region is about a second. Over a 39-day catch-up across 8 regions that is
    the difference between ~20 minutes and ~2.3 hours.

    Outputs land in ``staging`` and are moved into place by the caller, so a
    failed pass leaves no half-written diff where a consumer could fetch it.
    Regions whose polygon is missing are omitted from the config rather than
    silently receiving an uncut planet-wide diff.
    """
    import json

    staging.mkdir(parents=True, exist_ok=True)
    usable = [r for r in regions if (polys / f"{r}.poly").exists()]
    if not usable:
        return {}
    cfg = {
        "directory": str(staging),
        "extracts": [
            {
                "output": f"{r}.osc.gz",
                "output_format": "osc.gz",
                "polygon": {"file_name": str(polys / f"{r}.poly"), "file_type": "poly"},
            }
            for r in usable
        ],
    }
    cfg_path = staging / "extract-config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    try:
        subprocess.run(
            [osmium_bin, "extract", "--with-history", "-c", str(cfg_path),
             "--overwrite", str(planet_diff)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ReplicationError(
            f"osmium extract (multi) failed: {(exc.stderr or '').strip()[:300]}"
        ) from exc
    except FileNotFoundError as exc:
        raise ReplicationError(f"osmium not found ({osmium_bin})") from exc
    return {r: staging / f"{r}.osc.gz" for r in usable
            if (staging / f"{r}.osc.gz").exists()}


# ---------------------------------------------------------------------------
# Publish.
# ---------------------------------------------------------------------------


def publish(
    *,
    regions: list[str] | None = None,
    max_days: int = 7,
    upstream: str | None = None,
    www: Path | None = None,
    polys: Path | None = None,
    osmium_bin: str = "osmium",
    dry_run: bool = False,
    work_dir: Path | None = None,
) -> PublishResult:
    """Publish new per-region diffs, bounded by ``max_days``.

    Bounded on purpose. A region that has never published (the Phase 1 state)
    is arbitrarily far behind, and an unbounded catch-up would download tens of
    GB of upstream diffs on the first run without anyone choosing to. Callers
    walk forward in bounded steps; each run leaves a consistent state.txt, so
    stopping early is safe and resuming is free.
    """
    w = www or www_root()
    p_root = polys or polys_root()
    up_seq, up_ts = upstream_state(upstream)
    names = regions if regions is not None else discover_regions(w)
    if not names:
        raise ReplicationError(f"no <region>-updates directories under {w}")

    # Where each region starts. A region with no sequenceNumber has never
    # published; fall back to the SERVED EXTRACT's timestamp, which is the only
    # honest baseline — publishing diffs newer than the extract they apply to
    # would produce a replication stream nobody can consume.
    starts: dict[str, int | None] = {}
    for r in names:
        seq, _ts = region_state(r, w)
        starts[r] = seq

    known = [s for s in starts.values() if s is not None]
    if known:
        from_seq = min(known)
    else:
        from_seq = None

    result = PublishResult(
        upstream_sequence=up_seq,
        from_sequence=from_seq if from_seq is not None else -1,
        to_sequence=from_seq if from_seq is not None else -1,
        days=0,
    )

    if from_seq is None:
        # Nothing to anchor to. Report rather than guess a sequence: an
        # incorrect anchor silently publishes diffs that do not line up with
        # the extract, which is worse than publishing nothing.
        for r in names:
            result.regions.append(RegionResult(
                region=r, skipped=True,
                reason=("no sequenceNumber in state.txt — run `--anchor SEQ` once to "
                        "set the baseline matching the served extract's timestamp"),
            ))
        return result

    target = min(up_seq, from_seq + max_days)
    if target <= from_seq:
        for r in names:
            result.regions.append(RegionResult(region=r, skipped=True, reason="current"))
        result.to_sequence = from_seq
        return result

    tmp_work = work_dir or Path(tempfile.mkdtemp(prefix="osm-repl-"))
    tmp_work.mkdir(parents=True, exist_ok=True)
    per_region = {r: RegionResult(region=r) for r in names}

    try:
        for seq in range(from_seq + 1, target + 1):
            if dry_run:
                log.info("[dry] would fetch upstream %d and cut %d region(s)", seq, len(names))
                continue
            planet_diff = fetch_planet_diff(seq, tmp_work, upstream=upstream)
            result.planet_bytes += planet_diff.stat().st_size
            ts = diff_timestamp(seq, upstream=upstream) or up_ts

            due = [r for r in names
                   if not (starts[r] is not None and seq <= starts[r])]
            for r in due:
                if not (p_root / f"{r}.poly").exists():
                    per_region[r].skipped = True
                    per_region[r].reason = f"no polygon at {p_root / (r + '.poly')}"
            cuttable = [r for r in due if not per_region[r].skipped]
            if not cuttable:
                continue

            cut_stage = tmp_work / f"cut-{seq}"
            produced = cut_diff_multi(planet_diff, cuttable, p_root, cut_stage,
                                      osmium_bin=osmium_bin)
            for r in cuttable:
                src = produced.get(r)
                if src is None:
                    per_region[r].skipped = True
                    per_region[r].reason = f"osmium produced no output for sequence {seq}"
                    continue
                out = w / f"{r}-updates" / f"{sequence_path(seq)}.osc.gz"
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(out))
                size = out.stat().st_size
                per_region[r].published.append(seq)
                per_region[r].bytes_written += size
                # state.txt is written AFTER the diff lands, every sequence, so
                # an interrupted run leaves a state a consumer can still trust:
                # it never advertises a diff that is not there.
                (w / f"{r}-updates" / "state.txt").write_text(
                    format_state(seq, ts), encoding="utf-8"
                )
                (out.parent / out.name.replace(".osc.gz", ".state.txt")).write_text(
                    format_state(seq, ts), encoding="utf-8"
                )
            shutil.rmtree(cut_stage, ignore_errors=True)
            result.days += 1
            result.to_sequence = seq
    finally:
        if work_dir is None:
            shutil.rmtree(tmp_work, ignore_errors=True)

    result.regions = [per_region[r] for r in names]
    return result


def stamp_extract(
    region: str,
    seq: int,
    timestamp: str,
    base_url: str,
    *,
    www: Path | None = None,
    osmium_bin: str = "osmium",
) -> int:
    """Write the replication baseline into a served extract's PBF header.

    Without this the whole producer side is unreachable: ``pbf_update``
    refuses to delta when ``header.sequence is None`` and silently falls back
    to re-downloading the entire extract — the exact bandwidth this project
    exists to avoid. Phase 1 stamped a base_url and a timestamp but no
    SEQUENCE, so every consumer took the full path.

    A PBF header cannot be patched in place (its blob length changes), so this
    rewrites the file — 853 MB takes ~1 min here, 37 GB for europe takes the
    better part of an hour. It is therefore one-time and explicit, never part
    of a routine publish: the sequence only changes when the extract itself is
    regenerated.

    Returns the rewritten size in bytes.
    """
    w = www or www_root()
    pbf = w / f"{region}-latest.osm.pbf"
    if not pbf.exists():
        raise ReplicationError(f"no extract at {pbf}")
    tmp = pbf.with_name(pbf.name + ".stamping.osm.pbf")
    try:
        subprocess.run(
            [osmium_bin, "cat", "-o", str(tmp), "--overwrite",
             f"--output-header=osmosis_replication_sequence_number={seq}",
             f"--output-header=osmosis_replication_timestamp={timestamp}",
             f"--output-header=osmosis_replication_base_url={base_url}",
             str(pbf)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        raise ReplicationError(
            f"osmium cat failed stamping {region}: {(exc.stderr or '').strip()[:300]}"
        ) from exc
    size = tmp.stat().st_size
    # Replace only after a complete rewrite — a half-written extract served to
    # a consumer is worse than a stale one.
    tmp.replace(pbf)
    return size


def anchor(regions: list[str], seq: int, timestamp: str, *, www: Path | None = None) -> list[str]:
    """Set the starting sequence for regions that have never published.

    Separate from :func:`publish` and never inferred, because the anchor must
    match the timestamp of the extract already on disk. Guessing it produces a
    stream whose diffs do not compose with the extract they claim to update —
    a corruption that only shows up in a consumer, days later.
    """
    w = www or www_root()
    touched: list[str] = []
    for r in regions:
        d = w / f"{r}-updates"
        if not d.is_dir():
            continue
        (d / "state.txt").write_text(format_state(seq, timestamp), encoding="utf-8")
        # The anchor IS the extract's own sequence — that is what makes it an
        # anchor. Recorded now, because after the first publish the head moves
        # and this fact would otherwise be unrecoverable.
        (d / EXTRACT_STATE).write_text(format_state(seq, timestamp), encoding="utf-8")
        touched.append(r)
    return touched
