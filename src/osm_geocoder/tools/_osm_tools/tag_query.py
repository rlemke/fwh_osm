# SPDX-License-Identifier: Apache-2.0
"""Ad-hoc tag queries against the LOCAL extracts — the Overpass replacement.

``pbf_extract`` already turns a region's PBF into GeoJSON with two osmium
passes and a sidecar-validated cache. Its filter, though, is a closed enum:
25 named categories (water, parks, healthcare …), each with a hand-written
expression and a ``filter_version`` somebody has to remember to bump.

Anything outside those 25 has had to go to Overpass — and that is where the
fleet keeps meeting other people's rate limits. The tag-quality maps are
documented "cache-first, do NOT fan out (egress rate-limit)"; the ALPR map is a
single query because a fan-out would be throttled; the enclave map worked around
the same wall. The data to answer all of them is already on this disk.

So this module keeps the machinery and opens the filter:

    query_region("north-america/us/district-of-columbia",
                 "nwr/man_made=surveillance")

Same two passes, same storage, same sidecar discipline — an arbitrary osmium
``tags-filter`` expression, cached per *(region, expression)*.

**The cache key is the expression itself, not a version number.** A category
cache is invalidated by a human bumping ``filter_version``; forget, and stale
output is served silently. Here the digest of the normalised expression is part
of the cache path, so a changed question is a different key by construction and
an unchanged one hits cache. Nobody has to remember anything.

Scale, measured on this hardware: a full scan of the 853 MB central-america
extract takes ~28s, so a continent is minutes and the 87 GB planet is under an
hour — single-threaded, no rate limit, and repeatable.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _osm_tools import sidecar
from _osm_tools.pbf_extract import (
    DEFAULT_FORMAT,
    NAMESPACE,
    SOURCE_CACHE_TYPE,
    ExtractionError,
    _count_features_geojsonseq,
    _osmium_version,
    _sha256_file,
    pbf_abs_path,
    pbf_rel_path,
)
from _osm_tools.storage import get_storage

#: One cache_type for every ad-hoc query; the expression digest separates them.
QUERY_CACHE_TYPE = "query"

#: Extra trees of ready-made extracts to query, beyond the download cache
#: (colon-separated). The point of self-hosting was to stop downloading, and the
#: results of it — the 87 GB planet and the continent extracts — do NOT live in
#: `cache/osm/pbf/`: they are produced locally and published to the object store.
#: Without this the biggest sources on the machine are the ones a query cannot
#: reach, which would be a strange thing for a facet whose whole purpose is to
#: use what is already on disk.
LOCAL_EXTRACT_ROOTS = os.environ.get("FW_OSM_LOCAL_EXTRACTS", "")


def _local_roots() -> list[Path]:
    return [Path(p) for p in LOCAL_EXTRACT_ROOTS.split(":") if p.strip()]


def resolve_source(region: str, storage: Any = None) -> tuple[Path, dict]:
    """The PBF to scan for ``region``, and the sidecar-ish dict describing it.

    The download cache wins when it has the region (it carries a real sidecar
    with the source digest, which is what freshness is judged on). Otherwise the
    configured local trees are searched for `<region>-latest.osm.pbf` and, for a
    top-level name, `<name>-latest.osm.pbf` directly in the root — the layout
    `planet_bootstrap` writes.

    A local file has no sidecar, so one is synthesised from its size and mtime.
    That is weaker than a content digest and deliberately so: hashing an 87 GB
    planet to decide whether to scan it would cost as much as the scan.
    """
    s = storage or get_storage()
    pbf_rel = pbf_rel_path(region)
    side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel, s)
    if side:
        cached = pbf_abs_path(region, s)
        if cached.exists():
            return cached, side

    leaf = f"{region.rsplit('/', 1)[-1]}-latest.osm.pbf"
    for root in _local_roots():
        for candidate in (root / pbf_rel, root / leaf):
            if candidate.exists():
                st = candidate.stat()
                return candidate, {
                    # Not a content hash — see the docstring.
                    "sha256": f"mtime:{int(st.st_mtime)}:{st.st_size}",
                    "size_bytes": st.st_size,
                    "source": {"local_path": str(candidate)},
                }

    raise ExtractionError(
        f"no PBF for {region!r}: not in the download cache, and not found in "
        f"FW_OSM_LOCAL_EXTRACTS ({LOCAL_EXTRACT_ROOTS or '<unset>'}). "
        "Fetch it (download-pbf) or point FW_OSM_LOCAL_EXTRACTS at the tree that holds it."
    )

#: An osmium filter is `[nwr]/key[=value,...]`, optionally bare `nwr/key`.
_TERM = re.compile(r"^[nwr]{1,3}/[A-Za-z0-9_:.\-]+(=[^\s]+)?$")


@dataclass
class QueryResult:
    """Outcome of one ad-hoc tag query."""

    region: str
    expression: str
    digest: str
    path: str
    relative_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    duration_seconds: float
    was_cached: bool
    source_pbf_path: str
    sidecar: dict = field(default_factory=dict)


def normalise(expression: str) -> str:
    """Whitespace-normalised, term-sorted form of a filter expression.

    Sorting matters for the cache: ``nwr/amenity=cafe nwr/amenity=bar`` and the
    same two terms the other way round are the same question, and should not
    each pay for a full planet scan.
    """
    terms = [t for t in expression.split() if t]
    if not terms:
        raise ExtractionError("empty filter expression")
    return " ".join(sorted(terms))


def digest_of(expression: str) -> str:
    return hashlib.sha256(normalise(expression).encode()).hexdigest()[:12]


def validate(expression: str) -> str:
    """Return the normalised expression, or refuse with a usable message.

    Deliberately strict about one thing: a bare object-type term like ``nwr``
    or ``n/`` matches *everything*, which on a continent is a multi-hour
    rewrite of the whole extract dressed up as a query. That is a mistake, not
    a request, so it is refused rather than run.
    """
    norm = normalise(expression)
    for term in norm.split():
        if not _TERM.match(term):
            raise ExtractionError(
                f"not a valid osmium tags-filter term: {term!r}. "
                "Expected forms like 'nwr/amenity=pharmacy', 'r/boundary=protected_area' "
                "or 'nwr/man_made' (key-only)."
            )
    return norm


def query_rel_path(region: str, digest: str) -> str:
    return f"{digest}/{region}-latest.{DEFAULT_FORMAT}"


def query_abs_path(region: str, digest: str, storage: Any = None) -> Path:
    s = storage or get_storage()
    return Path(sidecar.cache_path(NAMESPACE, QUERY_CACHE_TYPE, query_rel_path(region, digest), s))


def is_up_to_date(region: str, digest: str, pbf_side: dict, out_abs: Path, storage: Any = None) -> bool:
    """True if the cached answer was computed from THIS source PBF.

    No filter-version check is needed — the expression is in the path.
    """
    s = storage or get_storage()
    existing = sidecar.read_sidecar(NAMESPACE, QUERY_CACHE_TYPE, query_rel_path(region, digest), s)
    if not existing:
        return False
    if existing.get("source", {}).get("sha256") != pbf_side.get("sha256"):
        return False
    if not out_abs.exists():
        return False
    return out_abs.stat().st_size == existing.get("size_bytes")


def query_region(
    region: str,
    expression: str,
    *,
    force: bool = False,
    osmium_bin: str = "osmium",
    storage: Any = None,
) -> QueryResult:
    """Run an arbitrary tag query against a region's cached PBF."""
    norm = validate(expression)
    digest = digest_of(norm)
    s = storage or get_storage()

    src_pbf, pbf_side = resolve_source(region, s)
    pbf_rel = pbf_rel_path(region)

    out_rel = query_rel_path(region, digest)
    out_abs = query_abs_path(region, digest, s)

    if not force and is_up_to_date(region, digest, pbf_side, out_abs, s):
        existing = sidecar.read_sidecar(NAMESPACE, QUERY_CACHE_TYPE, out_rel, s) or {}
        extra = existing.get("extra") or {}
        return QueryResult(
            region=region,
            expression=norm,
            digest=digest,
            path=str(out_abs),
            relative_path=out_rel,
            size_bytes=existing.get("size_bytes", 0),
            sha256=existing.get("sha256", ""),
            feature_count=extra.get("feature_count", 0),
            duration_seconds=0.0,
            was_cached=True,
            source_pbf_path=str(src_pbf),
            sidecar=existing,
        )

    out_abs.parent.mkdir(parents=True, exist_ok=True)
    staging = out_abs.with_name(out_abs.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    filtered_pbf = staging / "filtered.osm.pbf"
    export_out = staging / f"{region.replace('/', '_')}.{DEFAULT_FORMAT}"

    start = time.monotonic()
    try:
        subprocess.run(
            [osmium_bin, "tags-filter", "--overwrite", "-o", str(filtered_pbf),
             str(src_pbf), *norm.split()],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [osmium_bin, "export", "-f", DEFAULT_FORMAT, "-o", str(export_out),
             "--overwrite", str(filtered_pbf)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ExtractionError(f"osmium step failed: {(exc.stderr or '').strip() or exc}") from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    elapsed = time.monotonic() - start

    size, sha256_hex = _sha256_file(export_out)
    feature_count = _count_features_geojsonseq(export_out)
    s.finalize_from_local(str(export_out), str(out_abs))
    shutil.rmtree(staging, ignore_errors=True)

    generated_at = sidecar.utcnow_iso()
    side = sidecar.write_sidecar(
        NAMESPACE,
        QUERY_CACHE_TYPE,
        out_rel,
        kind="file",
        size_bytes=size,
        sha256=sha256_hex,
        source={
            "namespace": NAMESPACE,
            "cache_type": SOURCE_CACHE_TYPE,
            "relative_path": pbf_rel,
            "sha256": pbf_side.get("sha256"),
            "size_bytes": pbf_side.get("size_bytes"),
            "source_timestamp": pbf_side.get("source", {}).get("source_timestamp"),
            "downloaded_at": pbf_side.get("source", {}).get("downloaded_at"),
        },
        tool={
            "command": "osmium tags-filter | osmium export",
            "osmium_version": _osmium_version(osmium_bin),
        },
        extra={
            "region": region,
            "format": DEFAULT_FORMAT,
            "feature_count": feature_count,
            "filter": {
                "kind": "osmium-tags-filter",
                "expression": norm,
                # Recorded so the digest in the path is explainable rather than
                # an opaque directory name.
                "digest": digest,
            },
            "duration_seconds": round(elapsed, 2),
        },
        generated_at=generated_at,
        storage=s,
    )

    return QueryResult(
        region=region,
        expression=norm,
        digest=digest,
        path=str(out_abs),
        relative_path=out_rel,
        size_bytes=size,
        sha256=sha256_hex,
        feature_count=feature_count,
        duration_seconds=elapsed,
        was_cached=False,
        source_pbf_path=str(src_pbf),
        sidecar=side,
    )
