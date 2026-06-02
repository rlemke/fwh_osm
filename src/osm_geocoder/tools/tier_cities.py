"""Tier a populated-places GeoJSON into disjoint zoom bands by population.

Thin CLI wrapper around ``_osm_tools.tier_cities.tier_cities``. The FFL
event facet ``osm.Cities.TierCitiesByPopulation`` calls the same library
function, so the CLI and the workflow share one code path.

Default tiers (highest population first):

  zoom 3   pop >= 5,000,000
  zoom 6   pop >= 1,000,000
  zoom 8   pop >=   500,000
  zoom 10  pop >=    10,000

Each input feature lands in the highest tier it qualifies for; features
below the lowest threshold are dropped. The output GeoJSON carries
``zoom``, ``tier_min_population``, ``name``, ``country``, ``place``,
``population``, ``lon``, ``lat``, and ``bbox`` for every feature.

Usage::

    python tier_cities.py --input cities.geojson --output tiered.geojson
    python tier_cities.py --input cities.geojson --output tiered.geojson \\
        --tiers "3:5000000,6:1000000,8:500000,10:10000"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _osm_tools.tier_cities import (  # noqa: E402
    DEFAULT_TIERS,
    parse_tier_spec,
    tier_cities,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tier a populated-places GeoJSON into disjoint zoom bands by population.",
    )
    parser.add_argument("--input", required=True, help="Input GeoJSON (FeatureCollection).")
    parser.add_argument("--output", required=True, help="Output GeoJSON path.")
    parser.add_argument(
        "--tiers",
        default=",".join(f"{t.zoom}:{t.min_population}" for t in DEFAULT_TIERS),
        help=(
            "Comma-separated tier spec 'zoom:min_population,zoom:min_population,...'. "
            "Defaults to the 4-tier scheme above."
        ),
    )
    parser.add_argument("--log-level", default="INFO", help="Python logging level (default INFO).")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        tiers = parse_tier_spec(args.tiers)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = tier_cities(args.input, args.output, tiers=tiers)
    except FileNotFoundError as exc:
        print(f"error: input not found: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
        return 2

    # stdout: structured summary, safe to pipe / parse in tests
    json.dump(
        {
            "output_path": result.output_path,
            "total_count": result.total_count,
            "tier_counts": {str(z): n for z, n in result.tier_counts.items()},
            "format": result.format,
            "extraction_date": result.extraction_date,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
