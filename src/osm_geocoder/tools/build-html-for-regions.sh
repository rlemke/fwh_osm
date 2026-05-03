#!/usr/bin/env bash
# Run the OSM tool chain for specific region(s) and produce HTML map pages.
#
# Pipeline (each stage is a no-op when its cache is already current):
#   1. download-pbf           — fetch Geofabrik PBFs
#   2. convert-pbf-geojson    — PBF → GeoJSONSeq
#   3. extract                — per-category GeoJSONSeq (all categories)
#   4. build-vector-tiles     — GeoJSONSeq → PMTiles (all sources)
#   5. render-html-maps       — PMTiles → MapLibre HTML viewer per region
#
# Region selection is passed through to every stage as-is, so any of the
# region flags supported by the underlying tools work:
#   positional:       europe/liechtenstein europe/germany/berlin
#   from file:        --regions-file regions.txt
#   all / prefix:     --all   |   --all-under europe/germany
#
# Usage:
#   ./build-html-for-regions.sh europe/liechtenstein
#   ./build-html-for-regions.sh europe/germany/berlin europe/germany/munich
#   ./build-html-for-regions.sh --regions-file my-regions.txt
#   ./build-html-for-regions.sh --all-under europe/germany --jobs 4
#
# Script flags (consumed here, not forwarded verbatim):
#   --skip "names"        space-separated stages to skip
#                         (pbf, geojson, extract, vector-tiles, html)
#   --stop-on-fail        abort at first failed stage (default: continue)
#   --jobs N              forwarded to geojson/extract/vector-tiles/html
#                         only — download-pbf is sequential by design
#                         (Geofabrik rate-limits per IP)
#   --vector-tiles-timeout SECS
#                         forwarded to build-vector-tiles as --timeout SECS
#                         (per-region tippecanoe timeout). The other tools
#                         don't accept --timeout.
#   --max-zoom N          forwarded to build-vector-tiles as --max-zoom N
#                         (default 14; lower is dramatically faster — at
#                         z10 tippecanoe runtime is ~16× shorter). The
#                         other tools don't accept --max-zoom.
#   --min-zoom N          forwarded to build-vector-tiles as --min-zoom N.
#   -h | --help           show this help
#
# All other flags and positional args are forwarded to each tool verbatim,
# so --force, --dry-run, --regions-file, --all, --all-under,
# --include-parents, --backend, etc. all work.

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

print_help() {
    # Print every leading-comment line at the top of this file (between
    # the shebang and the first non-comment statement). Avoids drift
    # when help text grows.
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' \
        "${BASH_SOURCE[0]}"
}

SKIP=""
STOP_ON_FAIL=0
FORWARD_ARGS=()       # sent to every stage
JOBS_ARGS=()          # sent only to stages that accept --jobs
VECTOR_TILES_TIMEOUT_ARGS=()   # sent only to build-vector-tiles
VECTOR_TILES_ZOOM_ARGS=()      # sent only to build-vector-tiles

while [ $# -gt 0 ]; do
    case "$1" in
        --skip)
            [ $# -ge 2 ] || { echo "--skip requires a value" >&2; exit 2; }
            SKIP="$2"
            shift 2
            ;;
        --skip=*)
            SKIP="${1#--skip=}"
            shift
            ;;
        --stop-on-fail)
            STOP_ON_FAIL=1
            shift
            ;;
        --jobs)
            [ $# -ge 2 ] || { echo "--jobs requires a value" >&2; exit 2; }
            JOBS_ARGS=(--jobs "$2")
            shift 2
            ;;
        --jobs=*)
            JOBS_ARGS=(--jobs "${1#--jobs=}")
            shift
            ;;
        --vector-tiles-timeout)
            [ $# -ge 2 ] || { echo "--vector-tiles-timeout requires a value" >&2; exit 2; }
            VECTOR_TILES_TIMEOUT_ARGS=(--timeout "$2")
            shift 2
            ;;
        --vector-tiles-timeout=*)
            VECTOR_TILES_TIMEOUT_ARGS=(--timeout "${1#--vector-tiles-timeout=}")
            shift
            ;;
        --max-zoom)
            [ $# -ge 2 ] || { echo "--max-zoom requires a value" >&2; exit 2; }
            VECTOR_TILES_ZOOM_ARGS+=(--max-zoom "$2")
            shift 2
            ;;
        --max-zoom=*)
            VECTOR_TILES_ZOOM_ARGS+=(--max-zoom "${1#--max-zoom=}")
            shift
            ;;
        --min-zoom)
            [ $# -ge 2 ] || { echo "--min-zoom requires a value" >&2; exit 2; }
            VECTOR_TILES_ZOOM_ARGS+=(--min-zoom "$2")
            shift 2
            ;;
        --min-zoom=*)
            VECTOR_TILES_ZOOM_ARGS+=(--min-zoom "${1#--min-zoom=}")
            shift
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        --)
            shift
            FORWARD_ARGS+=("$@")
            break
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

# Require some form of region selection in the forwarded args.
if [ ${#FORWARD_ARGS[@]} -eq 0 ]; then
    echo "error: no regions specified" >&2
    echo "       pass region paths as positional args, or use" >&2
    echo "       --regions-file / --all / --all-under PREFIX" >&2
    echo "       (see --help for examples)" >&2
    exit 2
fi

FAILED_STEPS=()

run_step() {
    local name="$1"
    shift
    for skip_name in $SKIP; do
        if [ "$name" = "$skip_name" ]; then
            step_skip "$name (--skip)"
            return 0
        fi
    done
    step_header "$name"
    if "$@"; then
        step_ok "$name"
        return 0
    else
        local rc=$?
        step_fail "$name (exit $rc)"
        FAILED_STEPS+=("$name")
        if [ "$STOP_ON_FAIL" = "1" ]; then
            printf '\n%s--stop-on-fail set — aborting chain%s\n' "$RED" "$RESET"
            exit 1
        fi
        return $rc
    fi
}

run_step "pbf"           "${SCRIPT_DIR}/download-pbf.sh"          "${FORWARD_ARGS[@]}"
run_step "geojson"       "${SCRIPT_DIR}/convert-pbf-geojson.sh"   "${JOBS_ARGS[@]}" "${FORWARD_ARGS[@]}"
run_step "extract"       "${SCRIPT_DIR}/extract.sh"               --extract-all-categories "${JOBS_ARGS[@]}" "${FORWARD_ARGS[@]}"
run_step "vector-tiles"  "${SCRIPT_DIR}/build-vector-tiles.sh"    --all-sources "${JOBS_ARGS[@]}" "${VECTOR_TILES_TIMEOUT_ARGS[@]}" "${VECTOR_TILES_ZOOM_ARGS[@]}" "${FORWARD_ARGS[@]}"
run_step "html"          "${SCRIPT_DIR}/render-html-maps.sh"      "${JOBS_ARGS[@]}" "${FORWARD_ARGS[@]}"

echo
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
    printf '%sHTML pipeline complete.%s\n' "$GREEN" "$RESET"
    printf 'Output: %s$AFL_CACHE_ROOT/osm/html/<region>-latest/index.html%s\n' "$BOLD" "$RESET"
    printf 'Serve:  python -m http.server --directory "$AFL_CACHE_ROOT/osm" 8000\n'
    exit 0
else
    printf '%s%d stage(s) failed:%s %s\n' \
        "$RED" "${#FAILED_STEPS[@]}" "$RESET" "${FAILED_STEPS[*]}"
    exit 1
fi
