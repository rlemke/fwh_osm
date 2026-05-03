#!/usr/bin/env bash
# Run build-vector-tiles.sh once per top-level Geofabrik continent.
#
# --all-sources tiles every cached extract category (water, parks,
# roads, …) plus the whole-region geojson for each continent's
# regions. Output is PMTiles + sidecars.
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=(--all-sources)

run_per_continent "${SCRIPT_DIR}/build-vector-tiles.sh" "${TOOL_ARGS[@]}" "$@"
