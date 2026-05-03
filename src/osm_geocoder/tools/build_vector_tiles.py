"""Build vector-tile PMTiles from cached GeoJSONSeq via ``tippecanoe``.

Consumes the output of ``convert-pbf-geojson`` (whole-region GeoJSON)
or any ``extract`` category (water, parks, forests, roads_routable, …)
and produces a PMTiles file suitable for web map rendering.

Usage::

    python build_vector_tiles.py europe/liechtenstein
    python build_vector_tiles.py --source water europe/liechtenstein
    python build_vector_tiles.py --sources geojson,water,parks europe/liechtenstein
    python build_vector_tiles.py --all-sources europe/liechtenstein
    python build_vector_tiles.py --source water --all-under europe/germany
    python build_vector_tiles.py --update-all --source water

Requires ``tippecanoe`` (install via ``tools/install-tools.sh`` or
``brew install tippecanoe``).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)
from _lib.vector_tiles_build import (  # noqa: E402
    DEFAULT_MAX_ZOOM,
    DEFAULT_MIN_ZOOM,
    DEFAULT_TIMEOUT_SECONDS,
    GEOJSON_SOURCE,
    BuildError,
    BuildResult,
    build_tiles,
    is_up_to_date,
    tileset_abs_path,
    valid_sources,
)

DEFAULT_JOBS = 2


def _read_regions_file(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _up_to_date_cheap(region: str, source: str, *, min_zoom: int, max_zoom: int, layer_name: str) -> bool:
    return is_up_to_date(
        region,
        source,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        layer_name=layer_name,
    )


def _run_one(
    region: str,
    source: str,
    *,
    min_zoom: int,
    max_zoom: int,
    layer_name: str | None,
    force: bool,
    dry_run: bool,
    tippecanoe_bin: str,
    timeout_seconds: int,
) -> str:
    if dry_run:
        dst = tileset_abs_path(region, source)
        print(f"[{source}/{region}] would build -> {dst}", file=sys.stderr)
        return "dry-run"
    try:
        result: BuildResult = build_tiles(
            region,
            source,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            layer_name=layer_name,
            force=force,
            tippecanoe_bin=tippecanoe_bin,
            timeout_seconds=timeout_seconds,
        )
    except BuildError as exc:
        print(f"[{source}/{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{source}/{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{source}/{region}] done "
        f"({result.size_bytes / (1024 * 1024):.1f} MiB, "
        f"z{result.min_zoom}-{result.max_zoom}, layer={result.layer_name}, "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "built"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build vector-tile PMTiles from cached GeoJSONSeq via tippecanoe.",
    )
    parser.add_argument("regions", nargs="*")
    parser.add_argument("--regions-file", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--all-under", metavar="PREFIX")
    parser.add_argument("--include-parents", action="store_true")
    parser.add_argument("--update-all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--list-missing", action="store_true")
    parser.add_argument(
        "--source",
        default=GEOJSON_SOURCE,
        choices=valid_sources(),
        help=f"Single source to tile (default: {GEOJSON_SOURCE}). "
        f"Valid: {', '.join(valid_sources())}.",
    )
    parser.add_argument(
        "--sources",
        metavar="LIST",
        help="Comma-separated list of sources to tile per region. "
        "Overrides --source when given.",
    )
    parser.add_argument(
        "--all-sources",
        action="store_true",
        help="Tile every valid source per region. Overrides --source / --sources.",
    )
    parser.add_argument(
        "--min-zoom",
        type=int,
        default=DEFAULT_MIN_ZOOM,
        help=f"Minimum tile zoom (default: {DEFAULT_MIN_ZOOM}).",
    )
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=DEFAULT_MAX_ZOOM,
        help=f"Maximum tile zoom (default: {DEFAULT_MAX_ZOOM}).",
    )
    parser.add_argument(
        "--layer-name",
        default=None,
        help="Override the PMTiles layer name (default: source name).",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--tippecanoe", default="tippecanoe")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Per-build timeout (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    # Resolve sources.
    if args.all_sources:
        sources = valid_sources()
    elif args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
        invalid = [s for s in sources if s not in valid_sources()]
        if invalid:
            parser.error(
                f"unknown source(s): {', '.join(invalid)}. "
                f"Valid: {', '.join(valid_sources())}"
            )
    else:
        sources = [args.source]

    # Resolve regions.
    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions.extend(_read_regions_file(args.regions_file))

    if args.all or args.all_under is not None:
        t0 = time.monotonic()
        from_manifest = regions_from_pbf_cache(under=args.all_under)
        scan_elapsed = time.monotonic() - t0
        before = len(from_manifest)
        if not args.include_parents:
            from_manifest = filter_leaves(from_manifest)
        print(
            f"pbf manifest: {before} region(s) matched in {scan_elapsed:.2f}s, "
            f"{len(from_manifest)} selected after "
            f"{'leaves-only' if not args.include_parents else 'include-parents'} filter",
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

    layer_name_for = lambda s: args.layer_name or s  # noqa: E731

    if args.update_all:
        t0 = time.monotonic()
        universe = regions_from_pbf_cache()
        if not args.include_parents:
            universe = filter_leaves(universe)
        scan_elapsed = time.monotonic() - t0
        print(
            f"update-all: scanned {len(universe)} pbf entries in {scan_elapsed:.2f}s",
            file=sys.stderr,
        )
        pairs: list[tuple[str, str]] = []
        for s in sources:
            t_sweep = time.monotonic()
            stale = [
                r
                for r in universe
                if not _up_to_date_cheap(
                    r,
                    s,
                    min_zoom=args.min_zoom,
                    max_zoom=args.max_zoom,
                    layer_name=layer_name_for(s),
                )
            ]
            sweep_elapsed = time.monotonic() - t_sweep
            print(
                f"update-all[{s}]: {len(universe)} cached pbf(s), "
                f"{len(stale)} need build "
                f"({len(universe) - len(stale)} current) in {sweep_elapsed:.2f}s",
                file=sys.stderr,
            )
            for r in stale:
                pairs.append((s, r))
    else:
        pairs = [(s, r) for s in sources for r in regions]

    seen_pairs: set[tuple[str, str]] = set()
    deduped_pairs: list[tuple[str, str]] = []
    for p in pairs:
        if p not in seen_pairs:
            seen_pairs.add(p)
            deduped_pairs.append(p)
    pairs = deduped_pairs

    if not pairs:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: nothing to do, all tilesets are current", file=sys.stderr)
            return 0
        parser.error(
            "no work to do (no regions provided and/or no stale entries found)"
        )

    if args.list:
        for s, r in pairs:
            print(f"{s}/{r}")
        print(f"{len(pairs)} (source, region) pair(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        missing = [
            (s, r)
            for s, r in pairs
            if not _up_to_date_cheap(
                r, s, min_zoom=args.min_zoom, max_zoom=args.max_zoom,
                layer_name=layer_name_for(s),
            )
        ]
        for s, r in missing:
            print(f"{s}/{r}")
        print(
            f"{len(missing)} not yet built of {len(pairs)} resolved "
            f"({len(pairs) - len(missing)} already current)",
            file=sys.stderr,
        )
        return 0

    if not args.dry_run and shutil.which(args.tippecanoe) is None and not Path(args.tippecanoe).is_file():
        print(
            f"error: {args.tippecanoe!r} not found. "
            "Install via tools/install-tools.sh or 'brew install tippecanoe'.",
            file=sys.stderr,
        )
        return 2

    results = {"built": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str, str]] = []

    def _run(pair: tuple[str, str]) -> tuple[str, str | None]:
        s, r = pair
        try:
            outcome = _run_one(
                r,
                s,
                min_zoom=args.min_zoom,
                max_zoom=args.max_zoom,
                layer_name=args.layer_name,
                force=args.force,
                dry_run=args.dry_run,
                tippecanoe_bin=args.tippecanoe,
                timeout_seconds=args.timeout,
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
        f"\nSummary: {results['built']} built, {results['skipped']} skipped, "
        f"{results['dry-run']} dry-run, {results['failed']} failed",
        file=sys.stderr,
    )
    if failures:
        for s, r, msg in failures:
            print(f"  - {s}/{r}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
