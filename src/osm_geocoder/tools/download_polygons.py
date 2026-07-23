"""Download OSM boundary polygons (osmfr) for planet extraction.

Thin CLI over ``_osm_tools.polygon_fetch``. Downloads continent- and/or
country-level ``.poly`` files from OSM France into a directory and emits a
``regions`` JSON (``[{key, poly}]``) ready to feed ``planet_bootstrap`` /
``planet_maintain``.

Usage::

    python download_polygons.py --dest /data/polys --scope all       # continents + ~199 countries
    python download_polygons.py --dest /data/polys --scope countries # countries only
    python download_polygons.py --dest /data/polys --scope continents --regions-out regions.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.polygon_fetch import SCOPES, PolygonError, fetch_polygons  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download osmfr boundary polygons.")
    ap.add_argument("--dest", required=True, help="directory for the .poly files")
    ap.add_argument("--scope", default="all", choices=SCOPES)
    ap.add_argument("--regions-out", help="write the [{key,poly}] regions JSON here")
    args = ap.parse_args(argv)

    try:
        regions = fetch_polygons(args.dest, scope=args.scope,
                                 on_log=lambda m: print(f"[polygons] {m}", file=sys.stderr))
    except PolygonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = [{"key": r.key, "poly": r.poly} for r in regions]
    if args.regions_out:
        Path(args.regions_out).write_text(json.dumps(payload, indent=2))
        print(f"[polygons] wrote {len(payload)} regions -> {args.regions_out}", file=sys.stderr)
    json.dump({"scope": args.scope, "count": len(payload), "regions": payload}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
