"""Heartbeat helpers for long-running I/O operations.

When loading large JSON files from slow/FUSE mounts (e.g. VirtioFS over
SMB in Docker Desktop), the entire container can stall — blocking all
threads, subprocesses, and heartbeats.

The solution is to **copy the file to local storage first** using ``dd``
or ``cp`` (which are separate processes that don't contend with the
Python GIL), then load the local copy with ``json.load()``.  During the
copy phase a heartbeat subprocess pings MongoDB.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

# Inline script for heartbeat subprocess.
# Tries to lower its OOM score so the kernel prefers to kill other processes.
# Uses pymongo with a short-lived client to minimize memory footprint.
_HEARTBEAT_SCRIPT = r"""
import os, sys, time
# Try to protect ourselves from OOM killer
try:
    with open(f'/proc/{os.getpid()}/oom_score_adj', 'w') as f:
        f.write('-999\n')
except Exception:
    pass
task_uuid, mongo_uri, db_name, interval_s = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
import pymongo
client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
db = client[db_name]
while True:
    try:
        now = int(time.time() * 1000)
        db.tasks.update_one({"uuid": task_uuid, "state": "running"}, {"$set": {"task_heartbeat": now}})
    except Exception:
        pass
    time.sleep(interval_s)
"""


def _local_cache_dir() -> str:
    from facetwork.config import get_output_base

    return os.path.join(get_output_base(), "cache", "osm-local")


# Files larger than this are copied locally before parsing (100 MB)
_SIZE_THRESHOLD = 100 * 1024 * 1024


def _resolve_mongo_config() -> tuple[str, str]:
    """Resolve MongoDB URI and database name from environment."""
    db_name = os.environ.get("AFL_MONGODB_DATABASE", "facetwork")
    uri = os.environ.get("AFL_MONGODB_URL", "")
    if not uri:
        # Legacy fallback
        host = os.environ.get("AFL_MONGO_HOST", "afl-mongodb")
        port = os.environ.get("AFL_MONGO_PORT", "27017")
        uri = f"mongodb://{host}:{port}"
    return uri, db_name


def _is_network_mount(path: str) -> bool:
    """Heuristic: treat /Volumes/ paths as potentially slow network mounts."""
    return path.startswith("/Volumes/")


def _local_cache_path(source_path: str) -> str:
    """Derive a local cache path for a source file."""
    basename = os.path.basename(source_path)
    cache_dir = _local_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, basename)


def _needs_local_copy(source_path: str) -> bool:
    """Check if the file should be copied to local storage before loading."""
    if not _is_network_mount(source_path):
        return False
    try:
        size = os.path.getsize(source_path)
        return size > _SIZE_THRESHOLD
    except OSError:
        return False


def json_load_with_heartbeat(
    f: Any,
    heartbeat: callable | None = None,
    interval: float = 30.0,
    task_uuid: str = "",
) -> Any:
    """Load JSON, copying large network files locally first.

    For files on network mounts (``/Volumes/...``) larger than 100 MB,
    the file is first copied to ``AFL_OUTPUT_BASE/cache/osm-local/`` using ``cp``
    (a separate process immune to GIL stalls), with a heartbeat
    subprocess pinging MongoDB during the copy.  The local copy is then
    loaded with ``json.load()`` at local-disk speed.

    For small files or local paths, falls back to plain ``json.load(f)``.
    """
    # Try to get the source path from the file object
    source_path = getattr(f, "name", "")

    if source_path and _needs_local_copy(source_path):
        # Close the original file handle — we'll re-open the local copy
        f.close()

        local_path = _local_cache_path(source_path)

        # Start heartbeat subprocess that stays alive during copy AND parse
        mongo_uri = ""
        db_name = "facetwork"
        hb_proc = None
        if task_uuid:
            try:
                mongo_uri, db_name = _resolve_mongo_config()
            except Exception:
                pass
        if task_uuid and mongo_uri:
            try:
                import shlex

                # Use a shell wrapper that auto-restarts the Python heartbeat
                # if it gets OOM-killed during heavy JSON parsing.
                py_args = shlex.join(
                    [
                        task_uuid,
                        mongo_uri,
                        db_name,
                        str(interval),
                    ]
                )
                shell_cmd = (
                    f"while true; do "
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(_HEARTBEAT_SCRIPT)} "
                    f"{py_args} 2>/dev/null; "
                    f"sleep 2; done"
                )
                hb_proc = subprocess.Popen(
                    ["sh", "-c", shell_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    # Start in new process group so we can kill the whole tree
                    preexec_fn=os.setsid,
                )
                log.info(
                    "Heartbeat subprocess started (pid=%s) for task %s", hb_proc.pid, task_uuid
                )
            except Exception as exc:
                log.warning("Failed to start heartbeat subprocess: %s", exc)

        try:
            # Copy if needed
            try:
                src_size = os.path.getsize(source_path)
                local_size = os.path.getsize(local_path) if os.path.exists(local_path) else -1
                if src_size == local_size:
                    log.info("Using cached local copy: %s (%d bytes)", local_path, local_size)
                else:
                    log.info("Copying %s -> %s (%d bytes)", source_path, local_path, src_size)
                    subprocess.run(["cp", source_path, local_path], check=True)
                    log.info("Copy complete: %s", local_path)
            except OSError:
                log.info("Copying %s -> %s", source_path, local_path)
                subprocess.run(["cp", source_path, local_path], check=True)
                log.info("Copy complete: %s", local_path)

            # Load from fast local storage
            with open(local_path) as local_f:
                data = json.load(local_f)
            return data
        finally:
            if hb_proc is not None:
                import signal

                # Kill the entire process group (shell + python child)
                try:
                    os.killpg(os.getpgid(hb_proc.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    hb_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(hb_proc.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass

    # Normal path: small file or local storage
    return json.load(f)
