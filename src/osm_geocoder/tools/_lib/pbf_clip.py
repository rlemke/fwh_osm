"""Clip a cached Geofabrik PBF to a bbox or polygon via ``osmium extract``.

Clips live in their own sibling cache_type ``pbf-clips`` (not nested
under ``pbf/``) so every cache_type root stays pure — see
``agent-spec/cache-layout.agent-spec.yaml``. The clip's region key is
``<name>`` (flat, not hierarchical), but the relative_path on disk is
``<name>-latest.osm.pbf`` so the artifact reads like any other PBF.

Downstream tools (pbf_geojson, pbf_extract, graphhopper_build, …) read
clipped PBFs via ``clipped_path(name)`` or by constructing the sidecar
path directly. They do not inherit a clip-as-region convention from
``pbf/``; clips are a distinct namespace-local cache_type.

Cache validity requires:

- Source PBF's SHA-256 still matches what the clip's sidecar recorded, AND
- The clip spec (bbox or polygon content) still matches.

Bumping either triggers a re-clip.
"""

from __future__ import annotations

import hashlib
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
CACHE_TYPE = "pbf-clips"
SOURCE_CACHE_TYPE = "pbf"
CLIP_VERSION = 1
CHUNK_SIZE = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 1800

_clip_locks: dict[str, threading.Lock] = {}
_clip_locks_guard = threading.Lock()


def _clip_lock(name: str) -> threading.Lock:
    with _clip_locks_guard:
        lock = _clip_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _clip_locks[name] = lock
        return lock


@dataclass
class ClipSpec:
    """How a clip is defined."""

    kind: str                   # "bbox" or "polygon"
    bbox: tuple[float, float, float, float] | None = None
    polygon_path: str | None = None
    polygon_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.bbox is not None:
            d["bbox"] = list(self.bbox)
        if self.polygon_path is not None:
            d["polygon_path"] = self.polygon_path
            d["polygon_sha256"] = self.polygon_sha256
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClipSpec":
        return cls(
            kind=d.get("kind", ""),
            bbox=tuple(d["bbox"]) if d.get("bbox") else None,  # type: ignore[arg-type]
            polygon_path=d.get("polygon_path"),
            polygon_sha256=d.get("polygon_sha256"),
        )

    def matches(self, other: dict[str, Any]) -> bool:
        """True if ``other`` (a clip sub-dict) describes the same clip."""
        if other.get("kind") != self.kind:
            return False
        if self.kind == "bbox":
            got = other.get("bbox")
            return list(self.bbox) == got if self.bbox else False
        if self.kind == "polygon":
            return other.get("polygon_sha256") == self.polygon_sha256
        return False


@dataclass
class ClipResult:
    """Outcome of a ``clip_pbf`` call."""

    name: str
    path: str
    relative_path: str
    source_region: str
    source_pbf_sha256: str
    clip: ClipSpec
    size_bytes: int
    sha256: str
    generated_at: str
    duration_seconds: float
    was_cached: bool
    sidecar: dict[str, Any] = field(default_factory=dict)


class ClipError(RuntimeError):
    """Raised when clipping fails (missing source, invalid spec, osmium error)."""


def clip_rel_path(name: str) -> str:
    """Relative path within ``pbf-clips/`` for a clip named ``name``."""
    return f"{name}-latest.osm.pbf"


def clipped_path(name: str, storage: Any = None) -> str:
    """Absolute cache path for a clip."""
    s = storage or LocalStorage()
    return sidecar.cache_path(NAMESPACE, CACHE_TYPE, clip_rel_path(name), s)


def clip_abs_path(name: str, storage: Any = None) -> Path:
    """Absolute cache path for a clip as a ``Path`` (back-compat alias)."""
    return Path(clipped_path(name, storage))


def clip_region_key(name: str) -> str:
    """Back-compat alias for tools that expect a region-key string."""
    return name


def _source_pbf_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, f"{region}-latest.osm.pbf", s))


def _staging_path(name: str, storage: Any = None) -> Path:
    """Stage adjacent to the final destination unless forced to tmp."""
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = name.replace("/", "_")
        return Path(base) / "facetwork-pbf-clip-staging" / safe / f"{name}-latest.osm.pbf"
    out = Path(clipped_path(name, storage))
    return out.with_name(out.name + ".staging")


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


def _sha256_file_only(path: Path) -> str:
    _, h = _sha256_file(path)
    return h


def _osmium_version(osmium_bin: str) -> str:
    try:
        result = subprocess.run(
            [osmium_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or "").splitlines()
        return first_line[0].strip() if first_line else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ClipError("bbox must be (west, south, east, north)")
    w, s, e, n = bbox
    if not (-180.0 <= w < e <= 180.0):
        raise ClipError(f"bbox longitudes out of range: west={w} east={e}")
    if not (-90.0 <= s < n <= 90.0):
        raise ClipError(f"bbox latitudes out of range: south={s} north={n}")


def build_spec(
    *,
    bbox: tuple[float, float, float, float] | None = None,
    polygon_path: str | None = None,
) -> ClipSpec:
    """Validate and normalize user-supplied clip parameters into a ClipSpec."""
    if bbox is not None and polygon_path is not None:
        raise ClipError("pass either bbox or polygon_path, not both")
    if bbox is not None:
        _validate_bbox(bbox)
        return ClipSpec(kind="bbox", bbox=bbox)
    if polygon_path is not None:
        p = Path(polygon_path)
        if not p.is_file():
            raise ClipError(f"polygon file not found: {polygon_path}")
        return ClipSpec(
            kind="polygon",
            polygon_path=str(p.resolve()),
            polygon_sha256=_sha256_file_only(p),
        )
    raise ClipError("must supply one of bbox or polygon_path")


def is_up_to_date(
    name: str,
    source_region: str,
    source_pbf_sha256: str,
    spec: ClipSpec,
    storage: Any = None,
) -> bool:
    """True if the cached clip still matches source SHA and clip spec."""
    s = storage or LocalStorage()
    rel = clip_rel_path(name)
    existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, rel, s)
    if not existing:
        return False
    extra = existing.get("extra") or {}
    clip_info = extra.get("clip") or {}
    if clip_info.get("source_region") != source_region:
        return False
    if clip_info.get("source_sha256") != source_pbf_sha256:
        return False
    if not spec.matches(clip_info):
        return False
    out = Path(clipped_path(name, s))
    if not out.exists():
        return False
    return out.stat().st_size == existing.get("size_bytes")


def clip_pbf(
    name: str,
    source_region: str,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    polygon_path: str | None = None,
    force: bool = False,
    osmium_bin: str = "osmium",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    storage: Any = None,
) -> ClipResult:
    """Clip a cached source PBF to a bbox or polygon.

    Output lands under ``cache/osm/pbf-clips/<name>-latest.osm.pbf``.
    """
    if not name or "/" in name:
        raise ClipError(
            f"clip name must not be empty or contain '/'. got: {name!r}"
        )
    s = storage or LocalStorage()
    spec = build_spec(bbox=bbox, polygon_path=polygon_path)

    source_rel = f"{source_region}-latest.osm.pbf"
    source_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, source_rel, s)
    if not source_side:
        raise ClipError(
            f"no pbf sidecar for source region {source_region!r}. "
            "Run download-pbf first."
        )
    source_pbf = _source_pbf_path(source_region, s)
    if not source_pbf.exists():
        raise ClipError(f"source pbf missing on disk: {source_pbf}")
    source_sha = source_side.get("sha256", "")

    with _clip_lock(name):
        out_abs = Path(clipped_path(name, s))
        out_rel = clip_rel_path(name)

        if not force and is_up_to_date(name, source_region, source_sha, spec, s):
            existing = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, out_rel, s) or {}
            return ClipResult(
                name=name,
                path=str(out_abs),
                relative_path=out_rel,
                source_region=source_region,
                source_pbf_sha256=source_sha,
                clip=spec,
                size_bytes=existing.get("size_bytes", out_abs.stat().st_size),
                sha256=existing.get("sha256", ""),
                generated_at=existing.get("generated_at", ""),
                duration_seconds=0.0,
                was_cached=True,
                sidecar=existing,
            )

        staged_pbf = _staging_path(name, s)
        if staged_pbf.parent.exists() and staged_pbf.parent != out_abs.parent:
            # Only clean a dedicated staging subdir; never wipe the shared
            # cache dir.
            shutil.rmtree(staged_pbf.parent, ignore_errors=True)
        staged_pbf.parent.mkdir(parents=True, exist_ok=True)
        if staged_pbf.exists():
            staged_pbf.unlink()

        cmd = [osmium_bin, "extract", "--overwrite", "-o", str(staged_pbf)]
        if spec.kind == "bbox":
            assert spec.bbox is not None
            cmd += ["--bbox", ",".join(f"{x}" for x in spec.bbox)]
        elif spec.kind == "polygon":
            assert spec.polygon_path is not None
            cmd += ["--polygon", spec.polygon_path]
        cmd += [str(source_pbf)]

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
            if staged_pbf.exists():
                staged_pbf.unlink()
            stderr = (exc.stderr or "").strip()
            raise ClipError(f"osmium extract failed: {stderr or exc}") from exc
        except subprocess.TimeoutExpired as exc:
            if staged_pbf.exists():
                staged_pbf.unlink()
            raise ClipError(
                f"osmium extract timed out after {timeout_seconds}s"
            ) from exc
        except BaseException:
            if staged_pbf.exists():
                staged_pbf.unlink()
            raise
        elapsed = time.monotonic() - start

        size, sha256_hex = _sha256_file(staged_pbf)

        s.finalize_from_local(str(staged_pbf), str(out_abs))

        generated_at = sidecar.utcnow_iso()
        side = sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            out_rel,
            kind="file",
            size_bytes=size,
            sha256=sha256_hex,
            source={
                "namespace": NAMESPACE,
                "cache_type": SOURCE_CACHE_TYPE,
                "relative_path": source_rel,
                "sha256": source_sha,
                "size_bytes": source_side.get("size_bytes"),
            },
            tool={
                "command": "osmium extract",
                "osmium_version": _osmium_version(osmium_bin),
            },
            extra={
                "clip": {
                    **spec.to_dict(),
                    "source_region": source_region,
                    "source_sha256": source_sha,
                    "version": CLIP_VERSION,
                },
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return ClipResult(
            name=name,
            path=str(out_abs),
            relative_path=out_rel,
            source_region=source_region,
            source_pbf_sha256=source_sha,
            clip=spec,
            size_bytes=size,
            sha256=sha256_hex,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            sidecar=side,
        )


def list_clips(storage: Any = None) -> list[dict[str, Any]]:
    """Return every clip currently cached as a sidecar dict."""
    s = storage or LocalStorage()
    return sidecar.list_entries(NAMESPACE, CACHE_TYPE, s)
