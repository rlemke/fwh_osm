"""Download GTFS transit feeds per agency into the gtfs/ cache.

Cache validity uses HTTP ``Last-Modified`` / ``ETag`` — a HEAD request
decides whether to skip or re-download. No full download happens if
the remote hasn't changed.

Usage::

    python download_gtfs.py --agency bart \\
        --url https://www.bart.gov/dev/schedules/google_transit.zip

    python download_gtfs.py --list
    python download_gtfs.py --update-all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib.gtfs_download import (  # noqa: E402
    DownloadError,
    DownloadResult,
    download,
    is_up_to_date_cheap,
    list_feeds,
)


def _run_one(agency: str, url: str, *, force: bool, dry_run: bool) -> str:
    if dry_run:
        print(f"[{agency}] would check / download {url}", file=sys.stderr)
        return "dry-run"
    try:
        result: DownloadResult = download(agency, url, force=force)
    except DownloadError as exc:
        print(f"[{agency}] FAILED: {exc}", file=sys.stderr)
        raise
    if result.was_cached:
        print(
            f"[{agency}] up-to-date (last-modified={result.last_modified}), skipping",
            file=sys.stderr,
        )
        return "skipped"
    print(
        f"[{agency}] done "
        f"({result.size_bytes / (1024 * 1024):.2f} MiB, "
        f"feed_version={result.feed_info.get('feed_version') or 'n/a'}, "
        f"{result.duration_seconds:.1f}s)",
        file=sys.stderr,
    )
    return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download GTFS transit feeds per agency with manifest-tracked freshness.",
    )
    parser.add_argument(
        "--agency",
        help="Agency identifier (becomes the cache filename). Required for new downloads.",
    )
    parser.add_argument(
        "--url",
        help="GTFS zip URL. Required for new downloads; for --update-all the manifest records it.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recorded feeds with feed_version + last_modified and exit.",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Refresh every agency in the manifest: HEAD each URL, re-download if changed.",
    )
    parser.add_argument(
        "--list-missing",
        action="store_true",
        help="List agencies whose remote feed is newer than the cached copy, and exit.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.list:
        feeds = list_feeds()
        for entry in feeds:
            feed_info = entry.get("feed_info", {}) or {}
            http = entry.get("http", {}) or {}
            print(
                f"{entry.get('agency', ''):<20}\t"
                f"size={entry.get('size_bytes', 0)}\t"
                f"version={feed_info.get('feed_version') or 'n/a'}\t"
                f"last_modified={http.get('last_modified') or 'n/a'}\t"
                f"url={entry.get('source_url', '')}"
            )
        print(f"{len(feeds)} feed(s)", file=sys.stderr)
        return 0

    if args.list_missing:
        feeds = list_feeds()
        stale: list[dict] = []
        for entry in feeds:
            agency = entry.get("agency", "")
            url = entry.get("source_url", "")
            if not agency or not url:
                continue
            if not is_up_to_date_cheap(agency, url):
                stale.append(entry)
        for entry in stale:
            print(entry.get("agency", ""))
        print(
            f"{len(stale)} stale of {len(feeds)} feed(s)",
            file=sys.stderr,
        )
        return 0

    if args.update_all:
        feeds = list_feeds()
        if not feeds:
            print("update-all: manifest is empty, nothing to refresh", file=sys.stderr)
            return 0
        print(f"update-all: {len(feeds)} feed(s) in manifest", file=sys.stderr)
        results = {"downloaded": 0, "skipped": 0, "dry-run": 0, "failed": 0}
        failures: list[tuple[str, str]] = []
        for entry in feeds:
            agency = entry.get("agency", "")
            url = entry.get("source_url", "")
            if not agency or not url:
                failures.append((agency, "manifest entry missing agency or url"))
                results["failed"] += 1
                continue
            try:
                outcome = _run_one(agency, url, force=args.force, dry_run=args.dry_run)
                results[outcome] += 1
            except DownloadError as exc:
                failures.append((agency, str(exc)))
                results["failed"] += 1
        print(
            f"\nSummary: {results['downloaded']} downloaded, "
            f"{results['skipped']} skipped, {results['dry-run']} dry-run, "
            f"{results['failed']} failed",
            file=sys.stderr,
        )
        return 1 if failures else 0

    # Single-feed mode
    if not args.agency:
        parser.error("--agency is required for a single-feed download")
    if not args.url:
        parser.error("--url is required for a single-feed download")

    try:
        outcome = _run_one(args.agency, args.url, force=args.force, dry_run=args.dry_run)
    except DownloadError:
        return 1
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
