"""Handlers for the ``osm.planet`` self-hosted-extracts pipeline.

Backs the event facets that let a workflow build (and keep current) our own
"Geofabrik": download the OSM planet, delta-update it from replication, download
osmfr boundary polygons, extract per-region PBFs, and publish them to object
storage. Each handler is a thin wrapper over the standalone tools in
``tools/_osm_tools`` (``planet_fetch``, ``polygon_fetch``, ``planet_bootstrap``),
so the FFL and the ``download-planet`` / ``download-polygons`` / ``planet-bootstrap``
CLIs share one implementation.

Blocking network + heavy disk I/O — registered with ``timeout_ms=0`` (rely on the
runner's global execution timeout), like the other cache/download handlers.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import threading
from typing import Any

from ...tools._osm_tools.planet_fetch import fetch_planet, update_planet
from ...tools._osm_tools.polygon_fetch import fetch_polygons, fetch_country_subregions
from ...tools._osm_tools.cancellation import cancellable, raise_if_cancelled
from ...tools._osm_tools.planet_bootstrap import bootstrap_batched
from ...tools._osm_tools.boundary_gen import generate_polygons

try:                                  # the runtime marks this non-retryable;
    from facetwork.runtime.errors import PermanentError   # fall back so the
except Exception:                     # handler still imports standalone/tests
    class PermanentError(RuntimeError):  # type: ignore[no-redef]
        pass

NAMESPACE = "osm.planet"

# Defaults for empty-string params (env-overridable), so the workflow can run
# with no arguments on a host that sets FW_PLANET_DIR / FW_S3_* .
#
# ⚠️ The historical default is a HOST path. It is correct on a bare-metal runner
# (osm-maintain / osm-replicate serve the self-hosted tree from exactly there) and
# CATASTROPHIC inside a container, where it does not exist: os.makedirs happily
# creates it in the container's WRITABLE LAYER, so an ~80 GB planet streams into
# the Docker VM's overlay instead of the mounted data volume. On 2026-08-29 that
# filled server3's 58 GB VM disk and took the fleet's MongoDB down with ENOSPC
# (WiredTiger error 28 -> WT_PANIC -> fassert), while 3.6 TiB sat free on the
# external volume bind-mounted at /scratch two directories away.
#
# So resolve it instead of assuming it: an explicit FW_PLANET_DIR always wins,
# then the host path IF IT ACTUALLY EXISTS, then the mounted scratch. Existence is
# the test because that is precisely what distinguishes the two environments.
_HOST_PLANET_DIR = "/Volumes/afl_data/osm-selfhost"


def _default_planet_dir() -> str:
    explicit = os.environ.get("FW_PLANET_DIR")
    if explicit:
        return explicit
    if os.path.isdir(_HOST_PLANET_DIR):
        return _HOST_PLANET_DIR
    scratch = os.environ.get("FW_LOCAL_SCRATCH")
    if scratch and os.path.isdir(scratch):
        return os.path.join(scratch, "osm-selfhost")
    return _HOST_PLANET_DIR


_PLANET_DIR = _default_planet_dir()

#: Planet + delta working room, GB. The dump alone is ~80 GB and update_planet
#: needs headroom beside it.
_PLANET_FREE_GB = int(os.environ.get("FW_PLANET_MIN_FREE_GB", "150"))


def _require_free_space(path: str, need_gb: int = _PLANET_FREE_GB) -> None:
    """Refuse to start a multi-tens-of-GB download onto a filesystem too small.

    The dest resolution above should keep this from ever firing, but the failure
    it guards is shared-fate: filling a Docker VM disk does not just fail this
    task, it kills every container storing data on that VM - MongoDB included.
    A guard that fails ONE task is strictly better than one that takes the fleet
    with it, so check the filesystem we are actually about to write to.

    PermanentError, not a retry: no amount of retrying makes a disk bigger, and
    burning the retry budget here just repeats the damage.
    """
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        st = os.statvfs(probe or "/")
    except OSError:
        return                      # cannot tell - do not invent a verdict
    free_gb = (st.f_bavail * st.f_frsize) / 1_000_000_000
    if free_gb < need_gb:
        raise PermanentError(
            f"refusing to download the planet to {path!r}: only {free_gb:.1f} GB free "
            f"on the filesystem holding {probe!r} (need {need_gb} GB). "
            f"Set FW_PLANET_DIR to a large volume - inside a container the mounted "
            f"data volume, NOT the container filesystem."
        )


def _planet_path(dest: str) -> str:
    return dest or os.path.join(_PLANET_DIR, "planet-latest.osm.pbf")


def _publish_polys(regions: list[dict], on_log=None) -> list[dict]:
    """Rewrite each region's `poly` to a PORTABLE URI on the durable backend.

    A Region used to carry an absolute LOCAL path, which silently assumed every
    step of BuildPlanetExtracts ran on one host. It does not: on 2026-08-29 the
    same workflow put DownloadPolygons and DownloadPlanet on different machines
    in both directions, so whichever host reached ExtractRegions was missing one
    of its two inputs. Polys are small (one GeoJSON/.poly per region), so
    publishing them costs little and makes the step boundary host-agnostic —
    which is the platform's own contract: step payloads carry portable URIs any
    runner can resolve.

    The planet is deliberately NOT treated this way; see _resolve_planet.
    Falls back to the local path when the backend is local, so single-host and
    test runs are unchanged.
    """
    log = on_log or (lambda _m: None)
    try:
        from ...tools._osm_tools.storage import get_storage, cache_root
        st = get_storage()
        root = st.join(cache_root(), "planet-polys") if hasattr(st, "join") else None
    except Exception as exc:                     # storage unavailable -> keep local
        log(f"poly publish skipped ({type(exc).__name__}: {exc}); using local paths")
        return regions
    if not root or not str(root).startswith(("s3://", "hdfs://")):
        return regions                           # local backend: paths already work
    out: list[dict] = []
    published = 0
    for r in regions:
        local = r.get("poly") or ""
        if not local or str(local).startswith(("s3://", "hdfs://")):
            out.append(r); continue
        try:
            dst = st.join(root, os.path.basename(local))
            with open(local, "rb") as fh, st.open_write_binary(dst) as w:
                shutil.copyfileobj(fh, w)
            out.append({**r, "poly": dst}); published += 1
        except Exception as exc:
            log(f"poly publish failed for {local}: {type(exc).__name__}: {exc}; keeping local path")
            out.append(r)
    log(f"published {published}/{len(regions)} polygon(s) as portable URIs under {root}")
    return out


def _localize_polys(regions: list[dict], on_log=None) -> list[dict]:
    """Bring any remote `poly` URI down to this host before osmium reads it."""
    log = on_log or (lambda _m: None)
    remote = [r for r in regions if str(r.get("poly") or "").startswith(("s3://", "hdfs://"))]
    if not remote:
        return regions
    from ...tools._osm_tools.storage import get_storage
    st = get_storage()
    out = []
    for r in regions:
        poly = r.get("poly") or ""
        out.append({**r, "poly": st.localize(poly)} if str(poly).startswith(("s3://", "hdfs://")) else r)
    log(f"localized {len(remote)} remote polygon(s) to this host")
    return out


def _resolve_planet(passed: str, on_log=None) -> str:
    """The planet is host-local BY NECESSITY, so resolve it per host.

    ~80 GB through the object store would mean uploading it and downloading it
    again — that is not portability, it is waste. osmium also needs a real local
    file. So unlike the polys, a planet path from ANOTHER host is not fetched:
    this host's own copy is used instead (osm-maintain keeps it current), and if
    there is none we fail with a message that says which host and why, rather
    than letting osmium fail on a missing file three frames down.
    """
    log = on_log or (lambda _m: None)
    if passed and os.path.exists(passed):
        return passed
    own = _planet_path("")
    if os.path.exists(own):
        if passed and passed != own:
            log(f"planet {passed!r} is not on this host; using local {own!r}")
        return own
    import socket
    raise PermanentError(
        f"no planet on this host ({socket.gethostname()}): neither {passed!r} (from an "
        f"upstream step, possibly another machine) nor {own!r} exists. ExtractRegions "
        f"must run where the planet lives — the host that maintains the self-hosted "
        f"tree — because an ~80 GB file cannot sensibly cross the step boundary."
    )


def _log(params: dict[str, Any]):
    sl = params.get("_step_log")
    return (lambda m: sl(m, level="info")) if sl else (lambda m: None)


def _is_maintained_planet(path: str) -> bool:
    """True when `path` is a usable planet this host already maintains.

    Cheap ON PURPOSE — it reads the PBF *header* only. The alternative,
    `fetch_planet`'s md5 comparison, is wrong here in two independent ways:

    1. It cannot pass. `update_planet` advances this file past upstream by
       applying replication diffs, so its md5 will NEVER equal the published
       planet-latest.osm.pbf.md5 again. The check is guaranteed to report "not
       cached" and re-download ~92 GB, forever.
    2. It cannot finish. Measured 2026-08-30 on server3: hashing the mounted
       planet runs at 27 MB/s, so a full pass takes ~57 min — nearly DOUBLE the
       30-minute stuck-task timeout. Every attempt was reclaimed mid-hash, and
       each reclaim started another concurrent hash of the same file.

    A readable replication header means osmium can open it and we know its
    vintage, which is the property we actually need. Freshness is UpdatePlanet's
    job, not a checksum's.
    """
    if not os.path.exists(path):
        return False
    try:
        import osmium.replication as _repl
        return _repl.get_replication_header(path).timestamp is not None
    except Exception:
        return False


def handle_download_planet(params: dict[str, Any]) -> dict[str, Any]:
    path = _planet_path(params.get("dest") or "")
    log = _log(params)
    force = bool(params.get("force"))

    if not force and _is_maintained_planet(path):
        size = os.path.getsize(path)
        log(f"planet already present and readable ({size} bytes) — no transfer")
        return {"planet_path": path, "size_mb": round(size / 1_000_000, 1), "was_cached": True}

    _require_free_space(path)
    verify = params.get("verify", True) is not False
    # Heartbeat + cancellation: an ~80 GB transfer is one blocking call with no
    # loop to hook, so without a ticker the runtime declares it dead at 30 min and
    # re-dispatches it WHILE IT IS STILL RUNNING — two curls resuming into the
    # same path by offset. Observed five times on 2026-08-30 before dead-letter.
    with cancellable(params.get("_cancellation_check")):
        with _heartbeating(params, "downloading planet"):
            res = fetch_planet(path, verify=verify, force=force, on_log=log)
    return {"planet_path": res.path, "size_mb": round(res.size_bytes / 1_000_000, 1),
            "was_cached": res.was_cached}


def handle_update_planet(params: dict[str, Any]) -> dict[str, Any]:
    # Same per-host resolution as ExtractRegions: this step receives a path from
    # DownloadPlanet, which may have run on ANOTHER host. Passing it through
    # unresolved meant applying diffs to a path that does not exist here — a
    # silent no-op at best. Wiring the resolver into only one of the two planet
    # consumers was the gap in the original portable-URI change.
    path = _resolve_planet(params.get("planet_path") or "", on_log=_log(params))
    try:
        max_diff_mb = int(params.get("max_diff_mb") or 4096)
    except (TypeError, ValueError):
        max_diff_mb = 4096
    # Same protection as the download: applying diffs to a 92 GB file is a single
    # long blocking call, and a reclaim mid-apply would run two of them.
    with cancellable(params.get("_cancellation_check")):
        with _heartbeating(params, "applying replication diffs"):
            u = update_planet(path, max_diff_mb=max_diff_mb, on_log=_log(params))
    return {"planet_path": path, "status": u.status, "advanced": u.advanced}


def handle_download_polygons(params: dict[str, Any]) -> dict[str, Any]:
    dest = params.get("dest") or os.path.join(_PLANET_DIR, "polys")
    scope = params.get("scope") or "all"
    log = _log(params)
    regions = fetch_polygons(dest, scope=scope, on_log=log)
    out = _publish_polys([{"key": r.key, "poly": r.poly} for r in regions], on_log=log)
    return {"poly_dir": dest, "region_count": len(regions), "regions": out}


def handle_generate_polygons(params: dict[str, Any]) -> dict[str, Any]:
    """Generate region polygons from OSM admin boundaries (self-contained, no
    external poly source). Source = the planet, or a continent/country extract for
    a cheaper pass. Returns the same [{key, poly}] shape ExtractRegions consumes."""
    source = params.get("source") or _planet_path("")
    try:
        admin_level = int(params.get("admin_level") or 2)
    except (TypeError, ValueError):
        admin_level = 2
    dest = params.get("dest") or os.path.join(_PLANET_DIR, f"boundary_polys/admin{admin_level}")
    log = _log(params)
    regions = generate_polygons(source, admin_level, dest, on_log=log)
    out = _publish_polys([{"key": r.key, "poly": r.poly} for r in regions], on_log=log)
    return {"poly_dir": dest, "region_count": len(regions), "regions": out}


def _resolve_extract_source(source_region: str, planet_path: str, on_log=None) -> str:
    """Cut from a CONTINENT extract when the caller names one, not the planet.

    osmium reads the whole source per batch, and its memory scales with what it
    must track across that source. US states only ever intersect north-america:
    20 GB against the planet's 92 GB, so naming the region is ~4.4x less I/O AND
    proportionally less RAM per region — which is what OOM-killed the 25-region
    batch on 2026-08-30. tiger_fetch's own docstring already says the source is
    "the planet, or the north-america continent extract for a cheaper pass"; this
    just lets a caller say so.

    Refuses rather than silently falling back to the planet: a caller that asked
    for the cheap source and got the expensive one would look like it worked and
    cost 4x, which is exactly the class of silent-wrong-default this file keeps
    getting bitten by.
    """
    if not source_region:
        return planet_path
    log = on_log or (lambda _m: None)
    cand = os.path.join(_PLANET_DIR, "www", f"{source_region}-latest.osm.pbf")
    if not os.path.exists(cand):
        raise PermanentError(
            f"source_region={source_region!r} requested but {cand!r} is not on this host. "
            f"Serve it into the planet tree first, or clear source_region to cut from the planet."
        )
    log(f"cutting from {source_region} ({os.path.getsize(cand)/1e9:.1f} GB) instead of the planet")
    return cand


def _extract_resume_skip(out: str, regions: list[dict], source: str, on_log=None):
    """Split `regions` into (todo, skipped) by what is already cut and current.

    The invariant is "an extract is valid if it is NEWER THAN THE SOURCE it was
    cut from" — no refresh knob to tune, and self-correcting: the nightly
    re-split advances the continent extract, which makes every derived state
    stale automatically. That is strictly better than an age threshold, which
    has to be guessed and then kept in step with the cadence by hand.

    Why this exists: ExtractRegions had NO resume. Measured 2026-08-30, a run
    reclaimed at 31 min restarted at batch 1/13 every time and never reached
    batch 4 — while 40 of 51 states were already sitting complete on disk.

    ⚠️ Zero-length outputs are NOT skipped. A killed osmium leaves truncated
    files behind (25 of them were on this host), and skipping one would mean a
    broken extract survives every future run and eventually gets published.
    """
    log = on_log or (lambda _m: None)
    try:
        src_mtime = os.path.getmtime(source)
    except OSError:
        return list(regions), []
    todo, skipped = [], []
    for r in regions:
        dst = os.path.join(out, f"{r['key']}-latest.osm.pbf")
        try:
            st = os.stat(dst)
        except OSError:
            todo.append(r); continue
        if st.st_size > 0 and st.st_mtime >= src_mtime:
            skipped.append(r)
        else:
            todo.append(r)
    if skipped:
        log(f"resume: {len(skipped)} region(s) already cut from this source — {len(todo)} to build")
    return todo, skipped


def handle_extract_regions(params: dict[str, Any]) -> dict[str, Any]:
    log = _log(params)
    planet = _resolve_extract_source(
        (params.get("source_region") or "").strip(),
        _resolve_planet(params.get("planet_path") or "", on_log=log),
        on_log=log,
    )
    regions = params.get("regions") or []
    if not regions:
        raise ValueError("ExtractRegions: 'regions' is empty (run DownloadPolygons first)")
    regions = _localize_polys(regions, on_log=log)
    all_regions = regions
    if not params.get("force_rebuild"):
        regions, _skipped = _extract_resume_skip(
            params.get("out") or os.path.join(_PLANET_DIR, "www"), regions, planet, on_log=log)
        if not regions:
            log(f"resume: all {len(all_regions)} region(s) already current — nothing to cut")
            return {"region_count": len(all_regions),
                    "out": params.get("out") or os.path.join(_PLANET_DIR, "www")}
    out = params.get("out") or os.path.join(_PLANET_DIR, "www")
    base_url = _extract_base_url(params)
    strategy = params.get("strategy") or "complete_ways"
    try:
        batch_size = int(params.get("batch_size") or 25)
    except (TypeError, ValueError):
        batch_size = 25
    # bootstrap_batched runs for HOURS (13 batches x ~13 min for the US states), and
    # it is the same call handle_build_admin_set already wraps at its own call site.
    # Unwrapped here, the stuck-task watchdog reclaimed it at 31 min WHILE IT WAS
    # PUBLISHING — logging is not heartbeating — and every reclaim started another
    # full execution from batch 1. Measured 2026-08-30: 7 states published, then
    # SEVEN concurrent osmium passes at ~12 GiB each, and the VM OOM-killed them.
    # The batch size was never the problem; the duplicate executions were.
    with cancellable(params.get("_cancellation_check")):
        with _heartbeating(params, f"extracting {len(regions)} region(s)"):
            results = bootstrap_batched(source=planet, out=out, regions=regions,
                                        base_url=base_url, strategy=strategy,
                                        batch_size=batch_size, on_log=_log(params))
    # the FULL set, not just what this execution cut — a resumed run that built
    # 11 of 51 has still delivered 51, and reporting 11 would read as a shortfall
    return {"region_count": len(all_regions), "out": out}


def _scratch_dir() -> str:
    """A PER-TASK host-local scratch dir (the fleet runner mounts /scratch → host
    afl_data/osm-scratch; falls back to /tmp). The UUID suffix is REQUIRED: a host
    runs several osm runners, so under a fan-out two BuildAdminSet tasks land on one
    host at once — a shared path let one task's final rmtree delete the other's
    in-flight download (and boto3 temp files collide), corrupting both. Per-task
    isolation makes concurrent extraction on a host safe."""
    import uuid
    for base in (os.environ.get("FW_LOCAL_SCRATCH"), "/scratch"):
        if base and os.path.isdir(base):
            root = base
            break
    else:
        root = "/tmp"
    p = os.path.join(root, "osm-admin-set", uuid.uuid4().hex)
    os.makedirs(p, exist_ok=True)
    return p


def _s3_client(endpoint: str | None = None):
    try:
        import boto3  # optional [s3] extra
    except ImportError as exc:
        raise RuntimeError("this facet needs boto3 (pip install '.[s3]')") from exc
    return boto3.client(
        "s3", endpoint_url=endpoint or os.environ.get("FW_S3_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.environ.get("FW_S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("FW_S3_SECRET_KEY", "minioadmin"))


_TRANSFER = None


def _tc():
    global _TRANSFER
    if _TRANSFER is None:
        from boto3.s3.transfer import TransferConfig
        _TRANSFER = TransferConfig(multipart_threshold=64 * 1024 * 1024,
                                   multipart_chunksize=64 * 1024 * 1024, max_concurrency=4)
    return _TRANSFER


def _ensure_public_bucket(s3, bucket: str) -> None:
    """Create ``bucket`` (idempotent) and grant anonymous read, so FW_GEOFABRIK_BASE_URL
    consumers can GET the extracts."""
    import json
    try:
        s3.create_bucket(Bucket=bucket)
    except Exception:
        pass
    try:
        s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                           "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/*"]}]}))
    except Exception:
        pass


def _publish_one(s3, out: str, key: str, bucket: str) -> None:
    """Upload one region's ``<key>-latest.osm.pbf`` + ``<key>-updates/state.txt``."""
    pbf = os.path.join(out, f"{key}-latest.osm.pbf")
    if not os.path.exists(pbf):
        return
    s3.upload_file(pbf, bucket, f"{key}-latest.osm.pbf", Config=_tc())
    state = os.path.join(out, f"{key}-updates", "state.txt")
    if os.path.exists(state):
        s3.upload_file(state, bucket, f"{key}-updates/state.txt")


def _published_region_ages(s3, bucket: str, prefix: str) -> dict[str, float]:
    """Region key -> AGE IN DAYS of what is published under ``prefix/``.

    Ages, not a bare key set, because "already published" answers two different
    questions and they need different answers: *resume* (this run published it
    minutes ago — skip) and *refresh* (July published it — rebuild).
    """
    import datetime as _dt

    ages: dict[str, float] = {}
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix.strip('/')}/"):
            for o in page.get("Contents", []):
                k = o["Key"]
                if not k.endswith("-latest.osm.pbf"):
                    continue
                lm = o.get("LastModified")
                age = (now - lm).total_seconds() / 86400 if lm else 0.0
                ages[k[: -len("-latest.osm.pbf")]] = age
    except Exception:
        pass
    return ages


def _regions_to_skip(published: dict[str, float], *, refresh_after_days: float,
                     force_refresh: bool) -> set[str]:
    """Which already-published regions to leave alone.

    ⚠️ Refresh is deliberately STALENESS-based rather than a boolean force,
    because the skip is load-bearing for resume: a large set (e.g. ~400 German
    Kreise) converges across task retries only because each retry skips what the
    last one finished. A blunt force flag would make every retry restart from
    zero and the set would never converge.

    Age separates the two cases cleanly. With ``refresh_after_days=7`` a retry
    ten minutes later still skips (those objects are minutes old) while a weekly
    run rebuilds anything a week stale — resume and refresh from one knob.

    ``force_refresh`` remains as the explicit "rebuild everything now" escape
    hatch, for when you know the data is wrong rather than merely old.
    """
    if force_refresh:
        return set()
    if refresh_after_days > 0:
        return {k for k, age in published.items() if age < refresh_after_days}
    return set(published)                     # 0 = never refresh (the default)


def _publish_tree(s3, out: str, bucket: str, log, only_keys=None) -> int:
    """Upload a local ``<key>-latest.osm.pbf`` + ``<key>-updates/state.txt`` tree to
    ``bucket`` (created anonymous-read). Returns the count published.

    ``only_keys`` restricts the upload to the regions this run actually produced.

    ⚠️ It matters. Unrestricted, the glob over the default ``out`` (the served
    tree) matched 59 files — the 51 US states AND all 8 CONTINENT extracts, ~95 GB
    that the run never built. That is not merely wasteful: it OVERWRITES the
    canonical continents in the bucket with whatever happens to sit on this host's
    disk, and those two stores are deliberately not interchangeable. Measured
    2026-08-30, it also failed outright — CompleteMultipartUpload returned
    InvalidPart on the 38 GB europe object after the task was reclaimed mid-upload.

    A zero-length file is never published: a killed osmium leaves truncated
    outputs behind, and replacing a good remote extract with an empty one is the
    worst outcome available here.
    """
    import glob
    _ensure_public_bucket(s3, bucket)
    wanted = set(only_keys) if only_keys else None
    published = skipped_foreign = skipped_empty = 0
    for pbf in sorted(glob.glob(os.path.join(out, "**", "*-latest.osm.pbf"), recursive=True)):
        key = os.path.relpath(pbf, out)[: -len("-latest.osm.pbf")]
        if wanted is not None and key not in wanted:
            skipped_foreign += 1
            continue
        try:
            if os.path.getsize(pbf) == 0:
                log(f"REFUSING to publish {key}: zero-length local file")
                skipped_empty += 1
                continue
        except OSError:
            continue
        _publish_one(s3, out, key, bucket)
        published += 1
        if published % 10 == 0:
            log(f"published {published} extracts")
    if skipped_foreign:
        log(f"scoped publish: {published} published, {skipped_foreign} file(s) outside this run left alone")
    if skipped_empty:
        log(f"⚠️ {skipped_empty} zero-length extract(s) NOT published")
    return published


# Last-resort in-cluster default. Prefer the deployment's configured endpoint:
# a literal repeated at each call site cannot follow a store move, and the fleet
# already carries the value (fleet_config -> FW_GEOFABRIK_BASE_URL).
_DEFAULT_EXTRACT_BASE_URL = "http://afl-minio:9000/osm-extracts"


# Seconds between liveness signals during a blocking phase. Well under the
# stuck-task and execution windows, and cheap: one small Mongo update.
_HEARTBEAT_INTERVAL_S = 30.0


@contextlib.contextmanager
def _heartbeating(params: dict[str, Any], what: str):
    """Keep the task's liveness signal alive across ONE BLOCKING CALL.

    The documented batch pattern puts `_task_heartbeat` at a loop boundary, but
    this handler's long phases have no loop to hook: a multi-GB `download_file`
    and an `osmium` pass are each a single call that blocks for tens of minutes.
    With nothing heartbeating, the runtime concludes the execution is dead and
    re-dispatches the task WHILE IT IS STILL RUNNING.

    Measured 2026-08-25 on `europe` @ admin_level 2: the task reached
    retry_count 2 and produced TWO concurrent executions, one per osm-capable
    host, each having re-downloaded the same 40.5 GB extract and each running a
    competing osmium pass. Nothing failed and no error was recorded — the run
    simply duplicated itself until it was terminated by hand.

    A ticker thread is the right shape here precisely BECAUSE the work is
    opaque: it reports elapsed time rather than progress, which is honest about
    what we can actually observe from outside a blocking C call.
    """
    hb = params.get("_task_heartbeat")
    if not callable(hb):
        yield
        return
    stop = threading.Event()

    def _tick():
        waited = 0.0
        while not stop.wait(_HEARTBEAT_INTERVAL_S):
            waited += _HEARTBEAT_INTERVAL_S
            try:
                hb(progress_message=f"{what} ({waited / 60:.0f} min elapsed)")
            except Exception:  # noqa: BLE001 - liveness must never kill the work
                pass

    t = threading.Thread(target=_tick, name="fw-heartbeat", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


def _extract_base_url(params: dict[str, Any]) -> str:
    """Extract-store URL: explicit param > deployment config > in-cluster default."""
    return (
        params.get("base_url")
        or os.environ.get("FW_OSM_EXTRACT_BASE_URL")
        or os.environ.get("FW_GEOFABRIK_BASE_URL")
        or _DEFAULT_EXTRACT_BASE_URL
    )


def handle_publish_extracts(params: dict[str, Any]) -> dict[str, Any]:
    """Upload a local extract tree to an S3/MinIO bucket (anonymous-read)."""
    out = params.get("out") or os.path.join(_PLANET_DIR, "www")
    bucket = params.get("bucket") or os.environ.get("FW_OSM_EXTRACT_BUCKET", "osm-extracts")
    # The LAST long blocking call on this path without a liveness signal. It
    # uploads the whole extract tree — 12 GB for the 51 US states — and a
    # reclaim mid-upload would run a second uploader over the same objects.
    # Same omission as DownloadPlanet/UpdatePlanet (87b6902) and ExtractRegions
    # (7909e94): the pattern is that every one of these was found only after it
    # failed in production, so this one is fixed on inspection instead.
    with cancellable(params.get("_cancellation_check")):
        with _heartbeating(params, f"publishing the extract tree to {bucket}"):
            keys = [r.get("key") for r in (params.get("regions") or []) if r.get("key")]
            published = _publish_tree(_s3_client(params.get("endpoint")), out, bucket,
                                      _log(params), only_keys=keys or None)
    return {"bucket": bucket, "published": published}


def handle_list_extracts(params: dict[str, Any]) -> dict[str, Any]:
    """List the DIRECT-CHILD extract keys under a prefix (e.g. the 51 US states under
    ``north-america/us``) — the fan-out driver for a per-child workflow. Returns only
    keys exactly one level below ``prefix`` (so states, not their nested counties)."""
    prefix = (params.get("prefix") or "").strip("/")
    bucket = params.get("bucket") or os.environ.get("FW_OSM_EXTRACT_BUCKET", "osm-extracts")
    s3 = _s3_client(params.get("endpoint"))
    depth = prefix.count("/") + 1 if prefix else 0
    suffix = "-latest.osm.pbf"
    keys = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for o in page.get("Contents", []):
            k = o["Key"]
            if k.endswith(suffix):
                region = k[: -len(suffix)]
                if region.count("/") == depth:            # direct child only
                    keys.add(region)
    regions = sorted(keys)
    log = _log(params)
    log(f"list {prefix}: {len(regions)} direct-child extract(s)")
    return {"regions": regions, "count": len(regions)}


def handle_build_admin_set(params: dict[str, Any]) -> dict[str, Any]:
    """Split a region into admin-level children (cancellation-aware wrapper)."""
    # Make this thread's osmium passes interruptible for the duration. Without
    # it, terminating the workflow marks Mongo terminal while the passes keep
    # running — measured 2026-08-25, two hosts burned CPU until the containers
    # were restarted by hand.
    with cancellable(params.get("_cancellation_check")):
        return _build_admin_set(params)


def _build_admin_set(params: dict[str, Any]) -> dict[str, Any]:
    """SINGLE-TASK admin set — the distributed-fleet-safe path. Downloads a source
    region from the bucket, generates its ``admin_level`` boundaries, extracts each,
    and publishes — ALL on the one host that claims this task, so there's no
    cross-host local-file handoff. Scoped by the source, e.g.
    ``source_region='europe/germany', admin_level=4`` → German Länder."""
    import shutil
    source_region = params.get("source_region") or ""
    if not source_region:
        raise ValueError("BuildAdminSet: 'source_region' required (e.g. 'europe/germany')")
    try:
        admin_level = int(params.get("admin_level") or 4)
    except (TypeError, ValueError):
        admin_level = 4
    bucket = params.get("bucket") or os.environ.get("FW_OSM_EXTRACT_BUCKET", "osm-extracts")
    base_url = _extract_base_url(params)
    strategy = params.get("strategy") or "complete_ways"
    # Region-aware straggler fallback (TIGER for US states, osmfr elsewhere). The
    # param stays named osmfr_fallback for back-compat; it's the on/off toggle.
    subregion_fallback = params.get("osmfr_fallback", True) is not False
    # batch_size 0 = ADAPTIVE (default): bootstrap_batched detects the memory ceiling,
    # keeps each osmium pass under a fraction of it, measures the real peak, learns a
    # per-region cost (persisted), and self-heals on OOM. Pass batch_size>0 to force a
    # fixed count. (osmium holds a ~1.5 GB node-id bitmap per region + a dense province
    # can approach the ~14 GB Docker-VM ceiling alone, which is what the batcher sizes for.)
    try:
        batch_size = int(params.get("batch_size") or 0)
    except (TypeError, ValueError):
        batch_size = 0
    # Refresh controls. Default 0/false = the pre-existing behaviour: anything
    # already published is skipped forever, which is why the July sub-region
    # extracts could never be regenerated by re-running this.
    try:
        refresh_after_days = float(params.get("refresh_after_days") or 0)
    except (TypeError, ValueError):
        refresh_after_days = 0.0
    force_refresh = params.get("force_refresh", False) is True
    cost_dir = os.environ.get("FW_OSM_COST_DIR") or os.path.expanduser("~/.facetwork/osm-cost")
    log = _log(params)
    s3 = _s3_client(params.get("endpoint"))

    work = _scratch_dir()
    src = os.path.join(work, f"{source_region.replace('/', '__')}.osm.pbf")
    log(f"downloading source {source_region} from s3://{bucket}")

    def _abort_if_cancelled(_bytes: int) -> None:
        # boto3 invokes this per chunk; raising aborts the transfer. Checking
        # only around the call would leave a ~30 min download running after a
        # terminate — the single longest uninterruptible stretch in this handler.
        raise_if_cancelled()

    with _heartbeating(params, f"downloading {source_region}"):
        s3.download_file(bucket, f"{source_region}-latest.osm.pbf", src,
                         Callback=_abort_if_cancelled)

    # country_prefix=source_region → sub-regions key consistently under the source
    # country (fixes the ISO→continent quirk, e.g. mexico) and lets county-level
    # (admin_level>=6, no ISO 3166-2) units through instead of being dropped.
    raise_if_cancelled()
    # country_prefix is what keys sub-national units under their COUNTRY
    # (europe/germany/bayern). At admin_level <= 2 the children ARE countries,
    # and that branch requires ISO 3166-2 ("DE-BY") — a country carries ISO
    # 3166-1 ("DE"), so every one is dropped as island noise. Measured
    # 2026-08-26: `europe` @ 2 assembled 94 countries and kept ZERO.
    #
    # The standalone path (country_prefix=None) is built for exactly this case:
    # _geofabrik_key maps a level-2 ISO 3166-1 code to `<continent>/<country>`,
    # which is the key layout the bucket already uses.
    prefix = source_region if int(admin_level) > 2 else None
    with _heartbeating(params, f"extracting admin_level={admin_level} boundaries"):
        regions = generate_polygons(src, admin_level, os.path.join(work, "polys"),
                                    country_prefix=prefix, on_log=log)
    poly_regions = [{"key": r.key, "poly": r.poly} for r in regions]

    # Straggler fallback: osmium export can't assemble some boundaries (nested
    # sub-relations, member ways beyond the source poly). Fill the GAP from a
    # region-AND-level-aware ready-made poly source (TIGER US states@4 / US
    # counties@6, osmfr subregions@4). It returns [] for a (country, level) it has
    # no provider for — so e.g. German Kreise (europe/germany@6) get no wrong-level
    # Länder injected; US counties (north-america/us/<state>@6) get TIGER counties.
    if subregion_fallback and admin_level >= 4:
        have = {r["key"].rsplit("/", 1)[-1] for r in poly_regions}
        extra = [f for f in fetch_country_subregions(
                     source_region, os.path.join(work, "polys_fallback"),
                     admin_level=admin_level, on_log=log)
                 if f.key.rsplit("/", 1)[-1] not in have]
        if extra:
            log(f"straggler fallback adds {len(extra)}: "
                f"{[f.key.rsplit('/', 1)[-1] for f in extra]}")
            poly_regions += [{"key": f.key, "poly": f.poly} for f in extra]

    out_dir = os.path.join(work, "out")
    _ensure_public_bucket(s3, bucket)

    # RESUME: skip regions a prior attempt already published, so a large set (e.g.
    # ~400 German counties) CONVERGES across task retries instead of restarting from
    # zero each time. Only meaningful for multi-region sets under this exact prefix.
    published_ages = _published_region_ages(s3, bucket, source_region)
    already = _regions_to_skip(published_ages, refresh_after_days=refresh_after_days,
                               force_refresh=force_refresh)
    todo = [r for r in poly_regions if r["key"] not in already]
    stale = len(published_ages) - len(already)

    # ⚠️ ORPHANS: published under this prefix but NOT produced by this run's
    # boundary generation. These can never be refreshed by this path — no amount
    # of force_refresh reaches them — because OSM has no admin_level relation for
    # them (or one without the ISO code the filter requires). They are typically
    # left over from an earlier build that used a different source, e.g. the
    # Census TIGER county set.
    #
    # Measured 2026-08-31 across the 51 US states: OSM admin_level=6 yields 2,522
    # of the 3,167 published counties, so 645 (20.4%) are orphans — alabama 57 of
    # 67, texas 227 of 257, georgia 143 of 159. Until this was reported, every run
    # looked complete while a fifth of the tier aged indefinitely, which is the
    # same silent staleness that let the whole sub-region tier reach 36 days.
    generated_keys = {r["key"] for r in poly_regions}
    orphans = sorted(k for k in published_ages if k not in generated_keys)
    if force_refresh:
        log(f"force refresh: rebuilding all {len(poly_regions)} region(s)")
    elif refresh_after_days > 0:
        log(f"refresh>{refresh_after_days}d: {len(already)} still fresh (skipped), "
            f"{stale} stale, {len(todo)} to build")
    elif len(todo) < len(poly_regions):
        log(f"resume: {len(poly_regions) - len(todo)} already published, {len(todo)} to go")

    # INCREMENTAL publish: upload each pass's extracts immediately, so progress is
    # durable even if the task times out mid-run (the next retry resumes from here).
    published = {"n": len(poly_regions) - len(todo)}

    def _on_pass(results):
        # Batch boundary: the cheapest honest place to stop between passes.
        raise_if_cancelled()
        for rr in results:
            _publish_one(s3, out_dir, rr.key, bucket)
            published["n"] += 1
        if results:
            log(f"published {published['n']}/{len(poly_regions)}")
        # The one place with real progress to report, so report it here rather
        # than only elapsed time.
        hb = params.get("_task_heartbeat")
        if callable(hb):
            hb(progress_message=f"published {published['n']}/{len(poly_regions)}")

    # A single batch is itself a long osmium pass, so the per-batch heartbeat in
    # _on_pass is not enough on its own — cover the whole call too.
    raise_if_cancelled()
    with _heartbeating(params, f"extracting {len(todo)} region(s)"):
        bootstrap_batched(
            source=src, out=out_dir, regions=todo,
            base_url=base_url, strategy=strategy, batch_size=batch_size,
            cost_state_dir=cost_dir, on_pass=_on_pass, on_log=log)

    shutil.rmtree(work, ignore_errors=True)
    # Say BUILT and SKIPPED separately. Collapsing them made a total no-op read
    # exactly like a full rebuild: on 2026-08-31 us-counties logged
    # "227 extracts published" for texas while building ZERO — every county was
    # skipped by resume (refresh_after_days had defaulted to 0 = never refresh)
    # and the bucket still held a 35-day-old vintage. rc=0, green, useless.
    # A refresh job that does nothing must not be indistinguishable from one
    # that did everything.
    skipped = len(poly_regions) - len(todo)
    built = published["n"]
    if orphans:
        oldest = max(published_ages[k] for k in orphans)
        sample = ", ".join(k.rsplit("/", 1)[-1] for k in orphans[:3])
        log(f"⚠️ {len(orphans)} published region(s) under {source_region} are NOT "
            f"reproducible at admin_level={admin_level} — this path can never "
            f"refresh them (oldest {oldest:.0f}d; e.g. {sample}"
            f"{', …' if len(orphans) > 3 else ''})")
    if skipped and not built:
        log(f"admin_level={admin_level} of {source_region}: NOTHING BUILT — "
            f"all {skipped} region(s) skipped as already-current "
            f"(refresh_after_days={refresh_after_days}; 0 means never refresh)")
    else:
        log(f"admin_level={admin_level} of {source_region}: {built} built, "
            f"{skipped} skipped as current ({len(poly_regions)} total)")
    # `unreproducible` is returned, not just logged, so a fan-out can aggregate the
    # shortfall instead of each parent reporting it into a log nobody sums.
    return {"region_count": len(poly_regions), "published": built,
            "unreproducible": len(orphans)}


_DISPATCH = {
    f"{NAMESPACE}.DownloadPlanet": handle_download_planet,
    f"{NAMESPACE}.UpdatePlanet": handle_update_planet,
    f"{NAMESPACE}.DownloadPolygons": handle_download_polygons,
    f"{NAMESPACE}.GenerateRegionPolygons": handle_generate_polygons,
    f"{NAMESPACE}.ExtractRegions": handle_extract_regions,
    f"{NAMESPACE}.PublishExtracts": handle_publish_extracts,
    f"{NAMESPACE}.BuildAdminSet": handle_build_admin_set,
    f"{NAMESPACE}.ListExtracts": handle_list_extracts,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner. All steps do blocking network/disk I/O, so
    timeout_ms=0 (rely on the runner's global execution timeout)."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
            timeout_ms=0,
        )


def register_planet_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
