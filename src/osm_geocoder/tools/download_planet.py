"""Download and/or delta-update the OSM planet (master for self-hosted extracts).

Thin CLI over ``_osm_tools.planet_fetch``. Fetches ``planet-latest.osm.pbf`` from a
planet mirror (resumable, md5-verified) and, with ``--update``, applies replication
diffs to bring it current — the master that ``planet_bootstrap`` / ``planet_maintain``
split into regional extracts.

Usage::

    python download_planet.py --dest /data/planet-latest.osm.pbf          # download
    python download_planet.py --dest /data/planet-latest.osm.pbf --update  # + delta-update
    python download_planet.py --dest /data/planet-latest.osm.pbf --update-only  # skip download

Mirror + replication are overridable via FW_PLANET_MIRROR / FW_PLANET_REPLICATION.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.planet_fetch import PlanetError, fetch_planet, update_planet  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download / delta-update the OSM planet.")
    ap.add_argument("--dest", required=True, help="planet PBF path")
    ap.add_argument("--no-verify", action="store_true", help="skip md5 verification")
    ap.add_argument("--force", action="store_true", help="re-download even if md5 matches")
    ap.add_argument("--update", action="store_true", help="apply replication diffs after download")
    ap.add_argument("--update-only", action="store_true", help="skip download, only delta-update")
    ap.add_argument("--max-diff-mb", type=int, default=4096, help="cap per-run replication catch-up")
    args = ap.parse_args(argv)

    log = lambda m: print(f"[planet] {m}", file=sys.stderr)
    out: dict = {"dest": args.dest}
    try:
        if not args.update_only:
            f = fetch_planet(args.dest, verify=not args.no_verify, force=args.force, on_log=log)
            out["fetch"] = {"size_bytes": f.size_bytes, "md5": f.md5, "was_cached": f.was_cached}
        if args.update or args.update_only:
            u = update_planet(args.dest, max_diff_mb=args.max_diff_mb, on_log=log)
            out["update"] = {"status": u.status, "advanced": u.advanced,
                             "old_timestamp": u.old_timestamp, "new_sequence": u.new_sequence}
    except PlanetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
