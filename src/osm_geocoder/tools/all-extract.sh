#!/usr/bin/env bash
# Run extract.sh once per top-level Geofabrik continent, pre-filtering
# every cached PBF into per-category GeoJSONSeq files (water, parks,
# roads, buildings, etc.).
#
# --extract-all-categories runs every category per region. Extra flags
# the user appends at the command line are forwarded on top.
#
# Per-continent failures are absorbed into a summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_continents.sh"

TOOL_ARGS=(--extract-all-categories)

run_per_continent "${SCRIPT_DIR}/extract.sh" "${TOOL_ARGS[@]}" "$@"
