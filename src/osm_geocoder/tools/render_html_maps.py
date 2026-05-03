"""Render per-region HTML map pages from cached PMTiles.

Builds one self-contained directory per region under ``html/<region>-latest/``:

- ``index.html`` — a MapLibre GL JS page that loads the region's PMTiles
  and renders water / parks / protected areas / roads (classified by
  OSM highway class) / POIs as styled interactive layers with click
  popups showing raw OSM tags.
- ``style.json`` — the generated MapLibre style referencing the
  region's PMTiles via relative ``pmtiles://`` URLs.

Also rewrites ``html/index.html`` — the master index page listing every
rendered region with links and timestamps.

Serve the result with a static HTTP server rooted at the cache root::

    python -m http.server --directory /Volumes/afl_data/osm 8000
    open http://localhost:8000/html/

Usage::

    python render_html_maps.py europe/liechtenstein
    python render_html_maps.py --all
    python render_html_maps.py --update-all
    python render_html_maps.py --list
    python render_html_maps.py --master-index-only

Requires no binaries — the renderer is pure Python. The generated HTML
pulls MapLibre and pmtiles.js from a CDN (internet required at view
time; page itself is static).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.html_render import (  # noqa: E402
    STYLE_VERSION,
    RenderError,
    RenderResult,
    html_abs_path,
    is_up_to_date,
    list_rendered,
    master_index_path,
    render_region,
    write_master_index,
    _discover_sources,
)
from _lib import sidecar  # noqa: E402
from _lib.pbf_download import (  # noqa: E402
    filter_leaves,
    regions_from_pbf_cache,
)

DEFAULT_JOBS = 4
NAMESPACE = "osm"
VECTOR_CACHE_TYPE = "vector_tiles"


def _read_regions_file(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _regions_with_tiles(prefix: str | None = None) -> list[str]:
    """Return regions that have at least one PMTiles entry in vector_tiles."""
    regions: set[str] = set()
    for entry in sidecar.list_entries(NAMESPACE, VECTOR_CACHE_TYPE):
        rel = entry.get("relative_path", "")
        if "-latest/" not in rel:
            continue
        base = rel.split("-latest/")[0]
        regions.add(base)
    out = sorted(regions)
    if prefix:
        p = prefix.strip().strip("/")
        pref = p + "/"
        out = [r for r in out if r == p or r.startswith(pref)]
    return out


def _up_to_date_cheap(region: str) -> bool:
    sources = _discover_sources(region)
    if not sources:
        return False
    return is_up_to_date(region, sources)


def _run_one(region: str, *, force: bool, dry_run: bool) -> str:
    if dry_run:
        print(f"[{region}] would render -> {html_abs_path(region)}/", file=sys.stderr)
        return "dry-run"
    try:
        result: RenderResult = render_region(region, force=force)
    except RenderError as exc:
        print(f"[{region}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(f"[{region}] up-to-date, skipping", file=sys.stderr)
        return "skipped"
    print(
        f"[{region}] done ({len(result.sources)} source layer(s), "
        f"{result.total_size_bytes} bytes, {result.duration_seconds:.3f}s)",
        file=sys.stderr,
    )
    return "rendered"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render per-region HTML map pages from cached PMTiles using "
            "MapLibre GL JS. Refreshes the master html/index.html at the "
            "end of every run."
        ),
    )
    parser.add_argument("regions", nargs="*")
    parser.add_argument("--regions-file", type=Path)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Render every region that has PMTiles in the vector_tiles cache.",
    )
    parser.add_argument("--all-under", metavar="PREFIX")
    parser.add_argument("--include-parents", action="store_true")
    parser.add_argument("--update-all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="List regions whose rendered HTML is missing or stale "
        "(source PMTiles SHA changed or style_version bumped).",
    )
    parser.add_argument(
        "--master-index-only",
        action="store_true",
        help="Just regenerate html/index.html from the current manifest and exit.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    args = parser.parse_args()

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    if args.master_index_only:
        write_master_index()
        print(f"master index: {master_index_path()}", file=sys.stderr)
        return 0

    # Resolve regions.
    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions.extend(_read_regions_file(args.regions_file))

    if args.all or args.all_under is not None:
        from_tiles = _regions_with_tiles(prefix=args.all_under)
        before = len(from_tiles)
        if not args.include_parents:
            from_tiles = filter_leaves(from_tiles)
        print(
            f"vector_tiles cache: {before} region(s) matched, "
            f"{len(from_tiles)} selected after "
            f"{'leaves-only' if not args.include_parents else 'include-parents'} filter",
            file=sys.stderr,
        )
        regions.extend(from_tiles)

    seen: set[str] = set()
    deduped: list[str] = []
    for r in regions:
        if r and r not in seen:
            seen.add(r)
            deduped.append(r)
    regions = deduped

    if args.update_all:
        universe = _regions_with_tiles()
        if not args.include_parents:
            universe = filter_leaves(universe)
        stale = [r for r in universe if not _up_to_date_cheap(r)]
        current = len(universe) - len(stale)
        print(
            f"update-all: {len(universe)} region(s) with tiles, "
            f"{len(stale)} need render ({current} already current)",
            file=sys.stderr,
        )
        regions = stale

    if not regions:
        if args.update_all and not (args.all or args.all_under or args.regions):
            # Even on a no-op update, refresh the master index so it reflects
            # any manifest drift (e.g. after removing entries).
            write_master_index()
            print("update-all: nothing to do, all pages are current", file=sys.stderr)
            print(f"master index: {master_index_path()}", file=sys.stderr)
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
            f"{len(missing)} not yet rendered of {len(regions)} resolved "
            f"({len(regions) - len(missing)} already current)",
            file=sys.stderr,
        )
        return 0

    results = {"rendered": 0, "skipped": 0, "dry-run": 0, "failed": 0}
    failures: list[tuple[str, str]] = []

    def _run(region: str) -> tuple[str, str | None]:
        try:
            return _run_one(region, force=args.force, dry_run=args.dry_run), None
        except Exception as exc:  # noqa: BLE001
            return "failed", str(exc)

    if args.jobs == 1 or len(regions) == 1:
        for r in regions:
            outcome, err = _run(r)
            if err:
                failures.append((r, err))
            results[outcome] += 1
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(_run, r): r for r in regions}
            for fut in concurrent.futures.as_completed(futures):
                r = futures[fut]
                outcome, err = fut.result()
                if err:
                    failures.append((r, err))
                results[outcome] += 1

    # Rewrite the master index at the end so it always reflects the
    # current manifest, not the pre-run state.
    write_master_index()
    print(
        f"\nSummary: {results['rendered']} rendered, {results['skipped']} skipped, "
        f"{results['dry-run']} dry-run, {results['failed']} failed",
        file=sys.stderr,
    )
    print(f"master index: {master_index_path()}", file=sys.stderr)
    if failures:
        for r, msg in failures:
            print(f"  - {r}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
