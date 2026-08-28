#!/usr/bin/env python3
"""CLI: state of the self-hosted OSM region extracts, and of Overpass.

    osm-extract-state.py                      # header survey, human readable
    osm-extract-state.py --json               # machine readable
    osm-extract-state.py --html /tmp/out      # write the HTML report
    osm-extract-state.py --local-dir DIR --count-features   # EXPENSIVE

⚠️ Exit codes are load-bearing, matching `fw maint dead-letters` and
`fw svc osm-watchdog`:
    0  every expected region present and current
    1  a region is MISSING or STALE  -> a real problem
    2  could not verify (the tree was unreachable)
Only 1 is an alarm. Alarming merely because we are offline would train the
reader to ignore the alarm.

⚠️ --count-features reads EVERY BYTE of every extract (41.6 s for 1.7 GB
measured, ~710 s for 40 GB). The default survey reads headers only: locally
0.26 s per file, and REMOTELY just a 64 KB Range request per file, so the whole
100 GB set is surveyed in seconds without downloading anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _osm_tools import extract_inventory as inv  # noqa: E402

_EXIT = {"ok": 0, "problem": 1, "unverified": 2}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local-dir", default="", help="also survey extracts on this disk")
    ap.add_argument("--count-features", action="store_true",
                    help="EXPENSIVE: read every byte to count nodes/ways/relations")
    ap.add_argument("--no-object-store", action="store_true")
    ap.add_argument("--no-overpass", action="store_true")
    ap.add_argument("--stale-after-hours", type=float, default=48.0)
    ap.add_argument("--html", default="", metavar="DIR", help="write HTML+JSON report here")
    ap.add_argument("--json", action="store_true", help="print the raw report")
    a = ap.parse_args()

    if a.count_features and not a.local_dir:
        print("--count-features needs --local-dir: counting means reading whole files, "
              "and remote files are never downloaded implicitly.", file=sys.stderr)
        return 2

    rep = inv.build_report(count_features=a.count_features, local_dir=a.local_dir,
                           include_object_store=not a.no_object_store,
                           include_overpass=not a.no_overpass,
                           stale_after_hours=a.stale_after_hours)
    if a.json:
        json.dump(rep, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        s = rep["summary"]
        print(f"OSM extract state: {rep['status'].upper()}   "
              f"{s['present']}/{s['expected']} present   "
              f"Overpass {s['overpass_usable']}")
        print(f"\n{'region':22s} {'currency':>12s} {'size':>9s} {'seq':>6s}")
        for region, v in sorted(rep["tree"]["regions"].items()):
            if not v.get("present"):
                cur = "MISSING"
            elif v.get("age_hours") is None:
                cur = "no header"
            else:
                cur = f"{v['age_hours']:.1f}h"
            size = f"{v['size_bytes'] / 1e9:.1f} GB" if v.get("size_bytes") else "-"
            line = (f"{region:22s} {cur:>12s} {size:>9s} "
                    f"{str(v.get('replication_sequence') or '-'):>6s}")
            loc = (rep.get("local") or {}).get("regions", {}).get(region) or {}
            if isinstance(loc.get("node_count"), int):
                line += (f"   nodes={loc['node_count']:,} ways={loc['way_count']:,} "
                         f"relations={loc['relation_count']:,}")
            print(line)
        if s["missing"]:
            print(f"\nMISSING: {', '.join(s['missing'])}")
        if s["stale"]:
            print(f"STALE (>{a.stale_after_hours:g}h): {', '.join(s['stale'])}")
        print("\nOverpass mirrors:")
        for m in rep["overpass"].get("mirrors", []):
            lag = (f"{m['data_lag_hours']:.2f}h"
                   if isinstance(m.get("data_lag_hours"), (int, float)) else "-")
            print(f"  {'usable' if m.get('usable') else 'DOWN  '} {m['endpoint']:48s} "
                  f"lag={lag:>8s} slots={m.get('slots_available', '-')}")
        if not a.count_features:
            print("\n(header-only survey: feature counts not measured; "
                  "--local-dir --count-features reads whole files)")
    if a.html:
        html, js = inv.render_report(rep, dest=a.html)
        print(f"\nreport: {html}\n        {js}", file=sys.stderr)
    return _EXIT.get(rep["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
