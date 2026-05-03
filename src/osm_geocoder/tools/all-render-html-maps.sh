#!/usr/bin/env bash
# Run render-html-maps.sh once per top-level Geofabrik continent.
#
# Emits a MapLibre HTML page per region from the cached PMTiles.
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=()

run_per_continent "${SCRIPT_DIR}/render-html-maps.sh" "${TOOL_ARGS[@]}" "$@"
