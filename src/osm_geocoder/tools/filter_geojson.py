#!/usr/bin/env python3
"""filter-geojson — filter a GeoJSON FeatureCollection by an arbitrary Python
script (a boolean expression over ``props``/``feature``, or a ``def keep``).

Shares the exact implementation with the FFL ``osm.Filters.ByScript`` handler:
both call ``_osm_tools.geojson_filter.filter_geojson``. Input/output may be local
paths or ``s3://``/``hdfs://`` URIs.

Examples:
  filter-geojson chargers.geojson tesla.geojson \\
    --script 'props.get("amenity")=="charging_station" and "tesla" in str(props.get("operator","")).lower()'
  filter-geojson in.geojson out.geojson --script-file keep.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.geojson_filter import FilterError, filter_geojson  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Input GeoJSON path/URI")
    p.add_argument("output", help="Output GeoJSON path/URI for the kept features")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--script", help="Python boolean expression or `def keep(feature)` body")
    g.add_argument("--script-file", help="File containing the filter script")
    a = p.parse_args()
    script = a.script if a.script is not None else Path(a.script_file).read_text(encoding="utf-8")
    try:
        r = filter_geojson(a.input, script, a.output)
    except FilterError as exc:
        print(f"filter error: {exc}", file=sys.stderr)
        return 2
    msg = f"kept {r.kept}/{r.total} -> {r.output_path}"
    if r.errors:
        msg += f" ({r.errors} features errored in predicate)"
    print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
