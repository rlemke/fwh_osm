"""Maintain a self-hosted regional extract set (Phase 2, Strategy A).

Thin CLI over ``_osm_tools.planet_maintain.maintain``: advance the master planet by
applying its replication diffs, then re-extract all regions (Phase 1 bootstrap) so
the served ``<region>-latest.osm.pbf`` files are fresh. Point ``FW_GEOFABRIK_BASE_URL``
at the served tree and the existing fwh_osm download path consumes them unchanged.

Run under a scheduler (cron / launchd / systemd timer / ``fw maint``), typically
nightly. Serving the output tree is an infra concern (nginx / caddy / MinIO); for a
quick local check: ``python -m http.server --directory <out>``.

Usage::

    python planet_maintain.py \
        --master /data/planet-latest.osm.pbf \
        --out /data/extracts \
        --regions regions.json \
        --base-url http://server3.local:8080/osm

    # regions.json — same spec as planet_bootstrap:
    #   [{"key": "europe/germany", "poly": "poly/germany.poly"}, ...]

Requires the ``osmium`` binary and pyosmium.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.planet_bootstrap import STRATEGIES, BootstrapError  # noqa: E402
from _osm_tools.planet_maintain import maintain  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Advance the master planet and re-extract regional PBFs (Phase 2).",
    )
    ap.add_argument("--master", required=True, help="master PBF kept current in place")
    ap.add_argument("--out", required=True, help="output root (Geofabrik-style layout)")
    ap.add_argument("--regions", required=True, help="JSON file: [{key, bbox|poly}, ...]")
    ap.add_argument("--base-url", required=True,
                    help="our extract+replication server base, e.g. http://server3.local:8080/osm")
    ap.add_argument("--strategy", default="smart", choices=STRATEGIES)
    ap.add_argument("--max-diff-mb", type=int, default=1024,
                    help="cap per-run replication catch-up (default 1024)")
    args = ap.parse_args(argv)

    try:
        regions = json.loads(Path(args.regions).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read --regions {args.regions}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(regions, list) or not regions:
        print("error: --regions must be a non-empty JSON list", file=sys.stderr)
        return 2

    try:
        res = maintain(
            master=args.master, out=args.out, regions=regions, base_url=args.base_url,
            strategy=args.strategy, max_diff_mb=args.max_diff_mb,
            on_log=lambda m: print(f"[maintain] {m}", file=sys.stderr),
        )
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(
        {"master": res.master.__dict__, "base_url": args.base_url,
         "regions": [r.__dict__ for r in res.regions]},
        sys.stdout, indent=2,
    )
    sys.stdout.write("\n")
    print(f"[maintain] master {res.master.status}; {len(res.regions)} region(s) re-extracted",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
