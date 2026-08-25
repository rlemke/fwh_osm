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
import threading
from typing import Any

from ...tools._osm_tools.planet_fetch import fetch_planet, update_planet
from ...tools._osm_tools.polygon_fetch import fetch_polygons, fetch_country_subregions
from ...tools._osm_tools.planet_bootstrap import bootstrap_batched
from ...tools._osm_tools.boundary_gen import generate_polygons

NAMESPACE = "osm.planet"

# Defaults for empty-string params (env-overridable), so the workflow can run
# with no arguments on a host that sets FW_PLANET_DIR / FW_S3_* .
_PLANET_DIR = os.environ.get("FW_PLANET_DIR", "/Volumes/afl_data/osm-selfhost")


def _planet_path(dest: str) -> str:
    return dest or os.path.join(_PLANET_DIR, "planet-latest.osm.pbf")


def _log(params: dict[str, Any]):
    sl = params.get("_step_log")
    return (lambda m: sl(m, level="info")) if sl else (lambda m: None)


def handle_download_planet(params: dict[str, Any]) -> dict[str, Any]:
    path = _planet_path(params.get("dest") or "")
    verify = params.get("verify", True) is not False
    res = fetch_planet(path, verify=verify, force=bool(params.get("force")), on_log=_log(params))
    return {"planet_path": res.path, "size_mb": round(res.size_bytes / 1_000_000, 1),
            "was_cached": res.was_cached}


def handle_update_planet(params: dict[str, Any]) -> dict[str, Any]:
    path = _planet_path(params.get("planet_path") or "")
    try:
        max_diff_mb = int(params.get("max_diff_mb") or 4096)
    except (TypeError, ValueError):
        max_diff_mb = 4096
    u = update_planet(path, max_diff_mb=max_diff_mb, on_log=_log(params))
    return {"planet_path": path, "status": u.status, "advanced": u.advanced}


def handle_download_polygons(params: dict[str, Any]) -> dict[str, Any]:
    dest = params.get("dest") or os.path.join(_PLANET_DIR, "polys")
    scope = params.get("scope") or "all"
    regions = fetch_polygons(dest, scope=scope, on_log=_log(params))
    return {"poly_dir": dest, "region_count": len(regions),
            "regions": [{"key": r.key, "poly": r.poly} for r in regions]}


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
    regions = generate_polygons(source, admin_level, dest, on_log=_log(params))
    return {"poly_dir": dest, "region_count": len(regions),
            "regions": [{"key": r.key, "poly": r.poly} for r in regions]}


def handle_extract_regions(params: dict[str, Any]) -> dict[str, Any]:
    planet = _planet_path(params.get("planet_path") or "")
    regions = params.get("regions") or []
    if not regions:
        raise ValueError("ExtractRegions: 'regions' is empty (run DownloadPolygons first)")
    out = params.get("out") or os.path.join(_PLANET_DIR, "www")
    base_url = _extract_base_url(params)
    strategy = params.get("strategy") or "complete_ways"
    try:
        batch_size = int(params.get("batch_size") or 25)
    except (TypeError, ValueError):
        batch_size = 25
    results = bootstrap_batched(source=planet, out=out, regions=regions, base_url=base_url,
                                strategy=strategy, batch_size=batch_size, on_log=_log(params))
    return {"region_count": len(results), "out": out}


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


def _published_region_keys(s3, bucket: str, prefix: str) -> set[str]:
    """Region keys already published under ``prefix/`` — for resume (skip done work)."""
    keys: set[str] = set()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix.strip('/')}/"):
            for o in page.get("Contents", []):
                k = o["Key"]
                if k.endswith("-latest.osm.pbf"):
                    keys.add(k[: -len("-latest.osm.pbf")])
    except Exception:
        pass
    return keys


def _publish_tree(s3, out: str, bucket: str, log) -> int:
    """Upload a local ``<key>-latest.osm.pbf`` + ``<key>-updates/state.txt`` tree to
    ``bucket`` (created anonymous-read). Returns the count published."""
    import glob
    _ensure_public_bucket(s3, bucket)
    published = 0
    for pbf in sorted(glob.glob(os.path.join(out, "**", "*-latest.osm.pbf"), recursive=True)):
        key = os.path.relpath(pbf, out)[: -len("-latest.osm.pbf")]
        _publish_one(s3, out, key, bucket)
        published += 1
        if published % 10 == 0:
            log(f"published {published} extracts")
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
    published = _publish_tree(_s3_client(params.get("endpoint")), out, bucket, _log(params))
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
    cost_dir = os.environ.get("FW_OSM_COST_DIR") or os.path.expanduser("~/.facetwork/osm-cost")
    log = _log(params)
    s3 = _s3_client(params.get("endpoint"))

    work = _scratch_dir()
    src = os.path.join(work, f"{source_region.replace('/', '__')}.osm.pbf")
    log(f"downloading source {source_region} from s3://{bucket}")
    with _heartbeating(params, f"downloading {source_region}"):
        s3.download_file(bucket, f"{source_region}-latest.osm.pbf", src)

    # country_prefix=source_region → sub-regions key consistently under the source
    # country (fixes the ISO→continent quirk, e.g. mexico) and lets county-level
    # (admin_level>=6, no ISO 3166-2) units through instead of being dropped.
    with _heartbeating(params, f"extracting admin_level={admin_level} boundaries"):
        regions = generate_polygons(src, admin_level, os.path.join(work, "polys"),
                                    country_prefix=source_region, on_log=log)
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
    already = _published_region_keys(s3, bucket, source_region)
    todo = [r for r in poly_regions if r["key"] not in already]
    if len(todo) < len(poly_regions):
        log(f"resume: {len(poly_regions) - len(todo)} already published, {len(todo)} to go")

    # INCREMENTAL publish: upload each pass's extracts immediately, so progress is
    # durable even if the task times out mid-run (the next retry resumes from here).
    published = {"n": len(poly_regions) - len(todo)}

    def _on_pass(results):
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
    with _heartbeating(params, f"extracting {len(todo)} region(s)"):
        bootstrap_batched(
            source=src, out=out_dir, regions=todo,
            base_url=base_url, strategy=strategy, batch_size=batch_size,
            cost_state_dir=cost_dir, on_pass=_on_pass, on_log=log)

    shutil.rmtree(work, ignore_errors=True)
    log(f"admin_level={admin_level} of {source_region}: {published['n']} extracts published")
    return {"region_count": len(poly_regions), "published": published["n"]}


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
