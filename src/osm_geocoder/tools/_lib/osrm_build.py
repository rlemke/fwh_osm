"""OSRM routing-graph build library (MLD algorithm).

Runs ``osrm-extract`` → ``osrm-partition`` → ``osrm-customize`` against
a cached PBF and produces per-(region, profile) graph directories at
``cache/osm/osrm/<region>-latest/<profile>/`` with sibling sidecars.

Cache validity requires:
- Source PBF's SHA-256 match, AND
- ``osrm_version`` match, AND
- Profile match.

Requires ``osrm-extract``, ``osrm-partition``, ``osrm-customize`` from
``osrm-backend`` (``brew install osrm-backend``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _lib import sidecar
from _lib.storage import LocalStorage

NAMESPACE = "osm"
SOURCE_CACHE_TYPE = "pbf"
OUTPUT_CACHE_TYPE = "osrm"

OSRM_VERSION = "5.27"

PROFILES: tuple[str, ...] = ("car", "bicycle", "foot")

DEFAULT_TIMEOUT_SECONDS = 3600

_PROFILE_SEARCH_PATHS: tuple[str, ...] = (
    "/opt/homebrew/share/osrm/profiles",
    "/usr/local/share/osrm/profiles",
    "/usr/share/osrm/profiles",
    "/usr/local/share/osrm-backend/profiles",
)

_build_locks: dict[tuple[str, str], threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(region: str, profile: str) -> threading.Lock:
    key = (region, profile)
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


@dataclass
class BuildResult:
    region: str
    profile: str
    graph_dir: str
    relative_path: str
    total_size_bytes: int
    osrm_version: str
    algorithm: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class BuildError(RuntimeError):
    pass


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def graph_rel_path(region: str, profile: str) -> str:
    return f"{region}-latest/{profile}"


def graph_abs_path(region: str, profile: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, graph_rel_path(region, profile), s))


def _staging_dir(region: str, profile: str, storage: Any = None) -> Path:
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-osrm-staging" / safe / profile
    out = graph_abs_path(region, profile, storage)
    return out.with_name(out.name + ".tmp")


def default_profile_file(profile: str) -> str | None:
    for base in _PROFILE_SEARCH_PATHS:
        candidate = Path(base) / f"{profile}.lua"
        if candidate.is_file():
            return str(candidate)
    return None


def _osrm_binary_version(bin_path: str) -> str:
    try:
        r = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = (r.stdout or r.stderr or "").splitlines()
        return out[0].strip() if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _graph_exists(graph_dir: Path) -> bool:
    if not graph_dir.is_dir():
        return False
    for entry in graph_dir.iterdir():
        if entry.name.endswith(".osrm") or entry.name.endswith(".osrm.mldgr"):
            return True
    return False


def is_up_to_date(
    region: str,
    profile: str,
    pbf_side: dict,
    graph_dir: Path,
    storage: Any = None,
) -> bool:
    if profile not in PROFILES:
        return False
    s = storage or LocalStorage()
    rel = graph_rel_path(region, profile)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    extra = existing.get("extra") or {}
    if extra.get("osrm_version") != OSRM_VERSION:
        return False
    if not _graph_exists(graph_dir):
        return False
    return True


def _run(cmd: list[str], *, timeout: int) -> None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"{cmd[0]} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise BuildError(
            f"{cmd[0]!r} not found on PATH. Install osrm-backend "
            "('brew install osrm-backend' or tools/install-tools.sh)."
        ) from exc
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").splitlines()[-10:]
        raise BuildError(
            f"{cmd[0]} failed (exit {result.returncode}): "
            f"{'; '.join(stderr_tail) or '(no stderr)'}"
        )


def build_graph(
    region: str,
    profile: str,
    *,
    force: bool = False,
    profile_file: str | None = None,
    extract_bin: str = "osrm-extract",
    partition_bin: str = "osrm-partition",
    customize_bin: str = "osrm-customize",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> BuildResult:
    """Build an OSRM MLD routing graph for (region, profile)."""
    if profile not in PROFILES:
        raise BuildError(
            f"unknown profile: {profile!r}. Valid: {', '.join(PROFILES)}"
        )
    s = storage or LocalStorage()
    lua_path = profile_file or default_profile_file(profile)
    if not lua_path or not Path(lua_path).is_file():
        raise BuildError(
            f"profile .lua for {profile!r} not found. "
            f"Pass --profile-file or set OSRM_PROFILES_DIR. "
            f"Searched: {', '.join(_PROFILE_SEARCH_PATHS)}"
        )

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise BuildError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )
    src_pbf = pbf_abs_path(region, s)
    if not src_pbf.exists():
        raise BuildError(f"pbf file missing on disk: {src_pbf}")
    source_url = pbf_side.get("source", {}).get("url", "")

    with _build_lock(region, profile):
        graph_dir = graph_abs_path(region, profile, s)
        rel = graph_rel_path(region, profile)

        if not force and is_up_to_date(region, profile, pbf_side, graph_dir, s):
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s) or {}
            return BuildResult(
                region=region,
                profile=profile,
                graph_dir=str(graph_dir),
                relative_path=rel + "/",
                total_size_bytes=existing.get("size_bytes", _dir_size(graph_dir)),
                osrm_version=OSRM_VERSION,
                algorithm="mld",
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        staging = _staging_dir(region, profile, s)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        safe = region.replace("/", "_")
        staged_pbf = staging / f"{safe}-latest.osm.pbf"
        staged_pbf.symlink_to(src_pbf)
        osrm_base = staging / f"{safe}-latest.osrm"

        start = time.monotonic()
        try:
            _run(
                [extract_bin, "-p", lua_path, str(staged_pbf)],
                timeout=timeout_seconds,
            )
            _run([partition_bin, str(osrm_base)], timeout=timeout_seconds)
            _run([customize_bin, str(osrm_base)], timeout=timeout_seconds)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        elapsed = time.monotonic() - start

        staged_pbf.unlink()

        if not _graph_exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
            raise BuildError(
                f"OSRM pipeline produced no graph files for {region}/{profile}"
            )
        s.finalize_dir_from_local(str(staging), str(graph_dir))

        total_size = _dir_size(graph_dir)
        generated_at = sidecar.utcnow_iso()

        primary_sha = ""
        osrm_main = graph_dir / f"{safe}-latest.osrm"
        if osrm_main.exists():
            primary_sha = _sha256_file(osrm_main)

        side = sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            rel,
            kind="directory",
            size_bytes=total_size,
            sha256=primary_sha,
            source={
                "namespace": NAMESPACE,
                "cache_type": SOURCE_CACHE_TYPE,
                "relative_path": pbf_rel,
                "sha256": pbf_side.get("sha256"),
                "size_bytes": pbf_side.get("size_bytes"),
                "source_checksum": pbf_side.get("source", {}).get("source_checksum"),
                "source_timestamp": pbf_side.get("source", {}).get("source_timestamp"),
                "downloaded_at": pbf_side.get("source", {}).get("downloaded_at"),
            },
            tool={
                "command": "osrm-extract | osrm-partition | osrm-customize",
                "extract_version": _osrm_binary_version(extract_bin),
            },
            extra={
                "region": region,
                "profile": profile,
                "osrm_version": OSRM_VERSION,
                "algorithm": "mld",
                "profile_file": lua_path,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return BuildResult(
            region=region,
            profile=profile,
            graph_dir=str(graph_dir),
            relative_path=rel + "/",
            total_size_bytes=total_size,
            osrm_version=OSRM_VERSION,
            algorithm="mld",
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_url=source_url,
            source_pbf_path=str(src_pbf),
            sidecar=side,
        )


def _sha256_file(path: Path) -> str:
    import hashlib
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def clean_graph(graph_dir: str) -> bool:
    p = Path(graph_dir)
    if p.exists():
        shutil.rmtree(p)
        return True
    return False
