#!/usr/bin/env bash
# Run build-valhalla-tiles.sh once per top-level Geofabrik continent.
#
# Valhalla has no build-time profile axis — one tileset per region
# serves every profile at query time.
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=()

run_per_continent "${SCRIPT_DIR}/build-valhalla-tiles.sh" "${TOOL_ARGS[@]}" "$@"
