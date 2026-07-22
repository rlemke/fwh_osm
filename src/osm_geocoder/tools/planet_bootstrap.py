"""Bootstrap Geofabrik-style regional extracts from a planet PBF (Strategy A).

Thin CLI wrapper around ``_osm_tools.planet_bootstrap.bootstrap``. One read of the
source PBF is split by ``osmium extract`` into per-region extracts, each stamped
with OUR ``osmosis_replication_*`` header so the existing delta path
(``osm.cache.UpdateRegion`` / ``update_delta.py``) follows our own server instead
of Geofabrik. Emits the Geofabrik-compatible layout ``<region>-latest.osm.pbf`` +
``<region>-updates/state.txt`` under ``--out``; serve that over HTTP and point
``FW_GEOFABRIK_BASE_URL`` at it to cut Geofabrik out of the critical path.

This is Phase 1 (the one-time split). Phase 2 (keep the master planet current with
``pyosmium-up-to-date`` + re-extract on a schedule, and serve the ``-updates/``
trees) is the ongoing maintenance loop.

Usage::

    # split a real planet with real .poly boundaries
    python planet_bootstrap.py \
        --source /data/planet-latest.osm.pbf \
        --out /data/extracts \
        --regions regions.json \
        --base-url http://server3.local:8080/osm

    # regions.json:
    #   [{"key": "europe/germany",           "poly": "poly/germany.poly"},
    #    {"key": "north-america/us/california","poly": "poly/california.poly"}]
    # bbox tiles (prototyping / axis-aligned) instead of a .poly:
    #   [{"key": "demo/west", "bbox": [7.409, 43.723, 7.425, 43.752]}]

Requires the ``osmium`` binary (osmium-tool) and pyosmium. The node-location index
for ``--strategy smart``/``complete_ways`` is disk-backed by osmium; provision the
scratch disk accordingly at planet scale.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.planet_bootstrap import (  # noqa: E402
    STRATEGIES,
    BootstrapError,
    bootstrap,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Split a planet PBF into Geofabrik-style regional extracts.",
    )
    ap.add_argument("--source", required=True,
                    help="source PBF (planet-latest.osm.pbf, or a stand-in extract)")
    ap.add_argument("--out", required=True, help="output root (Geofabrik-style layout)")
    ap.add_argument("--regions", required=True,
                    help="JSON file: [{key, bbox:[min_lon,min_lat,max_lon,max_lat] | poly:path}, ...]")
    ap.add_argument("--base-url", required=True,
                    help="our extract+replication server base, e.g. http://server3.local:8080/osm")
    ap.add_argument("--strategy", default="smart", choices=STRATEGIES,
                    help="osmium extract strategy (default: smart — reference-complete)")
    args = ap.parse_args(argv)

    try:
        regions = json.loads(Path(args.regions).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read --regions {args.regions}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(regions, list) or not regions:
        print(f"error: --regions must be a non-empty JSON list, got {type(regions).__name__}",
              file=sys.stderr)
        return 2

    try:
        results = bootstrap(
            source=args.source,
            out=args.out,
            regions=regions,
            base_url=args.base_url,
            strategy=args.strategy,
            on_log=lambda m: print(f"[bootstrap] {m}", file=sys.stderr),
        )
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # machine-readable result on stdout
    json.dump(
        {"source": args.source, "base_url": args.base_url, "strategy": args.strategy,
         "regions": [r.__dict__ for r in results]},
        sys.stdout, indent=2,
    )
    sys.stdout.write("\n")
    print(f"[bootstrap] {len(results)} region(s) OK — round-trip verified", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
