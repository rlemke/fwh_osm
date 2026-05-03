"""Build OSRM MLD routing graphs from cached OSM PBFs.

Thin CLI wrapper around ``_lib.osrm_build.build_graph``. Runs the
3-stage OSRM pipeline (extract → partition → customize) and stores
the result at ``<cache_root>/osrm/<region>-latest/<profile>/``.

Usage::

    python build_osrm_graph.py europe/liechtenstein
    python build_osrm_graph.py --profile bicycle europe/germany/berlin
    python build_osrm_graph.py --profiles car,bicycle,foot europe/liechtenstein
    python build_osrm_graph.py --all-profiles europe/liechtenstein
    python build_osrm_graph.py --update-all --profile car

Requires ``osrm-backend`` (``brew install osrm-backend`` or
``tools/install-tools.sh``). Uses OSRM's shipped .lua profiles from
``/opt/homebrew/share/osrm/profiles/`` (override via ``--profile-file``
or ``$OSRM_PROFILES_DIR``).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import sidecar  # noqa: E402
from _lib.osrm_build import (  # noqa: E402
    DEFAULT_TIMEOUT_SECONDS,
    NAMESPACE,
    OSRM_VERSION,
    PROFILES,
    SOURCE_CACHE_TYPE,
    BuildError,
    BuildResult,
    build_graph,
    default_profile_file,
    graph_abs_path,
    is_up_to_date,
    pbf_abs_path,
)
from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)

DEFAULT_JOBS = 1


def _read_regions_file(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _up_to_date_cheap(region: str, profile: str) -> bool:
    pbf_rel = f"{region}-latest.osm.pbf"
    pbf_side = sidecar.read_sidecar(NAMESPACE, SOURCE_CACHE_TYPE, pbf_rel)
    if not pbf_side:
        return False
    return is_up_to_date(region, profile, pbf_side, graph_abs_path(region, profile))


def _run_one(
    region: str,
    profile: str,
    *,
    force: bool,
    dry_run: bool,
    profile_file: str | None,
    timeout_seconds: int,
) -> str:
    if dry_run:
        src = pbf_abs_path(region)
        dst = graph_abs_path(region, profile)
        print(f"[{profile}/{region}] would build {src} -> {dst}/", file=sys.stderr)
        return "dry-run"
    try:
        result: BuildResult = build_graph(
            region,
            profile,
            force=force,
            profile_file=profile_file,
            timeout_seconds=timeout_seconds,
        )
    except BuildError as exc:
        print(f"[{profile}/{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{profile}/{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{profile}/{region}] done "
        f"({result.total_size_bytes / (1024 * 1024):.1f} MiB, "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "built"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build OSRM MLD routing graphs from cached OSM PBFs.",
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
        "--profile",
        default="car",
        choices=list(PROFILES),
        help=f"Routing profile (default: car). Valid: {', '.join(PROFILES)}.",
    )
    parser.add_argument(
        "--profiles",
        metavar="LIST",
        help="Comma-separated list of profiles to build per region.",
    )
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help=f"Build every supported profile: {', '.join(PROFILES)}.",
    )
    parser.add_argument(
        "--profile-file",
        help="Path to an OSRM .lua profile file. Overrides --profile's "
        "default lookup in OSRM's shipped profiles.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Concurrent builds (default: {DEFAULT_JOBS}). OSRM builds are "
        "CPU+RAM heavy; parallelism is constrained by RAM.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
    )
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    # Resolve profiles.
    if args.all_profiles:
        profiles = list(PROFILES)
    elif args.profiles:
        profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
        invalid = [p for p in profiles if p not in PROFILES]
        if invalid:
            parser.error(
                f"unknown profile(s): {', '.join(invalid)}. "
                f"Valid: {', '.join(PROFILES)}"
            )
    else:
        profiles = [args.profile]

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
        pairs: list[tuple[str, str]] = []
        for prof in profiles:
            stale = [r for r in universe if not _up_to_date_cheap(r, prof)]
            current = len(universe) - len(stale)
            print(
                f"update-all[{prof}]: {len(universe)} cached pbf(s), "
                f"{len(stale)} need build ({current} already current)",
                file=sys.stderr,
            )
            for r in stale:
                pairs.append((prof, r))
    else:
        pairs = [(prof, r) for prof in profiles for r in regions]

    seen_pairs: set[tuple[str, str]] = set()
    deduped_pairs: list[tuple[str, str]] = []
    for p in pairs:
        if p not in seen_pairs:
            seen_pairs.add(p)
            deduped_pairs.append(p)
    pairs = deduped_pairs

    if not pairs:
        if args.update_all and not (args.all or args.all_under or args.regions):
            print("update-all: nothing to do, all graphs are current", file=sys.stderr)
            return 0
        parser.error(
            "no work to do (no regions provided and/or no stale entries found)"
        )

    if args.list:
        for prof, r in pairs:
            print(f"{prof}/{r}")
        print(f"{len(pairs)} (profile, region) pair(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        missing = [(p, r) for p, r in pairs if not _up_to_date_cheap(r, p)]
        for p, r in missing:
            print(f"{p}/{r}")
        print(
            f"{len(missing)} not yet built of {len(pairs)} resolved "
            f"({len(pairs) - len(missing)} already current)",
            file=sys.stderr,
        )
        return 0

    # Prereq checks (skipped for --dry-run).
    if not args.dry_run:
        for binname in ("osrm-extract", "osrm-partition", "osrm-customize"):
            if shutil.which(binname) is None:
                print(
                    f"error: {binname!r} not found on PATH. "
                    "Install osrm-backend ('brew install osrm-backend' or "
                    "tools/install-tools.sh).",
                    file=sys.stderr,
                )
                return 2

    results = {"built": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str, str]] = []

    def _run(pair: tuple[str, str]) -> tuple[str, str | None]:
        prof, region = pair
        try:
            outcome = _run_one(
                region,
                prof,
                force=args.force,
                dry_run=args.dry_run,
                profile_file=args.profile_file,
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
        for prof, region, msg in failures:
            print(f"  - {prof}/{region}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
