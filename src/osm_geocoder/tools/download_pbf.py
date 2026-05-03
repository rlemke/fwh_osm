"""Download OSM PBF files from Geofabrik into the shared OSM cache.

Thin CLI wrapper around ``_lib.pbf_download.download_region``; the same
library is used by the FFL ``osm.ops.CacheRegion`` handler, so the FFL
and the tool share one cache layout and one manifest.

Geofabrik rate-limits concurrent downloads from a single IP, so this
tool processes regions sequentially with a configurable delay between
files. Each file is verified against Geofabrik's published ``.md5``
before being promoted into the cache.

Backends (``--backend`` / ``AFL_STORAGE``):

- ``local`` (default): standard POSIX filesystem cache, atomic temp+rename.
- ``hdfs``: writes into HDFS via WebHDFS. HDFS has no advisory locking, so
  sidecars assume single-writer semantics — run the tool from one
  coordinator process when using the HDFS backend.

Usage::

    python download_pbf.py europe/germany/berlin
    python download_pbf.py --force europe/germany/berlin
    python download_pbf.py europe/germany/berlin europe/germany/brandenburg
    python download_pbf.py --all-under europe/germany
    python download_pbf.py --backend hdfs europe/germany/berlin

Regions are Geofabrik paths relative to ``https://download.geofabrik.de/``,
*without* the ``-latest.osm.pbf`` suffix.

Data root: ``$AFL_DATA_ROOT`` (defaults to ``/Volumes/afl_data`` for
local, ``/user/afl`` for hdfs). PBFs cache under ``<data_root>/cache/osm/pbf/``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.pbf_download import (  # noqa: E402
    GEOFABRIK_BASE,
    DownloadError,
    DownloadResult,
    cached_path,
    download_region,
    filter_leaves,
    is_region_cached,
    regions_from_pbf_cache,
    staging_path,
)
from _lib.storage import default_backend, get_storage  # noqa: E402

GEOFABRIK_INDEX_URL = f"{GEOFABRIK_BASE}/index-v1.json"
USER_AGENT = "facetwork-osm-geocoder/1.0 (OSM PBF downloader)"
DEFAULT_DELAY_SECONDS = 1.5


def _progress(label: str, size: int, total: int, final: bool) -> None:
    """Progress callback used by the CLI — formats to stderr."""
    mib = size / (1024 * 1024)
    if total:
        pct = 100.0 * size / total
        total_mib = total / (1024 * 1024)
        msg = f"  {label}: {mib:7.1f} / {total_mib:7.1f} MiB ({pct:5.1f}%)"
    else:
        msg = f"  {label}: {mib:7.1f} MiB"
    is_tty = sys.stderr.isatty()
    if final:
        print(msg, file=sys.stderr, flush=True)
    elif is_tty:
        print(msg, end="\r", file=sys.stderr, flush=True)


def _read_regions_file(path: Path) -> list[str]:
    regions: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regions.append(line)
    return regions


def fetch_region_index() -> list[str]:
    """Fetch Geofabrik's ``index-v1.json`` and return all PBF region paths."""
    req = urllib.request.Request(
        GEOFABRIK_INDEX_URL, headers={"User-Agent": USER_AGENT}
    )
    print(f"fetching Geofabrik index from {GEOFABRIK_INDEX_URL}", file=sys.stderr)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    features = data.get("features", []) or []
    prefix = GEOFABRIK_BASE + "/"
    suffix = "-latest.osm.pbf"
    seen: set[str] = set()
    regions: list[str] = []
    for feat in features:
        urls = (feat.get("properties") or {}).get("urls") or {}
        pbf_url = urls.get("pbf")
        if not pbf_url:
            continue
        if not pbf_url.startswith(prefix) or not pbf_url.endswith(suffix):
            continue
        path = pbf_url[len(prefix):-len(suffix)]
        if path in seen:
            continue
        seen.add(path)
        regions.append(path)
    regions.sort()
    return regions


def filter_regions(
    all_regions: list[str], *, under: str | None, leaves_only: bool
) -> list[str]:
    """Filter ``all_regions`` by prefix and optionally drop parent regions."""
    under = (under or "").strip().strip("/")
    if under:
        pref = under + "/"
        selected = [r for r in all_regions if r == under or r.startswith(pref)]
    else:
        selected = list(all_regions)
    return filter_leaves(selected) if leaves_only else selected


def _run_one(region: str, *, storage, force: bool, dry_run: bool) -> str:
    """Run one region. Returns 'downloaded', 'skipped', 'dry-run', 'failed'."""
    print(f"[{region}] resolving Geofabrik metadata", file=sys.stderr)
    output_path = cached_path(region, storage=storage)
    print(f"[{region}] output:  {output_path}", file=sys.stderr)
    print(f"[{region}] staging: {staging_path(region)}", file=sys.stderr)
    if dry_run:
        # For dry-run, we still consult the manifest to show what would happen,
        # but we don't hit the network for MD5 here — just report the intent.
        print(f"[{region}] would download (dry-run)", file=sys.stderr)
        return "dry-run"
    try:
        result: DownloadResult = download_region(
            region, storage=storage, force=force, on_progress=_progress
        )
    except DownloadError as exc:
        print(f"[{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(
            f"[{region}] up-to-date (md5 {result.md5[:8]}…), skipping",
            file=sys.stderr,
        )
        return "skipped"
    print(
        f"[{region}] done ({result.size_bytes / (1024 * 1024):.1f} MiB, "
        f"sha256 {result.sha256[:12]}…)",
        file=sys.stderr,
    )
    return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download OSM PBF files from Geofabrik into the local cache.",
    )
    parser.add_argument(
        "regions",
        nargs="*",
        help="Geofabrik region keys, e.g. europe/germany/berlin",
    )
    parser.add_argument(
        "--regions-file",
        type=Path,
        help="Read regions from a file (one per line; '#' comments allowed).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download every region listed in the Geofabrik index. "
        "Combine with --include-parents to also fetch continent/country-level PBFs.",
    )
    parser.add_argument(
        "--all-under",
        metavar="PREFIX",
        help="Download every region nested under PREFIX, e.g. europe/germany.",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Refresh every region currently in the local pbf manifest, "
        "re-downloading any whose upstream MD5 no longer matches. Scope is "
        "the local cache — does not pull new regions from Geofabrik (use "
        "--all for that). Combines cleanly with --all / --all-under.",
    )
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help="When using --all / --all-under, include parent regions alongside "
        "their descendants. Default is leaves-only (parent PBFs already "
        "contain their children, so downloading both is wasteful).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved region list to stdout and exit; no downloads.",
    )
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="Print the resolved regions that are NOT yet in the local pbf "
        "cache (not in the manifest, or file is missing) and exit. Useful "
        "with --all / --all-under to see what's left to download from "
        "Geofabrik. No network calls.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the manifest reports an up-to-date cached copy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without fetching the PBF body.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds to sleep between downloads (default: {DEFAULT_DELAY_SECONDS}).",
    )
    parser.add_argument(
        "--backend",
        choices=("local", "hdfs"),
        default=default_backend(),
        help="Storage backend for the cache "
        "(default: $AFL_STORAGE or 'local'). HDFS assumes single-writer "
        "semantics (no advisory locking).",
    )
    args = parser.parse_args()

    try:
        storage = get_storage(args.backend)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"storage backend: {storage.name}", file=sys.stderr)

    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions.extend(_read_regions_file(args.regions_file))

    if args.all or args.all_under is not None:
        index = fetch_region_index()
        resolved = filter_regions(
            index,
            under=args.all_under,
            leaves_only=not args.include_parents,
        )
        print(
            f"Geofabrik index: {len(index)} total regions, "
            f"{len(resolved)} selected after filtering",
            file=sys.stderr,
        )
        regions.extend(resolved)

    if args.update_all:
        from_manifest = regions_from_pbf_cache(storage=storage)
        print(
            f"pbf manifest: {len(from_manifest)} cached region(s) to refresh",
            file=sys.stderr,
        )
        regions.extend(from_manifest)

    seen: set[str] = set()
    deduped: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.add(r)
            deduped.append(r)
    regions = deduped

    if not regions:
        # --update-all with an empty pbf manifest: nothing to refresh, success.
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: pbf manifest is empty, nothing to refresh", file=sys.stderr)
            return 0
        parser.error(
            "no regions provided (pass as args, or use --regions-file, "
            "--all, --all-under, or --update-all)"
        )

    if args.list:
        for r in regions:
            print(r)
        print(f"{len(regions)} region(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        missing = [r for r in regions if not is_region_cached(r, storage=storage)]
        for r in missing:
            print(r)
        print(
            f"{len(missing)} not yet cached of {len(regions)} resolved "
            f"({len(regions) - len(missing)} already present)",
            file=sys.stderr,
        )
        return 0

    results = {"downloaded": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    for i, region in enumerate(regions):
        if i > 0 and not args.dry_run:
            time.sleep(args.delay)
        try:
            outcome = _run_one(
                region, storage=storage, force=args.force, dry_run=args.dry_run
            )
            results[outcome] += 1
        except Exception as exc:  # noqa: BLE001
            results["failed"] += 1
            failures.append((region, str(exc)))

    print(
        f"\nSummary: {results['downloaded']} downloaded, "
        f"{results['skipped']} skipped, "
        f"{results['dry-run']} dry-run, "
        f"{results['failed']} failed",
        file=sys.stderr,
    )
    if failures:
        for region, msg in failures:
            print(f"  - {region}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
