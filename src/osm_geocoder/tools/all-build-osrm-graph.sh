#!/usr/bin/env bash
# Run build-osrm-graph.sh once per top-level Geofabrik continent.
#
# --all-profiles builds one MLD graph per profile per region
# (car, bicycle, foot).
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=(--all-profiles)

run_per_continent "${SCRIPT_DIR}/build-osrm-graph.sh" "${TOOL_ARGS[@]}" "$@"
