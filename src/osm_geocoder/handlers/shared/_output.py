"""Shared output helpers for OSM extractor handlers.

Provides HDFS-aware output directory resolution and file writing.
When AFL_OSM_OUTPUT_BASE is set (e.g. hdfs://afl-hadoop-hdfs:8020/osm-output),
extractors write output there. Otherwise they derive from AFL_OUTPUT_BASE/osm.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import IO

from facetwork.config import get_output_base
from facetwork.runtime.storage import get_storage_backend

_OUTPUT_BASE = os.environ.get("AFL_OSM_OUTPUT_BASE", "")
_REMOTE_SCHEMES = ("hdfs://", "s3://")


def _osm_output_base() -> str:
    """Resolve the base directory for durable osm output.

    Precedence:
      1. AFL_OSM_OUTPUT_BASE if set (explicit override — e.g. a dedicated prefix).
      2. AFL_DATA_ROOT/osm-output when the durable store is remote (s3://, hdfs://).
      3. "" → caller falls back to a host-local path.

    Rule (2) is the important one: EVERY RegistryRunner auto-loads the osm
    handlers, so any runner — not just the dedicated osm ones that set
    AFL_OSM_OUTPUT_BASE — can win an osm task claim. Without (2), a runner that
    only set AFL_STORAGE=s3 (+ AFL_DATA_ROOT) but not AFL_OSM_OUTPUT_BASE would
    write osm output to a HOST-LOCAL path, unreadable by the S3-backed downstream
    steps (ByScript, MergeLayers) that run on a different runner — dead-lettering
    the workflow. Defaulting to the object store keeps output where any runner
    can read it, so the fix ships in the handler image with no per-host env.
    """
    if _OUTPUT_BASE:
        return _OUTPUT_BASE.rstrip("/")
    data_root = os.environ.get("AFL_DATA_ROOT", "")
    if data_root.startswith(_REMOTE_SCHEMES):
        return f"{data_root.rstrip('/')}/osm-output"
    return ""


def resolve_output_dir(category: str, default_local: str = "") -> str:
    """Return output directory for a handler category.

    Uses the durable osm output base (AFL_OSM_OUTPUT_BASE, or AFL_DATA_ROOT/
    osm-output when storage is remote) → '{base}/{category}'. Falls back to a
    host-local AFL_OUTPUT_BASE/osm/{category} only when no remote store is
    configured. See :func:`_osm_output_base`.
    """
    base = _osm_output_base()
    if base:
        return f"{base}/{category}"
    base = default_local or os.path.join(get_output_base(), "osm")
    return f"{base}/{category}"


def resolve_local_output_dir(*parts: str) -> str:
    """Return local output directory under AFL_LOCAL_OUTPUT_DIR.

    Joins *parts* as subdirectories.  Creates the directory if it doesn't exist.

    Example::

        resolve_local_output_dir("maps", "alabama")
        # -> "/Volumes/afl_data/output/maps/alabama"
    """
    base = get_output_base()
    path = os.path.join(base, *parts)
    Path(path).mkdir(parents=True, exist_ok=True)
    return path


def open_output(path: str, mode: str = "w") -> IO:
    """Open a file for writing using the correct storage backend."""
    backend = get_storage_backend(path)
    return backend.open(path, mode)


def open_input(path: str, mode: str = "r") -> IO:
    """Open a file for reading using the correct storage backend.

    The read-side counterpart to :func:`open_output`. Local paths and remote
    URIs (``hdfs://`` / ``s3://``) are dispatched to the matching backend, so a
    handler reads a durable artifact identically wherever it lives.
    """
    backend = get_storage_backend(path)
    return backend.open(path, mode)


def read_storage_text(path: str, encoding: str = "utf-8") -> str:
    """Read a (possibly remote) text artifact fully into a string.

    Reads as bytes then decodes so behaviour is identical across the local /
    HDFS / S3 backends. Use for durable, modest-size artifacts (graph JSON,
    summaries, sidecars) — NOT for multi-GB GeoJSON, which should be streamed
    locally via :func:`iter_geojson_features`.
    """
    with open_input(path, "rb") as f:
        return f.read().decode(encoding)


def read_storage_json(path: str):
    """Read and parse a (possibly remote) JSON artifact into Python objects."""
    import json

    return json.loads(read_storage_text(path))


def _slugify_discriminators(discriminators: tuple) -> str:
    """Build a deterministic, collision-safe slug from a tuple of discriminators.

    The discriminators are the parameters that make one derived artifact distinct
    from a sibling query on the same input (e.g. ``("amenity", "fast_food")``).
    ``None``/``""``/``"*"`` (the "any" wildcard) are dropped — they are not
    discriminating. The common case yields a short, human-readable slug
    (``amenity-fast_food``); when the readable form would be long it falls back to
    the first token plus a short stable hash of the full tuple, so the name stays
    bounded and still deterministic.
    """
    # Keep raw values (no strip) so e.g. "I " and "I" stay distinct after
    # sanitization ("I_" vs "I"); only the "any"/empty wildcards are dropped.
    parts = [str(d) for d in discriminators if d not in (None, "", "*")]
    if not parts:
        return ""
    import re

    readable = "-".join(re.sub(r"[^A-Za-z0-9._]+", "_", p) for p in parts)
    if len(readable) <= 48:
        return readable
    import hashlib

    digest = hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:10]
    head = re.sub(r"[^A-Za-z0-9._]+", "_", parts[0])[:16]
    return f"{head}_{digest}"


def _per_run_enabled() -> bool:
    return os.environ.get("AFL_OUTPUT_PER_RUN", "").strip().lower() in ("1", "true", "yes")


def derive_output_path(
    category: str,
    stem: str,
    label: str,
    *discriminators,
    ext: str = "geojson",
    run_id: str | None = None,
) -> str:
    """Build a collision-safe, deterministic output path for a *derived* artifact.

    Names a per-query leaf artifact (a filtered GeoJSON, a rendered map) so two
    different queries over the same input do not overwrite each other. The name
    encodes the *discriminating parameters* rather than an execution id, so it is
    idempotent (re-running the same query overwrites with identical content),
    reproducible, human-meaningful, and bounded by the number of distinct queries:

        <category-dir>/<stem>_<label>[_<slug>].<ext>

    e.g. ``…/osm-filtered/california-latest.osm_amenities_filtered_amenity-fast_food.geojson``.

    Do **not** use this for cacheable intermediates (category extracts, scan
    manifests) — those are input-addressed and shared across runs by design.

    Per-run isolation (opt-in): when ``AFL_OUTPUT_PER_RUN`` is set and ``run_id``
    (the execution workflow id) is provided, the artifact lands under
    ``<category-dir>/runs/<run_id>/`` instead — a retained, easily-cleaned
    per-run output tree. The shared cache is untouched either way.
    """
    out_dir = resolve_output_dir(category)
    if run_id and _per_run_enabled():
        out_dir = f"{out_dir}/runs/{run_id}"
    slug = _slugify_discriminators(discriminators)
    suffix = f"_{slug}" if slug else ""
    path = f"{out_dir}/{stem}_{label}{suffix}.{ext}"
    ensure_dir(path)
    return path


def uri_stem(path: str) -> str:
    """Get the stem (filename without extension) from a path or HDFS URI.

    Works with both local paths and ``hdfs://`` URIs because both
    use ``/`` as the separator.
    """
    import posixpath

    return posixpath.splitext(posixpath.basename(path))[0]


def ensure_dir(path: str) -> None:
    """Create the parent directory for an output path (storage-aware).

    Local paths → ``Path.mkdir``. Remote paths (``hdfs://``, ``s3://``) → the
    backend's ``makedirs`` (a no-op for object stores, which have no dirs).
    """
    if path.startswith(_REMOTE_SCHEMES):
        backend = get_storage_backend(path)
        backend.makedirs(path.rsplit("/", 1)[0])
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def finalize_output_file(local_tmp: str, dst: str) -> None:
    """Publish a locally-written temp file to its final ``dst`` (storage-aware).

    Local ``dst`` → atomic ``shutil.move``. Remote ``dst`` (``hdfs://``,
    ``s3://``) → stream the bytes through the storage backend, then remove the
    local temp. This is the *output* counterpart to the cache's
    stage-locally-then-finalize pattern, so handlers can write to a remote
    shared store transparently.
    """
    if not dst.startswith(_REMOTE_SCHEMES):
        ensure_dir(dst)
        shutil.move(local_tmp, dst)
        return
    backend = get_storage_backend(dst)
    backend.makedirs(dst.rsplit("/", 1)[0])
    with open(local_tmp, "rb") as src, backend.open(dst, "wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    os.remove(local_tmp)
