#!/usr/bin/env bash
# Refresh every cache that depends on upstream data.
#
# Runs each tool's --update-all in dependency order:
#   1. download-pbf --update-all    — refresh PBFs whose Geofabrik MD5
#                                     changed. Every downstream cache
#                                     keys on the resulting pbf SHA-256,
#                                     so this drives the rebuild chain.
#   2. clip-pbf --update-all        — re-clip any clip whose source PBF
#                                     changed. Clip outputs live in the
#                                     same pbf/ cache, so later tools
#                                     see them via the regular manifest.
#   3. convert-pbf-geojson --update-all
#   4. convert-pbf-shapefile --update-all
#   5. extract --update-all --extract-all-categories
#   6. build-graphhopper-graph --update-all --all-profiles
#   7. build-valhalla-tiles --update-all
#   8. build-osrm-graph --update-all --all-profiles
#   9. build-vector-tiles --update-all --all-sources
#  10. render-html-maps --update-all — regenerate per-region HTML pages +
#                                      master html/index.html.
#  11. download-gtfs --update-all   — HEAD each recorded agency URL.
#
# Each tool is a no-op when nothing is stale, so it's safe to run this
# as often as you like — CPU time maps to actual work.
#
# Environment overrides:
#   AFL_DATA_ROOT        data root (default /Volumes/afl_data for local)
#   AFL_CACHE_ROOT       cache root (default $AFL_DATA_ROOT/cache)
#   AFL_STAGING_ROOT     staging root (default $AFL_DATA_ROOT/staging)
#   AFL_STORAGE          backend (default local)
#   UPDATE_ALL_SKIP      space-separated list of step names to skip
#                        (e.g. UPDATE_ALL_SKIP="gtfs osrm valhalla")
#   UPDATE_ALL_STOP_ON_FAIL=1   stop at first failure (default: continue)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -t 1 ]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

step_header() { printf '\n%s=== %s ===%s\n' "$BOLD" "$*" "$RESET"; }
step_ok()     { printf '%s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
step_skip()   { printf '%s[skip]%s %s\n' "$YELLOW" "$RESET" "$*"; }
step_fail()   { printf '%s[fail]%s %s\n' "$RED" "$RESET" "$*"; }

SKIP="${UPDATE_ALL_SKIP:-}"
STOP_ON_FAIL="${UPDATE_ALL_STOP_ON_FAIL:-0}"

FAILED_STEPS=()

run_step() {
    local name="$1"
    shift
    for skip_name in $SKIP; do
        if [ "$name" = "$skip_name" ]; then
            step_skip "$name (UPDATE_ALL_SKIP)"
            return 0
        fi
    done
    step_header "$name"
    if "$@"; then
        step_ok "$name"
        return 0
    else
        step_fail "$name (exit $?)"
        FAILED_STEPS+=("$name")
        if [ "$STOP_ON_FAIL" = "1" ]; then
            printf '\n%sUPDATE_ALL_STOP_ON_FAIL=1 — aborting chain%s\n' "$RED" "$RESET"
            exit 1
        fi
        return 1
    fi
}

run_step "pbf"                    "${SCRIPT_DIR}/download-pbf.sh"             --update-all
run_step "clip-pbf"               "${SCRIPT_DIR}/clip-pbf.sh"                 --update-all
run_step "convert-pbf-geojson"    "${SCRIPT_DIR}/convert-pbf-geojson.sh"      --update-all
run_step "convert-pbf-shapefile"  "${SCRIPT_DIR}/convert-pbf-shapefile.sh"    --update-all
run_step "extract"                "${SCRIPT_DIR}/extract.sh"                  --update-all --extract-all-categories
run_step "graphhopper"            "${SCRIPT_DIR}/build-graphhopper-graph.sh"  --update-all --all-profiles
run_step "valhalla"               "${SCRIPT_DIR}/build-valhalla-tiles.sh"     --update-all
run_step "osrm"                   "${SCRIPT_DIR}/build-osrm-graph.sh"         --update-all --all-profiles
run_step "vector-tiles"           "${SCRIPT_DIR}/build-vector-tiles.sh"       --update-all --all-sources
run_step "html-maps"              "${SCRIPT_DIR}/render-html-maps.sh"         --update-all
run_step "gtfs"                   "${SCRIPT_DIR}/download-gtfs.sh"            --update-all

echo
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    printf '%sAll update steps succeeded.%s\n' "$GREEN" "$RESET"
    exit 0
else
    printf '%s%d step(s) failed:%s %s\n' \
        "$RED" "${#FAILED_STEPS[@]}" "$RESET" "${FAILED_STEPS[*]}"
    exit 1
fi
