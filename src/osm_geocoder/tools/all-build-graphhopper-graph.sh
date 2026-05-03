#!/usr/bin/env bash
# Run build-graphhopper-graph.sh once per top-level Geofabrik continent.
#
# --all-profiles builds one MLD graph per profile per region (car,
# bike, foot, motorcycle, truck, hike, mtb, racingbike).
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=(--all-profiles)

run_per_continent "${SCRIPT_DIR}/build-graphhopper-graph.sh" "${TOOL_ARGS[@]}" "$@"
