"""Convert cached OSM PBF files to GeoJSON using ``osmium export``.

Thin CLI wrapper around ``_lib.pbf_geojson.convert_region``. Both this
tool and the FFL ``osm.ops.ConvertPbfToGeoJson`` handler call that
library, so they share one cache layout and one manifest.

Reads from the ``pbf`` cache and writes to the ``geojson`` cache. The
geojson manifest records the source PBF's SHA-256 so reruns skip
regions whose source PBF has not changed; pass ``--force`` to reconvert.

Conversions can run in parallel (``--jobs N``); each worker spawns an
``osmium`` subprocess.

Usage::

    python convert_pbf_geojson.py europe/germany/berlin
    python convert_pbf_geojson.py --all --jobs 4
    python convert_pbf_geojson.py --all-under europe/germany --format geojson
    python convert_pbf_geojson.py --update-all

Requires the ``osmium`` command-line tool (``osmium-tool``) on ``PATH``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)
from _lib.pbf_geojson import (  # noqa: E402
    DEFAULT_FORMAT,
    FORMAT_EXT,
    ConversionError,
    ConvertResult,
    convert_region,
    geojson_abs_path,
    is_up_to_date,
    pbf_abs_path,
)
from _lib import sidecar  # noqa: E402
from _lib.pbf_geojson import NAMESPACE, SOURCE_CACHE_TYPE  # noqa: E402

DEFAULT_JOBS = 2


def _read_regions_file(path: Path) -> list[str]:
    regions: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regions.append(line)
    return regions


def _up_to_date_cheap(region: str, fmt: str) -> bool:
    pbf_rel = f"{region}-latest.osm.pbf"
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel)
    if not pbf_side:
        return False
    return is_up_to_date(region, fmt, pbf_side, geojson_abs_path(region, fmt))


def _run_one(region: str, *, fmt: str, force: bool, dry_run: bool, osmium_bin: str) -> str:
    if dry_run:
        src = pbf_abs_path(region)
        dst = geojson_abs_path(region, fmt)
        print(f"[{region}] would convert {src} -> {dst}", file=sys.stderr)
        return "dry-run"
    try:
        result: ConvertResult = convert_region(
            region, fmt=fmt, force=force, osmium_bin=osmium_bin
        )
    except ConversionError as exc:
        print(f"[{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{region}] done ({result.size_bytes / (1024 * 1024):.1f} MiB in "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "converted"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert cached OSM PBF files to GeoJSON using osmium export.",
    )
    parser.add_argument(
        "regions",
        nargs="*",
        help="Region keys to convert, e.g. europe/germany/berlin. "
        "Must already be present in the pbf cache.",
    )
    parser.add_argument(
        "--regions-file",
        type=Path,
        help="Read regions from a file (one per line; '#' comments allowed).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Convert every region currently in the pbf manifest.",
    )
    parser.add_argument(
        "--all-under",
        metavar="PREFIX",
        help="Convert every cached PBF whose region path starts with PREFIX.",
    )
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help="When using --all / --all-under, include parent regions "
        "alongside their descendants. Default is leaves-only.",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Convert every region in the pbf manifest whose GeoJSON is "
        "missing or out of date relative to its source PBF.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved region list to stdout and exit; no conversions.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reconvert even when the existing GeoJSON matches the source PBF's SHA-256.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be converted without running osmium.",
    )
    parser.add_argument(
        "--format",
        choices=sorted(FORMAT_EXT.keys()),
        default=DEFAULT_FORMAT,
        help=f"Output format (default: {DEFAULT_FORMAT}). "
        "'geojson' is a FeatureCollection; 'geojsonseq' is one feature per line.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Number of concurrent conversions (default: {DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "--osmium",
        default="osmium",
        help="Path to the osmium binary (default: 'osmium' on PATH).",
    )
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    osmium_bin = args.osmium
    if shutil.which(osmium_bin) is None and not Path(osmium_bin).is_file():
        print(
            f"error: osmium binary not found ({osmium_bin!r}). "
            "Install osmium-tool (e.g. 'brew install osmium-tool' or "
            "'apt install osmium-tool').",
            file=sys.stderr,
        )
        return 2

    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions.extend(_read_regions_file(args.regions_file))

    if args.all or args.all_under is not None:
        from_manifest = regions_from_pbf_cache(under=args.all_under)
        before = len(from_manifest)
        if not args.include_parents:
            from_manifest = filter_leaves(from_manifest)
        print(
            f"pbf manifest: {before} region(s) matched, "
            f"{len(from_manifest)} selected after "
            f"{'leaves-only' if not args.include_parents else 'include-parents'} filter",
            file=sys.stderr,
        )
        regions.extend(from_manifest)

    if args.update_all:
        from_manifest = regions_from_pbf_cache()
        if not args.include_parents:
            from_manifest = filter_leaves(from_manifest)
        needs_work = [r for r in from_manifest if not _up_to_date_cheap(r, args.format)]
        print(
            f"update-all: {len(from_manifest)} cached pbf(s), "
            f"{len(needs_work)} need conversion "
            f"({len(from_manifest) - len(needs_work)} already current)",
            file=sys.stderr,
        )
        regions.extend(needs_work)

    seen: set[str] = set()
    deduped: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.add(r)
            deduped.append(r)
    regions = deduped

    if not regions:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: nothing to do, all GeoJSONs are current", file=sys.stderr)
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

    results = {"converted": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    def _run(region: str) -> tuple[str, str | None]:
        try:
            outcome = _run_one(
                region,
                fmt=args.format,
                force=args.force,
                dry_run=args.dry_run,
                osmium_bin=osmium_bin,
            )
            return outcome, None
        except Exception as exc:  # noqa: BLE001
            return "failed", str(exc)

    if args.jobs == 1 or len(regions) == 1:
        for region in regions:
            outcome, err = _run(region)
            if err:
                failures.append((region, err))
            results[outcome] += 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_run, r): r for r in regions}
            for fut in concurrent.futures.as_completed(futures):
                region = futures[fut]
                outcome, err = fut.result()
                if err:
                    failures.append((region, err))
                results[outcome] += 1

    print(
        f"\nSummary: {results['converted']} converted, "
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
