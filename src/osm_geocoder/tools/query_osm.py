"""Ask OpenStreetMap an arbitrary tag question from the LOCAL extracts.

Thin CLI wrapper around ``_osm_tools.tag_query.query_region``; the same library
backs the FFL ``osm.query.TagQuery`` handler, so the tool and the workflow share
one cache layout and one sidecar discipline.

No Overpass, no API key, no rate limit — this reads the PBFs already on disk
(the download cache, plus any tree named in ``FW_OSM_LOCAL_EXTRACTS``). The
answer is cached per *(region, filter)*, keyed by a digest of the filter itself,
so asking twice is free and asking something different cannot return the
previous answer.

Usage::

    python query_osm.py north-america/us/utah --filter 'nwr/amenity=pharmacy'
    python query_osm.py europe asia --filter 'nwr/man_made=surveillance'
    python query_osm.py north-america --filter 'nwr/man_made=surveillance' \\
                                      --where 'surveillance:type=ALPR'
    python query_osm.py europe/germany --filter 'nwr/amenity=cafe' --json

``--filter`` is osmium tags-filter syntax; terms are OR-ed
(``nwr/amenity=pharmacy,doctors``, ``w/highway=motorway n/amenity=fuel``).

``--where`` is the AND that osmium cannot express: it post-filters the produced
GeoJSON on a property, which is how "an ALPR camera" (``man_made=surveillance``
AND ``surveillance:type=ALPR``) is asked. It costs one pass over the matches,
not a second pass over the extract.

Progress goes to stderr, results to stdout, so ``--json`` pipes cleanly.

⚠️ Cost is driven by MATCHES, not just source size: the export pass assembles a
geometry per matching feature. Measured here, ``nwr/man_made=surveillance`` ran
at ~31 MB/s over 853 MB (744 matches) but ~6.8 MB/s over 20 GB (155,485
matches). Do not extrapolate a planet scan from a small region.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.pbf_extract import ExtractionError  # noqa: E402
from _osm_tools.tag_query import query_region  # noqa: E402


def _post_filter(path: str, where: str) -> tuple[int, list[dict]]:
    """Count (and collect) features whose property matches ``key=value``."""
    key, _, want = where.partition("=")
    key = key.strip()
    want = want.strip().lower()
    if not key or not want:
        raise ExtractionError(f"--where must be key=value, got {where!r}")
    hits: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\x1e \t\r\n")
            if not line:
                continue
            feature = json.loads(line)
            props = feature.get("properties") or {}
            if str(props.get(key, "")).lower() == want:
                hits.append(feature)
    return len(hits), hits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="query_osm",
        description="Ad-hoc OSM tag query against the local extracts (no Overpass).",
    )
    p.add_argument("regions", nargs="+", help="Geofabrik-style keys, e.g. europe/germany")
    p.add_argument("--filter", required=True, help="osmium tags-filter expression")
    p.add_argument("--where", help="post-filter the result on a property (key=value)")
    p.add_argument("--force", action="store_true", help="re-scan even if cached")
    p.add_argument("--json", action="store_true", help="emit JSON on stdout")
    p.add_argument(
        "-o", "--output",
        help="with --where, write the matching features here as GeoJSON-seq",
    )
    args = p.parse_args(argv)

    osmium_bin = os.environ.get("FW_OSMIUM_BIN", "osmium")
    results: list[dict] = []
    failures: list[tuple[str, str]] = []

    for region in args.regions:
        try:
            res = query_region(region, args.filter, force=args.force, osmium_bin=osmium_bin)
        except ExtractionError as exc:
            failures.append((region, str(exc)))
            print(f"  {region}: {exc}", file=sys.stderr)
            continue

        row = {
            "region": region,
            "matches": res.feature_count,
            "cached": res.was_cached,
            "seconds": res.duration_seconds,
            "path": res.path,
            "digest": res.digest,
        }
        if args.where:
            n, hits = _post_filter(res.path, args.where)
            row["where"] = args.where
            row["where_matches"] = n
            if args.output:
                with open(args.output, "a", encoding="utf-8") as out:
                    for feature in hits:
                        out.write(json.dumps(feature) + "\n")
                row["output"] = args.output
        results.append(row)

        note = "cached" if res.was_cached else f"{res.duration_seconds:.1f}s"
        extra = f" · {row['where_matches']} matching {args.where}" if args.where else ""
        print(f"  {region:<32} {res.feature_count:>9} features ({note}){extra}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        total = sum(r["matches"] for r in results)
        line = f"\nTotal: {total} features across {len(results)} region(s)"
        if args.where:
            line += f"; {sum(r.get('where_matches', 0) for r in results)} matching {args.where}"
        print(line, file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
