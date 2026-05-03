"""Extract category-specific feature layers from cached OSM PBFs.

Each category produces a pre-filtered GeoJSONSeq file in its own cache
subdirectory (``<cache_root>/<category>/<region>-latest.geojsonseq``)
with its own ``manifest.json``. Downstream consumers load the small
already-filtered file instead of re-parsing the full PBF.

Categories are defined in ``_lib/pbf_extract.py::CATEGORIES``. Adding a
new one is a single dict entry (name, tag filter, description,
filter_version).

Usage::

    python extract.py water europe/germany/berlin
    python extract.py protected_areas --all
    python extract.py parks --all-under europe/germany
    python extract.py --extract-all-categories --all-under europe
    python extract.py --list-categories

Requires the ``osmium`` command-line tool (``osmium-tool``) on ``PATH``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import sidecar  # noqa: E402
from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)
from _lib.pbf_extract import (  # noqa: E402
    CATEGORIES,
    NAMESPACE,
    SOURCE_CACHE_TYPE,
    ExtractionError,
    ExtractResult,
    extract_abs_path,
    extract_region,
    is_up_to_date,
    pbf_abs_path,
)

DEFAULT_JOBS = 2


def _help_epilog() -> str:
    lines = ["available categories:"]
    name_width = max(len(c) for c in CATEGORIES) if CATEGORIES else 10
    for name in sorted(CATEGORIES):
        cat = CATEGORIES[name]
        lines.append(f"  {name:<{name_width}}  {cat.description}")
    lines.append("")
    lines.append("filter expressions (osmium tags-filter syntax):")
    for name in sorted(CATEGORIES):
        cat = CATEGORIES[name]
        lines.append(f"  {name:<{name_width}}  {cat.filter_expression}")
    return "\n".join(lines)


def _read_regions_file(path: Path) -> list[str]:
    regions: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regions.append(line)
    return regions


def _up_to_date_cheap(region: str, category: str) -> bool:
    pbf_rel = f"{region}-latest.osm.pbf"
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel)
    if not pbf_side:
        return False
    return is_up_to_date(region, category, pbf_side, extract_abs_path(region, category))


def _run_one(
    region: str,
    category: str,
    *,
    force: bool,
    dry_run: bool,
    osmium_bin: str,
) -> str:
    if dry_run:
        src = pbf_abs_path(region)
        dst = extract_abs_path(region, category)
        print(f"[{category}/{region}] would extract {src} -> {dst}", file=sys.stderr)
        return "dry-run"
    try:
        result: ExtractResult = extract_region(
            region, category, force=force, osmium_bin=osmium_bin
        )
    except ExtractionError as exc:
        print(f"[{category}/{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{category}/{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{category}/{region}] done "
        f"({result.size_bytes / (1024 * 1024):.1f} MiB, "
        f"{result.feature_count} features, {result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "extracted"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract category-specific feature layers from cached OSM PBFs.",
        epilog=_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "category",
        nargs="?",
        help="Category to extract (use --list-categories to see choices). "
        "Omit when --extract-all-categories is given.",
    )
    parser.add_argument(
        "regions",
        nargs="*",
        help="Region keys, e.g. europe/germany/berlin. Must be in the pbf manifest.",
    )
    parser.add_argument(
        "--regions-file",
        type=Path,
        help="Read regions from a file (one per line; '#' comments allowed).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Extract for every region currently in the pbf manifest.",
    )
    parser.add_argument(
        "--all-under",
        metavar="PREFIX",
        help="Extract for every cached PBF whose region path starts with PREFIX.",
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
        help="Extract for every region in the pbf manifest whose category "
        "output is missing or stale (different pbf SHA or filter_version).",
    )
    parser.add_argument(
        "--extract-all-categories",
        action="store_true",
        help="For each resolved region, extract every category in the "
        "registry. Equivalent to running the tool once per category.",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Print available categories and their filter expressions, then exit.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved (category, region) pairs to stdout and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even when the existing file matches source PBF SHA and filter version.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be extracted without running osmium.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Number of concurrent extractions (default: {DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "--osmium",
        default="osmium",
        help="Path to the osmium binary (default: 'osmium' on PATH).",
    )
    args = parser.parse_args()

    if args.list_categories:
        print(_help_epilog())
        return 0

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

    # Resolve categories.
    if args.extract_all_categories:
        # With --extract-all-categories, the first positional (which argparse
        # would have read as ``category``) is actually a region. Shift it
        # back into ``regions`` so `extract.sh --extract-all-categories
        # europe/liechtenstein` works as the user expects.
        if args.category is not None:
            args.regions = [args.category, *args.regions]
            args.category = None
        categories = sorted(CATEGORIES.keys())
    else:
        if not args.category:
            parser.error(
                "category is required (or pass --extract-all-categories / --list-categories)"
            )
        if args.category not in CATEGORIES:
            parser.error(
                f"unknown category: {args.category!r}. "
                f"Valid: {', '.join(sorted(CATEGORIES))}"
            )
        categories = [args.category]

    # Resolve regions.
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

    seen_regions: set[str] = set()
    deduped_regions: list[str] = []
    for r in regions:
        if r and r not in seen_regions:
            seen_regions.add(r)
            deduped_regions.append(r)
    regions = deduped_regions

    # Build (category, region) work list.
    if args.update_all:
        # Only use pbf manifest as the region universe, regardless of args.regions.
        universe = regions_from_pbf_cache()
        if not args.include_parents:
            universe = filter_leaves(universe)
        pairs: list[tuple[str, str]] = []
        for cat in categories:
            stale = [r for r in universe if not _up_to_date_cheap(r, cat)]
            current = len(universe) - len(stale)
            print(
                f"update-all[{cat}]: {len(universe)} cached pbf(s), "
                f"{len(stale)} need extraction ({current} already current)",
                file=sys.stderr,
            )
            for r in stale:
                pairs.append((cat, r))
    else:
        pairs = [(cat, r) for cat in categories for r in regions]

    # Dedupe (category, region) pairs while preserving order.
    seen_pairs: set[tuple[str, str]] = set()
    deduped_pairs: list[tuple[str, str]] = []
    for p in pairs:
        if p not in seen_pairs:
            seen_pairs.add(p)
            deduped_pairs.append(p)
    pairs = deduped_pairs

    if not pairs:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: nothing to do, all category caches are current", file=sys.stderr)
            return 0
        parser.error(
            "no work to do (no regions provided and/or no stale entries found)"
        )

    if args.list:
        for cat, r in pairs:
            print(f"{cat}/{r}")
        print(f"{len(pairs)} (category, region) pair(s)", file=sys.stderr)
        return 0

    results = {"extracted": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str, str]] = []

    def _run(pair: tuple[str, str]) -> tuple[str, str | None]:
        cat, region = pair
        try:
            outcome = _run_one(
                region,
                cat,
                force=args.force,
                dry_run=args.dry_run,
                osmium_bin=osmium_bin,
            )
            return outcome, None
        except Exception as exc:  # noqa: BLE001
            return "failed", str(exc)

    if args.jobs == 1 or len(pairs) == 1:
        for pair in pairs:
            outcome, err = _run(pair)
            if err:
                failures.append((pair[0], pair[1], err))
            results[outcome] += 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_run, p): p for p in pairs}
            for fut in concurrent.futures.as_completed(futures):
                pair = futures[fut]
                outcome, err = fut.result()
                if err:
                    failures.append((pair[0], pair[1], err))
                results[outcome] += 1

    print(
        f"\nSummary: {results['extracted']} extracted, "
        f"{results['skipped']} skipped, "
        f"{results['dry-run']} dry-run, "
        f"{results['failed']} failed",
        file=sys.stderr,
    )
    if failures:
        for cat, region, msg in failures:
            print(f"  - {cat}/{region}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
