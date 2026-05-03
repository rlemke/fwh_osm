"""Shared output helpers for OSM extractor handlers.

Provides HDFS-aware output directory resolution and file writing.
When AFL_OSM_OUTPUT_BASE is set (e.g. hdfs://afl-hadoop-hdfs:8020/osm-output),
extractors write output there. Otherwise they derive from AFL_OUTPUT_BASE/osm.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from facetwork.config import get_output_base
from facetwork.runtime.storage import get_storage_backend

_OUTPUT_BASE = os.environ.get("AFL_OSM_OUTPUT_BASE", "")


def resolve_output_dir(category: str, default_local: str = "") -> str:
    """Return output directory for a handler category.

    When AFL_OSM_OUTPUT_BASE is set (e.g. hdfs://afl-hadoop-hdfs:8020/osm-output),
    returns '{base}/{category}'. Otherwise derives from AFL_OUTPUT_BASE/osm/{category}.
    """
    if _OUTPUT_BASE:
        return f"{_OUTPUT_BASE.rstrip('/')}/{category}"
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


def uri_stem(path: str) -> str:
    """Get the stem (filename without extension) from a path or HDFS URI.

    Works with both local paths and ``hdfs://`` URIs because both
    use ``/`` as the separator.
    """
    import posixpath

    return posixpath.splitext(posixpath.basename(path))[0]


def ensure_dir(path: str) -> None:
    """Create parent directory for output paths.

    For local paths, creates parent directories via Path.mkdir().
    For HDFS paths, calls makedirs() on the HDFS backend.
    """
    if path.startswith("hdfs://"):
        parent = path.rsplit("/", 1)[0]
        backend = get_storage_backend(path)
        backend.makedirs(parent)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
