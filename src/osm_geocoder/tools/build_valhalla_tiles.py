"""Build Valhalla routing tilesets from cached OSM PBFs.

Thin CLI wrapper around ``_lib.valhalla_build.build_tiles``. Both this
tool and the FFL ``osm.ops.Valhalla.BuildTiles`` handler call that
library, so they share one cache layout and one manifest.

Cache layout::

    <cache_root>/valhalla/<region>-latest/
        0/...    level 0 tiles (inter-regional)
        1/...    level 1 tiles (regional)
        2/...    level 2 tiles (local, densest)

Valhalla profiles (``auto``, ``bicycle``, ``pedestrian``, ``truck``,
``motor_scooter``, ``motorcycle``, ``bus``, ``taxi``) are **query-time**
costing models — one tileset serves every profile. There is no
build-time profile axis, so this tool has no ``--profile`` flag.

Cross-region routing works natively within a tileset: if you build
``europe/germany``, queries across sub-state boundaries Just Work. If
you need to route between separately-built tilesets, either merge the
PBFs first or build a parent region.

Usage::

    python build_valhalla_tiles.py europe/liechtenstein
    python build_valhalla_tiles.py --all-under europe/germany
    python build_valhalla_tiles.py --update-all
    python build_valhalla_tiles.py --list-missing --all

Requires Valhalla binaries (``valhalla_build_config``,
``valhalla_build_tiles``). Install via ``tools/install-tools.sh`` or
``brew install valhalla`` directly.
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
from _lib.valhalla_build import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    NAMESPACE,
    QUERY_PROFILES,
    SOURCE_CACHE_TYPE,
    VALHALLA_VERSION,
    BuildError,
    BuildResult,
    build_tiles,
    is_up_to_date,
    pbf_abs_path,
    tileset_abs_path,
)

DEFAULT_JOBS = 1  # Valhalla builds are CPU+RAM heavy; default to serial


def _help_epilog() -> str:
    return (
        f"valhalla version: {VALHALLA_VERSION}\n"
        f"\nquery-time profiles (no build-time axis): {', '.join(QUERY_PROFILES)}\n"
        "\ncache validity requires matching source PBF SHA-256 AND matching\n"
        "valhalla_version — a toolchain upgrade invalidates all tilesets\n"
        "automatically, no per-region --force needed.\n"
        "\ncross-region routing: a single tileset seamlessly routes across its\n"
        "entire extent. If you need coverage across separately-built tilesets,\n"
        "build a parent region (e.g. europe/germany rather than individual\n"
        "states)."
    )


def _read_regions_file(path: Path) -> list[str]:
    regions: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        regions.append(line)
    return regions


def _up_to_date_cheap(region: str) -> bool:
    pbf_rel = f"{region}-latest.osm.pbf"
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel)
    if not pbf_side:
        return False
    return is_up_to_date(region, pbf_side, tileset_abs_path(region))


def _run_one(
    region: str,
    *,
    force: bool,
    dry_run: bool,
    config_bin: str,
    tiles_bin: str,
    timeout_seconds: int,
) -> str:
    if dry_run:
        src = pbf_abs_path(region)
        dst = tileset_abs_path(region)
        print(f"[{region}] would build {src} -> {dst}/", file=sys.stderr)
        return "dry-run"
    try:
        result: BuildResult = build_tiles(
            region,
            force=force,
            config_bin=config_bin,
            tiles_bin=tiles_bin,
            timeout_seconds=timeout_seconds,
        )
    except BuildError as exc:
        print(f"[{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    levels_str = ", ".join(f"L{k}={v}" for k, v in sorted(result.tile_levels.items()))
    print(
        f"[{region}] done "
        f"({result.total_size_bytes / (1024 * 1024):.1f} MiB, "
        f"{result.tile_count} tiles [{levels_str}], "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "built"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Valhalla routing tilesets from cached OSM PBFs.",
        epilog=_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("regions", nargs="*")
    parser.add_argument("--regions-file", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--all-under", metavar="PREFIX")
    parser.add_argument("--include-parents", action="store_true")
    parser.add_argument("--update-all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="Print resolved regions whose tileset is missing or stale "
        "(different source PBF SHA or Valhalla version) and exit.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Concurrent builds (default: {DEFAULT_JOBS}). Valhalla is "
        "CPU+RAM heavy per build; parallelism is constrained by RAM.",
    )
    parser.add_argument(
        "--config-bin",
        default="valhalla_build_config",
        help="Path to the valhalla_build_config binary (default: on PATH).",
    )
    parser.add_argument(
        "--tiles-bin",
        default="valhalla_build_tiles",
        help="Path to the valhalla_build_tiles binary (default: on PATH).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Per-build wall-clock limit in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

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

    seen: set[str] = set()
    deduped: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.add(r)
            deduped.append(r)
    regions = deduped

    if args.update_all:
        universe = regions_from_pbf_cache()
        if not args.include_parents:
            universe = filter_leaves(universe)
        stale = [r for r in universe if not _up_to_date_cheap(r)]
        current = len(universe) - len(stale)
        print(
            f"update-all: {len(universe)} cached pbf(s), "
            f"{len(stale)} need build ({current} already current)",
            file=sys.stderr,
        )
        # update-all replaces the caller's region set with the stale subset.
        regions = stale

    if not regions:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: nothing to do, all tilesets are current", file=sys.stderr)
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
        missing = [r for r in regions if not _up_to_date_cheap(r)]
        for r in missing:
            print(r)
        print(
            f"{len(missing)} not yet built of {len(regions)} resolved "
            f"({len(regions) - len(missing)} already current)",
            file=sys.stderr,
        )
        return 0

    # Prereq checks before launching real work. Skipped for --dry-run
    # so callers can rehearse the tool without Valhalla installed.
    if not args.dry_run:
        if shutil.which(args.config_bin) is None and not Path(args.config_bin).is_file():
            print(
                f"error: {args.config_bin!r} not found. "
                "Install Valhalla (e.g. 'brew install valhalla' or run "
                "tools/install-tools.sh).",
                file=sys.stderr,
            )
            return 2
        if shutil.which(args.tiles_bin) is None and not Path(args.tiles_bin).is_file():
            print(
                f"error: {args.tiles_bin!r} not found. "
                "Install Valhalla (e.g. 'brew install valhalla').",
                file=sys.stderr,
            )
            return 2

    results = {"built": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    def _run(region: str) -> tuple[str, str | None]:
        try:
            outcome = _run_one(
                region,
                force=args.force,
                dry_run=args.dry_run,
                config_bin=args.config_bin,
                tiles_bin=args.tiles_bin,
                timeout_seconds=args.timeout,
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
        f"\nSummary: {results['built']} built, "
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
