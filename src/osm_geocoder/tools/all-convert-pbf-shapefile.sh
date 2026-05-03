#!/usr/bin/env bash
# Run convert-pbf-shapefile.sh once per top-level Geofabrik continent.
#
# Converts every cached PBF into multi-layer Shapefile bundles.
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=()

run_per_continent "${SCRIPT_DIR}/convert-pbf-shapefile.sh" "${TOOL_ARGS[@]}" "$@"
