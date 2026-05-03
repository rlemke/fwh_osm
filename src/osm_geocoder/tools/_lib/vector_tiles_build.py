"""Vector-tile build library — GeoJSONSeq → PMTiles via ``tippecanoe``.

Takes any pre-filtered GeoJSONSeq produced by the tool set (the
whole-region ``geojson/`` output or a category extract) and produces a
PMTiles file at
``cache/osm/vector_tiles/<region>-latest/<source>.pmtiles`` with a
sibling sidecar.

Cache validity requires:
- Source GeoJSONSeq's SHA-256 still matches the upstream sidecar,
- ``tippecanoe_version`` still matches, AND
- Tiling options (min/max zoom, layer name) still match.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _lib import sidecar
from _lib.pbf_extract import CATEGORIES
from _lib.storage import LocalStorage

NAMESPACE = "osm"
OUTPUT_CACHE_TYPE = "vector_tiles"

DEFAULT_MIN_ZOOM = 0
DEFAULT_MAX_ZOOM = 14
DEFAULT_TIMEOUT_SECONDS = 3600
CHUNK_SIZE = 1024 * 1024

_build_locks: dict[tuple[str, str], threading.Lock] = {}
_build_locks_guard = threading.Lock()


def _build_lock(region: str, source: str) -> threading.Lock:
    key = (region, source)
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


GEOJSON_SOURCE = "geojson"


def valid_sources() -> list[str]:
    return [GEOJSON_SOURCE, *sorted(CATEGORIES)]


def _source_cache_type(source: str) -> str:
    """The cache_type within the ``osm`` namespace that produces ``source``."""
    if source == GEOJSON_SOURCE:
        return "geojson"
    if source in CATEGORIES:
        return source
    raise BuildError(
        f"unknown source: {source!r}. Valid: {', '.join(valid_sources())}"
    )


def _source_rel_path(source: str, region: str) -> str:
    return f"{region}-latest.geojsonseq"


def _source_abs_path(source: str, region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, _source_cache_type(source), _source_rel_path(source, region), s))


def _source_sidecar(source: str, region: str, storage: Any = None) -> dict | None:
    s = storage or LocalStorage()
    return sidecar.read_sidecar(NAMESPACE, _source_cache_type(source), _source_rel_path(source, region), s)


@dataclass
class BuildResult:
    region: str
    source: str
    path: str
    relative_path: str
    size_bytes: int
    sha256: str
    min_zoom: int
    max_zoom: int
    layer_name: str
    tippecanoe_version: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_path: str
    source_sha256: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class BuildError(RuntimeError):
    """Raised when vector-tile build fails."""


def tileset_rel_path(region: str, source: str) -> str:
    return f"{region}-latest/{source}.pmtiles"


def tileset_abs_path(region: str, source: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, tileset_rel_path(region, source), s))


def _staging_path(region: str, source: str, storage: Any = None) -> Path:
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-vector-tiles-staging" / safe / f"{source}.pmtiles"
    out = tileset_abs_path(region, source, storage)
    return out.with_name(out.name + ".tmp")


def _tippecanoe_version(tippecanoe_bin: str) -> str:
    try:
        result = subprocess.run(
            [tippecanoe_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = result.stdout or result.stderr or ""
        first = out.splitlines()
        return first[0].strip() if first else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha256_file(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def is_up_to_date(
    region: str,
    source: str,
    *,
    min_zoom: int,
    max_zoom: int,
    layer_name: str,
    storage: Any = None,
    source_sidecar: dict | None = None,
    existing_sidecar: dict | None = None,
) -> bool:
    """True if cached tileset matches current source SHA + build options.

    ``source_sidecar`` / ``existing_sidecar`` let callers pass sidecars
    they've already read, avoiding a duplicate disk read on the hot
    path. This matters in ``build_tiles`` which reads the source
    sidecar itself before calling us, and again in the cached-hit
    return path where we'd re-read the output sidecar otherwise.
    """
    s = storage or LocalStorage()
    rel = tileset_rel_path(region, source)
    existing = existing_sidecar
    if existing is None:
        existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)
    if not existing:
        return False
    source_side = source_sidecar
    if source_side is None:
        source_side = _source_sidecar(source, region, s)
    if not source_side:
        return False
    if existing.get("source", {}).get("sha256") != source_side.get("sha256"):
        return False
    opts = (existing.get("extra") or {}).get("options") or {}
    if opts.get("min_zoom") != min_zoom:
        return False
    if opts.get("max_zoom") != max_zoom:
        return False
    if opts.get("layer_name") != layer_name:
        return False
    out_abs = tileset_abs_path(region, source, s)
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == existing.get("size_bytes")


def build_tiles(
    region: str,
    source: str = GEOJSON_SOURCE,
    *,
    min_zoom: int = DEFAULT_MIN_ZOOM,
    max_zoom: int = DEFAULT_MAX_ZOOM,
    layer_name: str | None = None,
    force: bool = False,
    tippecanoe_bin: str = "tippecanoe",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> BuildResult:
    """Build a vector-tile PMTiles file from a GeoJSONSeq source."""
    if source not in valid_sources():
        raise BuildError(
            f"unknown source: {source!r}. Valid: {', '.join(valid_sources())}"
        )
    if not (0 <= min_zoom <= max_zoom <= 22):
        raise BuildError(
            f"invalid zoom range: min={min_zoom} max={max_zoom} (must satisfy 0 <= min <= max <= 22)"
        )
    effective_layer_name = layer_name or source
    s = storage or LocalStorage()

    src_side = _source_sidecar(source, region, s)
    if not src_side:
        raise BuildError(
            f"no {_source_cache_type(source)!r} sidecar for region {region!r}. "
            f"Run the producing tool first "
            f"({'convert-pbf-geojson' if source == GEOJSON_SOURCE else f'extract {source}'})."
        )
    src_path = _source_abs_path(source, region, s)
    if not src_path.exists():
        raise BuildError(f"source geojsonseq missing on disk: {src_path}")
    src_sha = src_side.get("sha256", "")

    with _build_lock(region, source):
        out_abs = tileset_abs_path(region, source, s)
        rel = tileset_rel_path(region, source)

        # Read the output sidecar once and pass it into both the
        # is_up_to_date check and the BuildResult builder below —
        # saves a redundant read on the cached-hit path.
        existing_side = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, rel, s)

        if not force and is_up_to_date(
            region,
            source,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            layer_name=effective_layer_name,
            storage=s,
            source_sidecar=src_side,
            existing_sidecar=existing_side,
        ):
            existing = existing_side or {}
            return BuildResult(
                region=region,
                source=source,
                path=str(out_abs),
                relative_path=rel,
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                layer_name=effective_layer_name,
                tippecanoe_version=existing.get("tool", {}).get("tippecanoe_version", ""),
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                source_path=str(src_path),
                source_sha256=src_sha,
                sidecar=existing,
            )

        staging = _staging_path(region, source, s)
        staging.parent.mkdir(parents=True, exist_ok=True)
        if staging.exists():
            staging.unlink()

        mbtiles_staging = staging.with_name(source + ".mbtiles")
        if mbtiles_staging.exists():
            mbtiles_staging.unlink()

        cmd = [
            tippecanoe_bin,
            "-o",
            str(mbtiles_staging),
            "-Z",
            str(min_zoom),
            "-z",
            str(max_zoom),
            "--force",
            "--layer",
            effective_layer_name,
            "--drop-densest-as-needed",
            "--coalesce-densest-as-needed",
            "--read-parallel",
            str(src_path),
        ]
        start = time.monotonic()
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            mbtiles_staging.unlink(missing_ok=True)
            stderr = (exc.stderr or "").strip()
            raise BuildError(f"tippecanoe failed: {stderr or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            mbtiles_staging.unlink(missing_ok=True)
            raise BuildError(
                f"tippecanoe timed out after {timeout_seconds}s"
            ) from exc
        except BaseException:
            mbtiles_staging.unlink(missing_ok=True)
            raise

        pmtiles_bin = os.environ.get("PMTILES_BIN", "pmtiles")
        try:
            subprocess.run(
                [pmtiles_bin, "convert", str(mbtiles_staging), str(staging)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=True,
            )
        except FileNotFoundError:
            mbtiles_staging.unlink(missing_ok=True)
            raise BuildError(
                f"'{pmtiles_bin}' not found. Install with: brew install pmtiles"
            )
        except subprocess.CalledProcessError as exc:
            mbtiles_staging.unlink(missing_ok=True)
            staging.unlink(missing_ok=True)
            stderr = (exc.stderr or "").strip()
            raise BuildError(f"pmtiles convert failed: {stderr or exc}") from exc
        finally:
            mbtiles_staging.unlink(missing_ok=True)

        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(staging)

        s.finalize_from_local(str(staging), str(out_abs))

        generated_at = sidecar.utcnow_iso()
        tippe_ver = _tippecanoe_version(tippecanoe_bin)
        side = sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            rel,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source={
                "namespace": NAMESPACE,
                "cache_type": _source_cache_type(source),
                "relative_path": _source_rel_path(source, region),
                "sha256": src_sha,
                "size_bytes": src_side.get("size_bytes"),
                "generated_at": src_side.get("generated_at", ""),
            },
            tool={
                "command": "tippecanoe",
                "tippecanoe_version": tippe_ver,
            },
            extra={
                "region": region,
                "source_kind": source,
                "options": {
                    "min_zoom": min_zoom,
                    "max_zoom": max_zoom,
                    "layer_name": effective_layer_name,
                },
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return BuildResult(
            region=region,
            source=source,
            path=str(out_abs),
            relative_path=rel,
            size_bytes=size,
            sha256=sha256_hex,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            layer_name=effective_layer_name,
            tippecanoe_version=tippe_ver,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_path=str(src_path),
            source_sha256=src_sha,
            sidecar=side,
        )
