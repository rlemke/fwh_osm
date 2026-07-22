"""Phase 2 (Strategy A): keep the self-hosted master planet current + re-extract.

The schedulable maintenance step behind the ``planet_maintain.py`` tool, layered
on Phase 1 (``planet_bootstrap.bootstrap``). One run does:

  1. :func:`update_master` — advance the master PBF by applying replication diffs
     (pyosmium ``ReplicationServer`` — the SAME primitive the fwh_osm delta path
     uses). Real planet: ``planet-latest.osm.pbf`` + planet replication (reachable,
     unlike the banned Geofabrik / flaky osmfr-dev endpoints).
  2. re-extract — split the updated master into per-region extracts via Phase 1
     ``bootstrap()``, stamping OUR replication header. Regions inherit the master's
     new sequence, so freshness propagates.

Run it under a scheduler (cron / launchd / systemd timer / ``fw maint``), typically
nightly. Consumers then RE-DOWNLOAD ``<region>-latest.osm.pbf`` from the served
tree (point ``FW_GEOFABRIK_BASE_URL`` at it) — the existing fwh_osm download path,
unchanged.

Strategy A deliberately does NOT publish per-region diff files: regions refresh by
re-download of the whole (small) extract, sidestepping the reference-completeness
hazard of clipping diffs per region (that is Strategy B). **Serving the output tree
is an infra concern** (nginx / caddy / MinIO over the dir) — intentionally NOT part
of this tool; for a quick local check use ``python -m http.server --directory <out>``.

Requires the ``osmium`` binary and pyosmium (already dependencies of the delta path).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import osmium.replication as _repl
from osmium.replication.server import ReplicationServer

from .planet_bootstrap import RegionResult, bootstrap

# apply_diffs_to_file's max_size is in KB; cap per-run catch-up (default 1 GB).
_KB_PER_MB = 1024


@dataclass
class MasterUpdate:
    old_sequence: int | None
    new_sequence: int | None
    status: str          # "updated" | "already current" | "unreachable: X" | "no replication header"
    advanced: bool


@dataclass
class MaintainResult:
    master: MasterUpdate
    regions: list[RegionResult]


def update_master(master: str, *, max_diff_mb: int = 1024,
                  on_log: Callable[[str], None] | None = None) -> MasterUpdate:
    """Advance ``master`` in place by applying its replication diffs.

    Reads the replication position from the master's header and applies forward
    diffs (up to ``max_diff_mb`` per run). Never raises on a flaky/unreachable
    replication host — returns a ``status`` describing what happened and leaves the
    master untouched, so a scheduled run degrades to "re-extract at the current
    sequence" rather than failing.
    """
    log = on_log or (lambda _m: None)
    h = _repl.get_replication_header(master)
    if not h.url or h.sequence is None:
        return MasterUpdate(h.sequence, h.sequence, "no replication header", False)
    server = ReplicationServer(h.url)

    # Probe the server's current sequence FIRST. apply_diffs_to_file swallows a
    # dead/unreachable host and returns None — indistinguishable from "already
    # current" — which would silently mask a broken replication source (the master
    # quietly stops advancing). Probing lets us report "unreachable" loudly.
    try:
        state = server.get_state_info()
    except Exception as exc:
        log(f"master update skipped — replication unreachable ({type(exc).__name__})")
        return MasterUpdate(h.sequence, h.sequence, f"unreachable: {type(exc).__name__}", False)
    if state is None:
        log("master update skipped — replication state unavailable")
        return MasterUpdate(h.sequence, h.sequence, "unreachable: no state", False)
    if state.sequence <= h.sequence:
        return MasterUpdate(h.sequence, h.sequence, "already current", False)

    tmp = str(Path(master).with_name("_master_update_tmp.osm.pbf"))  # osmium infers fmt from ext
    try:
        newseq = server.apply_diffs_to_file(
            master, tmp, h.sequence + 1, max_size=max_diff_mb * _KB_PER_MB
        )
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return MasterUpdate(h.sequence, h.sequence, f"apply failed: {type(exc).__name__}", False)
    if newseq is None:  # server advanced then served no diffs (race) — treat as current
        return MasterUpdate(h.sequence, h.sequence, "already current", False)
    os.replace(tmp, master)
    log(f"master advanced {h.sequence} -> {newseq}")
    return MasterUpdate(h.sequence, newseq, "updated", True)


def maintain(*, master: str, out: str, regions: list[dict], base_url: str,
             strategy: str = "smart", max_diff_mb: int = 1024,
             on_log: Callable[[str], None] | None = None) -> MaintainResult:
    """One maintenance cycle: advance the master, then re-extract all regions.

    Returns the :class:`MasterUpdate` and the per-region :class:`RegionResult`s.
    Propagates :class:`~planet_bootstrap.BootstrapError` from the re-extract (a
    hard failure — bad region spec, osmium error, or header round-trip mismatch).
    """
    log = on_log or (lambda _m: None)
    upd = update_master(master, max_diff_mb=max_diff_mb, on_log=log)
    log(f"master: {upd.old_sequence} -> {upd.new_sequence} ({upd.status})")
    results = bootstrap(source=master, out=out, regions=regions, base_url=base_url,
                        strategy=strategy, on_log=log)
    return MaintainResult(upd, results)
