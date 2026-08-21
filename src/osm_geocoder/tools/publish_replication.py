"""Publish per-region OSM replication diffs — the producer half of the split.

Phase 1 split the planet and stamped every extract to follow OUR replication
URL, but never published anything there: each ``state.txt`` carried a bare
timestamp with no sequenceNumber and no ``.osc.gz`` ever existed. Every extract
has therefore been a frozen snapshot. This fills those directories.

    python publish_replication.py --status
    python publish_replication.py --anchor 5051      # one-time baseline
    python publish_replication.py --days 3           # publish 3 new days
    python publish_replication.py --days 40          # catch all the way up

The anchor is one-time and NEVER inferred: it must be the last upstream
sequence already contained in the served extract. Prefer the sequence just
BEFORE the extract's timestamp — re-applying a day is idempotent, whereas
anchoring too late leaves a permanent gap in the stream.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools import replication_publish as rp  # noqa: E402


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def cmd_status(args) -> int:
    www = Path(args.www) if args.www else rp.www_root()
    up_seq, up_ts = rp.upstream_state(args.upstream)
    print(f"upstream: sequence {up_seq} ({up_ts})")
    print(f"tree:     {www}")
    print()
    print(f"{'region':<20}{'published':>11}{'behind':>8}  extract timestamp")
    print("-" * 72)
    for r in rp.discover_regions(www):
        seq, _ts = rp.region_state(r, www)
        ext = rp.extract_timestamp(r, www) if args.slow else "(--slow to read)"
        if seq is None:
            print(f"{r:<20}{'never':>11}{'—':>8}  {ext}")
        else:
            print(f"{r:<20}{seq:>11}{up_seq - seq:>8}  {ext}")
    return 0


def cmd_anchor(args) -> int:
    www = Path(args.www) if args.www else rp.www_root()
    regions = args.region or rp.discover_regions(www)
    ts = rp.diff_timestamp(args.anchor, upstream=args.upstream)
    if not ts:
        print(f"could not read upstream timestamp for sequence {args.anchor}", file=sys.stderr)
        return 1
    touched = rp.anchor(regions, args.anchor, ts, www=www)
    print(f"anchored {len(touched)} region(s) at sequence {args.anchor} ({ts}):")
    for r in touched:
        print(f"  {r}")
    print("\nThis is the baseline the next --days run publishes FROM. It must be a")
    print("sequence already contained in the served extract; if unsure, go one lower —")
    print("a re-applied day is idempotent, a skipped day is a permanent gap.")
    return 0


def cmd_stamp(args) -> int:
    import os

    www = Path(args.www) if args.www else rp.www_root()
    base = args.base_url or os.environ.get(rp.BASE_URL_ENV, "")
    if not base:
        print(f"--stamp-extracts needs a base URL (--base-url or ${rp.BASE_URL_ENV})",
              file=sys.stderr)
        return 1
    regions = args.region or rp.discover_regions(www)
    print(f"stamping {len(regions)} extract(s) — each is a full REWRITE, not a patch\n")
    rc = 0
    for r in regions:
        # The EXTRACT's sequence, never the published head — the head runs
        # ahead by design, and stamping it would make consumers skip every
        # diff published so far.
        seq, ts = rp.extract_state(r, www)
        if seq is None:
            print(f"  {r:<20} skipped — no extract sequence recorded; --anchor first")
            rc = 2
            continue
        try:
            size = rp.stamp_extract(r, seq, ts, f"{base.rstrip('/')}/{r}-updates", www=www)
        except rp.ReplicationError as exc:
            print(f"  {r:<20} FAILED — {exc}")
            rc = 1
            continue
        print(f"  {r:<20} sequence {seq} ({ts})  {_fmt_bytes(size)}")
    return rc


def cmd_check(args) -> int:
    """Verify the whole chain and exit non-zero if any link has stalled.

    Every other failure in this pipeline is loud. This one was not: if the
    nightly job dies, the stream stops advancing, the index ages out of its
    freshness budget, and consumers quietly revert to their fallback. Nothing
    errors — the maps keep rendering, from a slower source, forever. So the
    stall needs something that actively looks for it.
    """
    import os
    from datetime import UTC, datetime

    www = Path(args.www) if args.www else rp.www_root()
    problems: list[str] = []

    up_seq, up_ts = rp.upstream_state(args.upstream)
    print(f"upstream: {up_seq} ({up_ts})")

    behind_limit = args.max_days_behind
    for r in rp.discover_regions(www):
        seq, _ts = rp.region_state(r, www)
        if seq is None:
            problems.append(f"{r}: never published")
            continue
        behind = up_seq - seq
        flag = "" if behind <= behind_limit else "  <-- STALLED"
        print(f"  stream {r:<18} {seq}  ({behind} behind){flag}")
        if behind > behind_limit:
            problems.append(f"{r}: stream {behind} days behind (limit {behind_limit})")

    names = [i for i in (args.check_index or []) if i]
    if not names:
        names = [i for i in os.environ.get("FW_OSM_NIGHTLY_INDEXES", "").replace(",", " ").split() if i]
    for name in names:
        try:
            from _osm_tools import tag_index as ti

            st = ti.stats(name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"index {name}: unreadable ({exc})")
            continue
        age_h = None
        if st.updated_at:
            try:
                when = datetime.strptime(st.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                age_h = (datetime.now(UTC) - when).total_seconds() / 3600.0
            except ValueError:
                age_h = None
        behind = (up_seq - st.sequence) if st.sequence is not None else None
        age_s = f"{age_h:.0f}h" if age_h is not None else "unknown"
        flag = ""
        if age_h is None or age_h > args.max_index_age_hours:
            flag = "  <-- STALLED"
            problems.append(f"index {name}: last updated {age_s} ago "
                            f"(limit {args.max_index_age_hours:.0f}h)")
        elif behind is not None and behind > behind_limit:
            flag = "  <-- BEHIND"
            problems.append(f"index {name}: {behind} sequences behind")
        print(f"  index  {name:<18} {st.sequence}  {st.count} rows, updated {age_s} ago{flag}")

    print()
    if problems:
        print("STALLED:")
        for p_ in problems:
            print(f"  - {p_}")
        return 1
    print("OK — stream and indexes are current.")
    return 0


def cmd_apply(args) -> int:
    import os

    www = Path(args.www) if args.www else rp.www_root()
    base = args.base_url or os.environ.get(rp.BASE_URL_ENV, "")
    if not base:
        print(f"--apply needs a base URL (--base-url or ${rp.BASE_URL_ENV})", file=sys.stderr)
        return 1
    regions = args.region or rp.discover_regions(www)
    print(f"rolling {len(regions)} served extract(s) forward over published diffs\n")
    rc = 0
    for r in regions:
        try:
            frm, to, size = rp.apply_published(r, base, www=www)
        except rp.ReplicationError as exc:
            print(f"  {r:<20} FAILED — {exc}")
            rc = 1
            continue
        if to == frm:
            print(f"  {r:<20} already at {to}")
        else:
            print(f"  {r:<20} {frm} -> {to}  ({_fmt_bytes(size)})")
    return rc


def cmd_publish(args) -> int:
    www = Path(args.www) if args.www else rp.www_root()
    res = rp.publish(
        regions=args.region or None,
        max_days=args.days,
        upstream=args.upstream,
        www=www,
        polys=Path(args.polys) if args.polys else None,
        dry_run=args.dry,
        update_indexes=[i for i in (args.update_index or []) if i],
    )
    print(f"upstream at {res.upstream_sequence}; published {res.from_sequence} -> "
          f"{res.to_sequence} ({res.days} day(s), {_fmt_bytes(res.planet_bytes)} fetched)")
    for e in res.index_errors:
        print(f"  INDEX ERROR {e}")
    for r in res.regions:
        if r.skipped:
            print(f"  {r.region:<20} skipped — {r.reason}")
        elif r.published:
            print(f"  {r.region:<20} +{len(r.published)} diff(s), "
                  f"{_fmt_bytes(r.bytes_written)}")
        else:
            print(f"  {r.region:<20} nothing to do")
    if any(r.skipped and "anchor" in r.reason for r in res.regions):
        return 2
    return 1 if res.index_errors else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="publish_replication", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--status", action="store_true", help="show how far behind each region is")
    p.add_argument("--anchor", type=int, metavar="SEQ",
                   help="one-time: set the baseline sequence for regions that never published")
    p.add_argument("--days", type=int, default=0, metavar="N",
                   help="publish at most N new days of diffs")
    p.add_argument("--region", action="append", default=[], help="limit to a region (repeatable)")
    p.add_argument("--www", default="", help=f"published tree (default ${rp.WWW_ENV})")
    p.add_argument("--polys", default="", help=f"region polygons (default ${rp.POLYS_ENV})")
    p.add_argument("--upstream", default="", help=f"upstream replication URL (default {rp.DEFAULT_UPSTREAM})")
    p.add_argument("--check", action="store_true",
                   help="verify the whole chain (stream + indexes) and exit non-zero if "
                        "anything has stalled. Every other failure here is loud; a stopped "
                        "nightly job is not, because consumers just fall back.")
    p.add_argument("--check-index", action="append", default=[], metavar="NAME",
                   help="index to include in --check (default $FW_OSM_NIGHTLY_INDEXES)")
    p.add_argument("--max-days-behind", type=int, default=3,
                   help="--check: stream/index days behind upstream before stalling (3)")
    p.add_argument("--max-index-age-hours", type=float, default=72.0,
                   help="--check: index age before stalling (72)")
    p.add_argument("--update-index", action="append", default=[], metavar="NAME",
                   help="also advance this tag index with each day's diff (repeatable). "
                        "Done in the publish loop because the diff is already local and "
                        "the sequences are already walked in order.")
    p.add_argument("--apply", action="store_true",
                   help="roll the SERVED extracts forward over diffs already published "
                        "for them (one osmium pass each; no HTTP). Publishing does not "
                        "apply, so anything reading these FILES directly stays as old "
                        "as the last --apply.")
    p.add_argument("--stamp-extracts", action="store_true",
                   help="one-time: write the replication sequence into each served extract's "
                        "PBF header (REWRITES the file — minutes to an hour each). Without "
                        "it consumers cannot delta and fall back to a full re-download.")
    p.add_argument("--base-url", default="",
                   help=f"--stamp-extracts: URL to stamp (default ${rp.BASE_URL_ENV})")
    p.add_argument("--slow", action="store_true", help="--status: also read each extract's header")
    p.add_argument("--dry", action="store_true", help="say what would be fetched, write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    a.upstream = a.upstream or None

    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        if a.status:
            return cmd_status(a)
        if a.anchor is not None:
            return cmd_anchor(a)
        if a.check:
            return cmd_check(a)
        if a.apply:
            return cmd_apply(a)
        if a.stamp_extracts:
            return cmd_stamp(a)
        if a.days:
            return cmd_publish(a)
        p.print_help()
        return 0
    except rp.ReplicationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
