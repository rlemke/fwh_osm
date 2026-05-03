"""Valhalla tile-set build library.

Profiles are query-time in Valhalla (unlike GraphHopper), so there is no
per-profile directory — one tileset per region at
``cache/osm/valhalla/<region>-latest/`` with a sibling sidecar.

Cache validity requires:
- Source PBF's SHA-256 still matches, AND
- Recorded ``valhalla_version`` matches ``VALHALLA_VERSION`` here.

Requires ``valhalla_build_config`` and ``valhalla_build_tiles``
(``brew install valhalla`` on macOS).
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
OUTPUT_CACHE_TYPE = "valhalla"

VALHALLA_VERSION = "3.5"

QUERY_PROFILES: tuple[str, ...] = (
    "auto",
    "bicycle",
    "pedestrian",
    "truck",
    "motor_scooter",
    "motorcycle",
    "bus",
    "taxi",
)

DEFAULT_TIMEOUT_SECONDS = 3600

_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(region: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(region)
        if lock is None:
            lock = threading.Lock()
            _build_locks[region] = lock
        return lock


@dataclass
class BuildResult:
    region: str
    tile_dir: str
    relative_path: str
    total_size_bytes: int
    tile_count: int
    tile_levels: dict[str, int]
    valhalla_version: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class BuildError(RuntimeError):
    """Raised when a tile build fails."""


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def tileset_rel_path(region: str) -> str:
    return f"{region}-latest"


def tileset_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, tileset_rel_path(region), s))


def _staging_dir(region: str, storage: Any = None) -> Path:
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-valhalla-staging" / safe
    out = tileset_abs_path(region, storage)
    return out.with_name(out.name + ".staging")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _count_tiles(tile_dir: Path) -> tuple[int, dict[str, int]]:
    total = 0
    per_level: dict[str, int] = {}
    if not tile_dir.exists():
        return 0, per_level
    for tile in tile_dir.rglob("*.gph"):
        total += 1
        try:
            rel = tile.relative_to(tile_dir)
            level = rel.parts[0]
        except (ValueError, IndexError):
            continue
        per_level[level] = per_level.get(level, 0) + 1
    return total, per_level


def _tileset_exists(tile_dir: Path) -> bool:
    if not tile_dir.is_dir():
        return False
    for _ in tile_dir.rglob("*.gph"):
        return True
    return False


def is_up_to_date(
    region: str,
    pbf_side: dict,
    tile_dir: Path,
    storage: Any = None,
) -> bool:
    s = storage or LocalStorage()
    rel = tileset_rel_path(region)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    extra = existing.get("extra") or {}
    if extra.get("valhalla_version") != VALHALLA_VERSION:
        return False
    if not _tileset_exists(tile_dir):
        return False
    return True


def _generate_config(
    tile_dir: Path, config_path: Path, config_bin: str
) -> None:
    try:
        result = subprocess.run(
            [config_bin, "--mjolnir-tile-dir", str(tile_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BuildError(
            f"{config_bin!r} not found on PATH. "
            "Install Valhalla (e.g. 'brew install valhalla')."
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "(no stderr)"
        raise BuildError(
            f"valhalla_build_config failed (exit {result.returncode}): {stderr}"
        )
    if not result.stdout:
        raise BuildError("valhalla_build_config produced no output")
    config_path.write_text(result.stdout)


def _run_build(
    pbf_path: Path,
    config_path: Path,
    tiles_bin: str,
    timeout_seconds: int,
) -> None:
    cmd = [tiles_bin, "-c", str(config_path), str(pbf_path)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"valhalla_build_tiles timed out after {timeout_seconds}s "
            f"for {pbf_path.name}"
        ) from exc
    except FileNotFoundError as exc:
        raise BuildError(
            f"{tiles_bin!r} not found on PATH. "
            "Install Valhalla (e.g. 'brew install valhalla')."
        ) from exc
    if result.returncode != 0:
        stderr_tail = (result.stderr or "").splitlines()[-20:]
        raise BuildError(
            f"valhalla_build_tiles failed (exit {result.returncode}): "
            f"{'; '.join(stderr_tail) or '(no stderr)'}"
        )


def build_tiles(
    region: str,
    *,
    force: bool = False,
    config_bin: str = "valhalla_build_config",
    tiles_bin: str = "valhalla_build_tiles",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> BuildResult:
    """Build a Valhalla tileset from a region's cached PBF."""
    s = storage or LocalStorage()
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

    with _build_lock(region):
        tile_dir = tileset_abs_path(region, s)
        rel = tileset_rel_path(region)

        if not force and is_up_to_date(region, pbf_side, tile_dir, s):
            tile_count, levels = _count_tiles(tile_dir)
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s) or {}
            return BuildResult(
                region=region,
                tile_dir=str(tile_dir),
                relative_path=rel + "/",
                total_size_bytes=existing.get("size_bytes", _dir_size(tile_dir)),
                tile_count=tile_count,
                tile_levels=levels,
                valhalla_version=VALHALLA_VERSION,
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_url=source_url,
                source_pbf_path=str(src_pbf),
                sidecar=existing,
            )

        staging = _staging_dir(region, s)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        staging_tiles = staging / "tiles"
        staging_tiles.mkdir(parents=True, exist_ok=True)
        config_path = staging / "valhalla.json"

        start = time.monotonic()
        try:
            _generate_config(staging_tiles, config_path, config_bin)
            _run_build(src_pbf, config_path, tiles_bin, timeout_seconds)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        elapsed = time.monotonic() - start

        tile_count, levels = _count_tiles(staging_tiles)
        if tile_count == 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise BuildError(
                f"valhalla_build_tiles produced no tiles for {region}"
            )

        s.finalize_dir_from_local(str(staging_tiles), str(tile_dir))
        shutil.rmtree(staging, ignore_errors=True)

        total_size = _dir_size(tile_dir)
        generated_at = sidecar.utcnow_iso()
        # Primary sha: first .gph file encountered (stable key).
        primary_sha = ""
        for first_tile in tile_dir.rglob("*.gph"):
            primary_sha = _sha256_file(first_tile)
            break

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
                "command": "valhalla_build_tiles",
                "config_bin": config_bin,
                "tiles_bin": tiles_bin,
            },
            extra={
                "region": region,
                "valhalla_version": VALHALLA_VERSION,
                "tile_count": tile_count,
                "tile_levels": levels,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return BuildResult(
            region=region,
            tile_dir=str(tile_dir),
            relative_path=rel + "/",
            total_size_bytes=total_size,
            tile_count=tile_count,
            tile_levels=levels,
            valhalla_version=VALHALLA_VERSION,
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


def clean_tiles(tile_dir: str) -> bool:
    p = Path(tile_dir)
    if p.exists():
        shutil.rmtree(p)
        return True
    return False


def to_valhalla_cache(result: BuildResult) -> dict[str, Any]:
    return {
        "osmSource": result.source_pbf_path,
        "tileDir": result.tile_dir,
        "date": result.generated_at,
        "size": result.total_size_bytes,
        "wasInCache": result.was_cached,
        "version": result.valhalla_version,
        "tileCount": result.tile_count,
    }
