#!/usr/bin/env bash
# Run convert-pbf-geojson.sh once per top-level Geofabrik continent.
#
# Converts every cached PBF into a GeoJSONSeq file.
#
# Per-continent failures are absorbed into a summary at the end.
# Sourced helper (_continents.sh) owns the iteration + logging so
# every all-*.sh in this directory stays near-identical.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

# Extra flags on top of --all-under <continent> --include-parents.
# Callers of this script can append more flags at invocation time
# and they'll be forwarded through too.
TOOL_ARGS=()

run_per_continent "${SCRIPT_DIR}/convert-pbf-geojson.sh" "${TOOL_ARGS[@]}" "$@"
