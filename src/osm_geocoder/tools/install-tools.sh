#!/usr/bin/env bash
# Install binary dependencies required by the osm-geocoder tool set.
#
# Installs via Homebrew (macOS native + Linux Homebrew both supported).
# On Linux with apt, the equivalent packages are noted in comments for
# users who prefer distro packages.
#
# Idempotent: re-running only installs what's missing.
#
# Required tools:
#   osmium-tool   — PBF parsing, tags-filter, export  (download-pbf, extract,
#                   convert-pbf-geojson, clip-pbf)
#   gdal          — ogr2ogr for shapefile conversion   (convert-pbf-shapefile)
#   openjdk@17    — java runtime for GraphHopper       (build-graphhopper-graph)
#   valhalla      — routing tile builder               (build-valhalla-tiles)
#   tippecanoe    — vector-tile (MBTiles) builder       (build-vector-tiles)
#   pmtiles       — MBTiles → PMTiles converter        (build-vector-tiles)
#   osrm-backend  — OSRM MLD routing-graph builder     (build-osrm-graph)
#
# Plus: GraphHopper's runnable JAR is downloaded directly from GitHub
# releases to ~/.graphhopper/graphhopper-web.jar. (Not available via brew.)

set -euo pipefail

# ---- colors for readability ------------------------------------------------
if [ -t 1 ]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi

log()    { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()     { printf '%s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()   { printf '%s[warn]%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()   { printf '%s[fail]%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ---- prereqs ---------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
    fail "Homebrew is not installed. Install it from https://brew.sh/ then re-run.
On Debian/Ubuntu the apt equivalent packages are:
  osmium-tool gdal-bin openjdk-17-jre-headless
  (valhalla: see https://github.com/valhalla/valhalla — typically built from source)"
fi

# ---- GraphHopper version + download URL -----------------------------------
# Keep in sync with GRAPHHOPPER_VERSION in tools/_lib/graphhopper_build.py.
GRAPHHOPPER_VERSION="${GRAPHHOPPER_VERSION:-8.0}"
GRAPHHOPPER_JAR_URL="https://github.com/graphhopper/graphhopper/releases/download/${GRAPHHOPPER_VERSION}/graphhopper-web-${GRAPHHOPPER_VERSION}.jar"
GRAPHHOPPER_DIR="${GRAPHHOPPER_DIR:-$HOME/.graphhopper}"
GRAPHHOPPER_JAR="${GRAPHHOPPER_JAR:-$GRAPHHOPPER_DIR/graphhopper-web.jar}"

# ---- brew formulae to install ---------------------------------------------
# Order matters slightly: openjdk before anything that pulls it transitively.
FORMULAE=(
    osmium-tool
    gdal
    openjdk@17
    valhalla
    tippecanoe
    pmtiles
    osrm-backend
)

install_formula() {
    local formula="$1"
    if brew list --formula "$formula" >/dev/null 2>&1; then
        ok "already installed: $formula"
        return 0
    fi
    log "installing: $formula"
    if ! brew install "$formula"; then
        warn "brew install $formula failed. You may need to 'brew update' or check for name conflicts."
        return 1
    fi
    ok "installed: $formula"
}

# ---- install brew formulae -------------------------------------------------
log "updating Homebrew formula index (light update)"
brew update --quiet || warn "brew update returned non-zero; continuing"

installed_count=0
failed_count=0
for formula in "${FORMULAE[@]}"; do
    if install_formula "$formula"; then
        installed_count=$((installed_count + 1))
    else
        failed_count=$((failed_count + 1))
    fi
done

# ---- download GraphHopper JAR ---------------------------------------------
log "ensuring GraphHopper ${GRAPHHOPPER_VERSION} JAR at ${GRAPHHOPPER_JAR}"
if [ -f "$GRAPHHOPPER_JAR" ]; then
    size=$(wc -c < "$GRAPHHOPPER_JAR" | tr -d ' ')
    if [ "$size" -gt 1000000 ]; then
        ok "already installed: GraphHopper JAR (${size} bytes)"
    else
        warn "existing JAR suspiciously small (${size} bytes), re-downloading"
        rm -f "$GRAPHHOPPER_JAR"
    fi
fi
if [ ! -f "$GRAPHHOPPER_JAR" ]; then
    mkdir -p "$GRAPHHOPPER_DIR"
    log "downloading from $GRAPHHOPPER_JAR_URL"
    if curl -L --fail --progress-bar -o "$GRAPHHOPPER_JAR" "$GRAPHHOPPER_JAR_URL"; then
        ok "downloaded: $GRAPHHOPPER_JAR"
    else
        rm -f "$GRAPHHOPPER_JAR"
        warn "GraphHopper JAR download failed. You can retry later or pass --jar to build-graphhopper-graph."
        failed_count=$((failed_count + 1))
    fi
fi

# ---- verification ----------------------------------------------------------
log "verifying installed tools"

verify() {
    local bin="$1" desc="$2"
    if command -v "$bin" >/dev/null 2>&1; then
        local version
        version=$("$bin" --version 2>&1 | head -1 || true)
        ok "$desc: $(command -v "$bin") — $version"
    else
        warn "$desc: $bin not on PATH"
        failed_count=$((failed_count + 1))
    fi
}

verify osmium "osmium-tool"
verify ogr2ogr "GDAL (ogr2ogr)"
verify java "Java runtime"
verify valhalla_build_config "Valhalla build_config"
verify valhalla_build_tiles "Valhalla build_tiles"
verify tippecanoe "tippecanoe"
verify pmtiles "pmtiles (MBTiles → PMTiles converter)"
verify osrm-extract "OSRM extract"
verify osrm-partition "OSRM partition"
verify osrm-customize "OSRM customize"

if [ -f "$GRAPHHOPPER_JAR" ]; then
    ok "GraphHopper JAR: $GRAPHHOPPER_JAR"
else
    warn "GraphHopper JAR: missing at $GRAPHHOPPER_JAR"
fi

# ---- summary ---------------------------------------------------------------
echo
if [ "$failed_count" -eq 0 ]; then
    log "${GREEN}all tools installed successfully${RESET}"
    echo
    echo "  $GREEN✓$RESET osmium-tool             — download-pbf, extract, convert-pbf-geojson"
    echo "  $GREEN✓$RESET GDAL / ogr2ogr          — convert-pbf-shapefile"
    echo "  $GREEN✓$RESET Java + GraphHopper JAR  — build-graphhopper-graph"
    echo "  $GREEN✓$RESET Valhalla                — build-valhalla-tiles"
    echo "  $GREEN✓$RESET tippecanoe              — build-vector-tiles"
    echo "  $GREEN✓$RESET pmtiles                 — MBTiles → PMTiles conversion"
    echo "  $GREEN✓$RESET osrm-backend            — build-osrm-graph"
    echo
    echo "Useful environment variables:"
    echo "  AFL_DATA_ROOT            data root (default /Volumes/afl_data)"
    echo "  AFL_CACHE_ROOT           cache root (default \$AFL_DATA_ROOT/cache)"
    echo "  AFL_STAGING_ROOT         staging root (default \$AFL_DATA_ROOT/staging)"
    echo "  GRAPHHOPPER_JAR          jar path (currently $GRAPHHOPPER_JAR)"
    echo "  GRAPHHOPPER_XMX          JVM heap (default 4g)"
    echo
    exit 0
else
    warn "$failed_count tool(s) did not install cleanly — see messages above"
    exit 1
fi
