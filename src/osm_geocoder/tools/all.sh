#!/usr/bin/env bash
# Whole-planet OSM pipeline from scratch.
#
# Two phases:
#   1. download-pbf per top-level Geofabrik continent (via the shared
#      _continents.sh helper). This is the bandwidth-heavy phase —
#      expect hours and tens of GB per continent.
#   2. update-all.sh on the populated pbf cache, which sweeps every
#      downstream sub-tool's --update-all in dependency order
#      (convert-pbf-geojson → extract → build-vector-tiles → routing
#      graphs → render-html-maps → download-gtfs).
#
# If you want continent-by-continent progress on a single downstream
# stage instead, run the matching per-stage wrapper directly:
#
#   ./all-convert-pbf-geojson.sh
#   ./all-extract.sh
#   ./all-build-vector-tiles.sh
#   ./all-render-html-maps.sh
#   ./all-build-graphhopper-graph.sh
#   ./all-build-valhalla-tiles.sh
#   ./all-build-osrm-graph.sh
#   ./all-convert-pbf-shapefile.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

# --delay 2 so the PBF fetch loop doesn't hammer Geofabrik. Extra
# flags the user passes on the command line get forwarded through.
run_per_continent "${SCRIPT_DIR}/download-pbf.sh" --delay 2 "$@"

# Phase 2 — every downstream stage is a no-op for anything already
# current, so this is safe even if some continents failed above.
echo
printf '\n=== running update-all on every cached pbf ===\n'
"${SCRIPT_DIR}/update-all.sh"
