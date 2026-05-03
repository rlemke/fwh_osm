"""Shared PBF → ESRI Shapefile conversion library.

Single source of truth for converting cached PBFs to multi-layer
shapefile bundles via ``ogr2ogr``. Used by the CLI tool and the FFL
``osm.ops.ConvertPbfToShapefile`` handler.

Output for each region is a **directory** of shapefile bundles (one
``.shp``/``.shx``/``.dbf``/``.prj``/``.cpg`` set per layer). The sidecar
lives next to the directory, not inside it.

The ``other_relations`` layer (GeometryCollection) is never produced —
shapefile cannot represent it. Use ``pbf_geojson`` if you need those.
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
SOURCE_CACHE_TYPE = "pbf"
OUTPUT_CACHE_TYPE = "shapefiles"
CHUNK_SIZE = 1024 * 1024

OSM_LAYER_NAMES: tuple[str, ...] = (
    "points",
    "lines",
    "multilinestrings",
    "multipolygons",
)

_region_locks: dict[str, threading.Lock] = {}
_region_locks_guard = threading.Lock()


def _region_lock(region: str) -> threading.Lock:
    with _region_locks_guard:
        lock = _region_locks.get(region)
        if lock is None:
            lock = threading.Lock()
            _region_locks[region] = lock
        return lock


@dataclass
class ConvertResult:
    region: str
    path: str
    relative_path: str
    requested_layers: tuple[str, ...]
    layers: list[dict[str, Any]]
    total_size_bytes: int
    shp_size_bytes: int
    generated_at: str
    duration_seconds: float
    was_cached: bool
    source_url: str
    source_pbf_path: str
    sidecar: dict[str, Any] = field(default_factory=dict)


class ConversionError(RuntimeError):
    """Raised when a conversion fails."""


def pbf_rel_path(region: str) -> str:
    return f"{region}-latest.osm.pbf"


def pbf_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel_path(region), s))


def shapefile_rel_path(region: str) -> str:
    """Relative path (the directory name) within the shapefiles cache."""
    return f"{region}-latest"


def shapefile_abs_path(region: str, storage: Any = None) -> Path:
    s = storage or LocalStorage()
    return Path(sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, shapefile_rel_path(region), s))


def _staging_dir(region: str, storage: Any = None) -> Path:
    """Stage adjacent to the final destination unless AFL_CONVERT_STAGING=tmp."""
    if (os.environ.get("AFL_CONVERT_STAGING") or "").lower() == "tmp":
        base = tempfile.gettempdir()
        safe = region.replace("/", "_")
        return Path(base) / "facetwork-shapefile-staging" / safe
    out = shapefile_abs_path(region, storage)
    return out.with_name(out.name + ".tmp")


def _ogr2ogr_version(ogr2ogr_bin: str) -> str:
    try:
        result = subprocess.run(
            [ogr2ogr_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first_line = (result.stdout or "").splitlines()
        return first_line[0].strip() if first_line else "unknown"
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


def _layer_metadata(out_dir: Path) -> list[dict]:
    layers: list[dict] = []
    if not out_dir.exists():
        return layers
    for shp in sorted(out_dir.glob("*.shp")):
        size, sha256_hex = _sha256_file(shp)
        layers.append({"name": shp.stem, "size_bytes": size, "sha256": sha256_hex})
    return layers


def normalize_layers(layers: tuple[str, ...] | list[str] | str | None) -> tuple[str, ...]:
    if layers is None:
        return OSM_LAYER_NAMES
    if isinstance(layers, str):
        raw = [n.strip() for n in layers.split(",") if n.strip()]
    else:
        raw = list(layers)
    if not raw:
        return OSM_LAYER_NAMES
    invalid = [n for n in raw if n not in OSM_LAYER_NAMES]
    if invalid:
        raise ConversionError(
            f"unknown layer(s): {', '.join(invalid)}. "
            f"Valid: {', '.join(OSM_LAYER_NAMES)}"
        )
    seen = set(raw)
    return tuple(n for n in OSM_LAYER_NAMES if n in seen)


def is_up_to_date(
    region: str,
    pbf_side: dict,
    out_abs: Path,
    requested_layers: tuple[str, ...],
    storage: Any = None,
) -> bool:
    """True if the cached bundle still satisfies the request."""
    s = storage or LocalStorage()
    out_rel = shapefile_rel_path(region)
    existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    if not out_abs.exists():
        return False
    extra = existing.get("extra") or {}
    existing_layers = set(extra.get("requested_layers", []))
    if not existing_layers.issuperset(requested_layers):
        return False
    size_by_name = {layer["name"]: layer["size_bytes"] for layer in extra.get("layers", [])}
    for name in requested_layers:
        layer_file = out_abs / f"{name}.shp"
        if not layer_file.exists():
            return False
        if name in size_by_name and layer_file.stat().st_size != size_by_name[name]:
            return False
    return True


def convert_region(
    region: str,
    *,
    layers: tuple[str, ...] | list[str] | str | None = None,
    force: bool = False,
    ogr2ogr_bin: str = "ogr2ogr",
    storage: Any = None,
) -> ConvertResult:
    """Convert a region's PBF to a multi-layer shapefile bundle."""
    layers_tuple = normalize_layers(layers)
    s = storage or LocalStorage()

    pbf_rel = pbf_rel_path(region)
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if not pbf_side:
        raise ConversionError(
            f"no pbf sidecar for {region!r}; run download-pbf first"
        )
    src_pbf = pbf_abs_path(region, s)
    if not src_pbf.exists():
        raise ConversionError(f"pbf file missing on disk: {src_pbf}")
    source_url = pbf_side.get("source", {}).get("url", "")

    with _region_lock(region):
        out_abs = shapefile_abs_path(region, s)
        out_rel = shapefile_rel_path(region)

        if not force and is_up_to_date(region, pbf_side, out_abs, layers_tuple, s):
            existing = sidecar.read_sidecar(NAMESPACE, OUTPUT_CACHE_TYPE, out_rel, s) or {}
            extra = existing.get("extra") or {}
            return ConvertResult(
                region=region,
                path=str(out_abs),
                relative_path=out_rel + "/",
                requested_layers=layers_tuple,
                layers=extra.get("layers", []),
                total_size_bytes=existing.get("size_bytes", 0),
                shp_size_bytes=extra.get("shp_size_bytes", 0),
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

        cmd = [
            ogr2ogr_bin,
            "-f",
            "ESRI Shapefile",
            "-lco",
            "ENCODING=UTF-8",
            "-skipfailures",
            str(staging),
            str(src_pbf),
            *layers_tuple,
        ]
        start = time.monotonic()
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            stderr = (exc.stderr or "").strip()
            raise ConversionError(f"ogr2ogr failed: {stderr or exc}") from exc
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        elapsed = time.monotonic() - start

        s.finalize_dir_from_local(str(staging), str(out_abs))

        layer_metadata = _layer_metadata(out_abs)
        total_size_shp = sum(layer["size_bytes"] for layer in layer_metadata)
        total_size_all = sum(f.stat().st_size for f in out_abs.rglob("*") if f.is_file())
        generated_at = sidecar.utcnow_iso()

        # Directory sidecar: sha256 is that of the primary .shp bundle (the
        # first-named requested layer's .shp), per cache-layout spec
        # "SHA-256 of the primary payload file". Fall back to empty if none.
        primary_sha = ""
        for layer in layer_metadata:
            if layer["name"] == layers_tuple[0]:
                primary_sha = layer.get("sha256", "")
                break

        side = sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            out_rel,
            kind="directory",
            size_bytes=total_size_all,
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
                "command": "ogr2ogr -f 'ESRI Shapefile'",
                "ogr2ogr_version": _ogr2ogr_version(ogr2ogr_bin),
            },
            extra={
                "region": region,
                "format": "shapefile",
                "requested_layers": list(layers_tuple),
                "shp_size_bytes": total_size_shp,
                "layers": layer_metadata,
                "duration_seconds": round(elapsed, 2),
            },
            generated_at=generated_at,
            storage=s,
        )

        return ConvertResult(
            region=region,
            path=str(out_abs),
            relative_path=out_rel + "/",
            requested_layers=layers_tuple,
            layers=layer_metadata,
            total_size_bytes=total_size_all,
            shp_size_bytes=total_size_shp,
            generated_at=generated_at,
            duration_seconds=elapsed,
            was_cached=False,
            source_url=source_url,
            source_pbf_path=str(src_pbf),
            sidecar=side,
        )


def to_osm_cache(result: ConvertResult) -> dict[str, Any]:
    """Map a ``ConvertResult`` to the ``OSMCache`` dict. ``path`` is the
    bundle directory, not a single file.
    """
    return {
        "url": result.source_url,
        "path": result.path,
        "date": result.generated_at,
        "size": result.total_size_bytes,
        "wasInCache": result.was_cached,
        "source": "cache" if result.was_cached else "convert",
    }
