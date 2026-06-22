"""Delta-update cached OSM PBFs from Geofabrik replication diffs — ONE region at
a time, with a delay between requests (rate-limit-friendly).

The throttled, serial, command-line counterpart to the ``osm.cache.UpdateRegion``
facet. It applies replication DIFFS (KB–MB per region) to each cached extract —
it does NOT re-download whole PBFs (the heavy ``download-pbf --update-all`` path
that can trip Geofabrik's rate limit). Use it to catch caches up after they've
drifted, e.g. once a rate-limit block clears.

Safe by default: ``diff_only`` is on, so a region with no cached baseline
(``uncached``) or one too far behind for the diff budget (``stale``) is reported
and SKIPPED rather than triggering a multi-GB full download. Pass ``--allow-full``
to opt into the full-download fallback.

Usage::

    # specific regions, 5s apart (default)
    python update_delta.py europe/germany north-america/us/california

    # every cached region, one at a time, 10s between requests
    python update_delta.py --all --delay 10

    # preview what would run
    python update_delta.py --all --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools import pbf_download, pbf_update  # noqa: E402


def _read_regions_file(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("regions", nargs="*",
                   help="Geofabrik region paths (e.g. europe/germany).")
    p.add_argument("--all", action="store_true",
                   help="Delta-update EVERY cached region (one at a time).")
    p.add_argument("--regions-file", type=Path,
                   help="File with one region path per line (# for comments).")
    p.add_argument("--delay", type=float, default=5.0,
                   help="Seconds to wait between regions (default: 5). Be kind to Geofabrik.")
    p.add_argument("--max-diff-mb", type=int, default=512,
                   help="Per-region diff budget in MB (default: 512).")
    p.add_argument("--allow-full", action="store_true",
                   help="Permit a full re-download for regions with no baseline or too "
                        "stale for diffs (default: skip them — diffs only, rate-limit-safe).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the regions that would be updated, then exit.")
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)

    regions: list[str] = list(args.regions)
    if args.regions_file:
        regions += _read_regions_file(args.regions_file)
    if args.all:
        regions += pbf_download.list_cached_regions()
    # de-dup, preserve order
    seen: set[str] = set()
    regions = [r for r in regions if not (r in seen or seen.add(r))]

    if not regions:
        p.error("no regions — pass region paths, --regions-file, or --all.")

    mode = "full-fallback ON" if args.allow_full else "diffs only"
    print(f"# delta-update: {len(regions)} region(s), one at a time, {args.delay}s between "
          f"requests, max_diff={args.max_diff_mb}MB, {mode}", file=sys.stderr)

    if args.dry_run:
        for r in regions:
            print(r)
        return 0

    counts: dict[str, int] = {}
    failures: list[str] = []
    t0 = time.monotonic()
    n = len(regions)
    for i, r in enumerate(regions):
        try:
            res = pbf_update.update_region(
                r, max_diff_mb=args.max_diff_mb, diff_only=not args.allow_full)
            counts[res.method] = counts.get(res.method, 0) + 1
            note = ""
            if res.method in ("uncached", "stale", "no_baseline"):
                note = "  (skipped — not eligible for a diff; cache it first or --allow-full)"
            print(f"[{i + 1}/{n}] {r}  {res.method}  {res.applied_bytes / 1e6:.1f}MB{note}")
        except Exception as exc:  # noqa: BLE001 - one bad region shouldn't stop the rest
            print(f"[{i + 1}/{n}] {r}  ERROR: {exc}", file=sys.stderr)
            failures.append(r)
        if i < n - 1 and args.delay > 0:
            time.sleep(args.delay)

    elapsed = time.monotonic() - t0
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing"
    print(f"# done in {elapsed:.0f}s: {summary} | {len(failures)} failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
