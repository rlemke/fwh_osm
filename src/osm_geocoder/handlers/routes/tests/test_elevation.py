#!/usr/bin/env python3
"""
Test script for elevation-based route filtering.

This script demonstrates the elevation handlers by:
1. Enriching sample trails with elevation data from Open-Elevation API
2. Filtering trails by elevation threshold (2000 ft)
3. Showing the results
"""

import json
import os
import sys

# Add the handlers to path — go up 3 levels from tests/mocked/py/ to osm-geocoder/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from osm_geocoder.handlers.routes.elevation_handlers import (
    handle_enrich_with_elevation,
    handle_filter_by_max_elevation,
)


def main():
    # Path to sample data
    sample_path = os.path.join(os.path.dirname(__file__), "..", "data", "sample_trails.geojson")

    print("=" * 60)
    print("Elevation-Based Route Filtering Test")
    print("=" * 60)

    # Load and show sample data
    with open(sample_path) as f:
        data = json.load(f)

    print(f"\nInput: {len(data['features'])} trails")
    for feat in data["features"]:
        name = feat["properties"].get("name", "Unnamed")
        coords = feat["geometry"]["coordinates"]
        print(f"  - {name} ({len(coords)} points)")

    # Step 1: Enrich with elevation data
    print("\n" + "-" * 60)
    print("Step 1: Enriching trails with elevation data (SRTM)...")
    print("  (Calling Open-Elevation API - may take a moment)")
    print("-" * 60)

    result = handle_enrich_with_elevation(
        {
            "input_path": sample_path,
            "dem_source": "srtm",
        }
    )

    enriched_result = result["result"]
    print(f"\nEnriched {enriched_result['matched_count']} trails")
    print(f"Output: {enriched_result['output_path']}")

    # Show elevation stats for each trail
    print("\nElevation Statistics:")
    for route in enriched_result.get("routes", []):
        name = route.get("name", "Unnamed")
        stats = route.get("stats", {})
        print(f"\n  {name}:")
        print(f"    Min: {stats.get('min_elevation_ft', 0):.0f} ft")
        print(f"    Max: {stats.get('max_elevation_ft', 0):.0f} ft")
        print(f"    Gain: {stats.get('elevation_gain_ft', 0):.0f} ft")
        print(f"    Avg: {stats.get('avg_elevation_ft', 0):.0f} ft")

    # Step 2: Filter by elevation threshold (2000 ft)
    print("\n" + "-" * 60)
    print("Step 2: Filtering trails with max elevation >= 2000 ft...")
    print("-" * 60)

    filter_result = handle_filter_by_max_elevation(
        {
            "input_path": enriched_result["output_path"],
            "min_max_elevation_ft": 2000,
        }
    )

    filtered = filter_result["result"]
    print(f"\nFound {filtered['matched_count']} trails above 2000 ft")
    print(f"Filter: {filtered['filter_applied']}")
    print(f"Output: {filtered['output_path']}")

    # Show matching trails
    if filtered["matched_count"] > 0:
        print("\nHigh Elevation Trails:")
        with open(filtered["output_path"]) as f:
            filtered_data = json.load(f)

        for feat in filtered_data["features"]:
            props = feat["properties"]
            name = props.get("name", "Unnamed")
            stats = props.get("elevation_stats", {})
            max_elev = stats.get("max_elevation_ft", 0)
            print(f"  - {name}: max {max_elev:.0f} ft")
    else:
        print("\nNo trails found above 2000 ft threshold")

    print("\n" + "=" * 60)
    print("Test Complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
