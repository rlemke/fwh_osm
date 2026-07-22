#!/usr/bin/env bash
# Nightly maintenance wrapper (Strategy A, Phase 2): advance the master planet and
# re-extract all regions into the served tree. Invoked by the launchd timer
# com.facetwork.osm-maintain; also runnable by hand for an on-demand refresh:
#   deploy/selfhost/maintain-wrapper.sh [config.env]
set -euo pipefail

# launchd runs with a minimal PATH; the tool shells out to `osmium`, so make sure
# Homebrew's bin (Apple Silicon + Intel layouts) is on PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

CONFIG="${1:-$HOME/.facetwork/osm-selfhost/config.env}"
[ -f "$CONFIG" ] || { echo "osm-maintain: no config at $CONFIG" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG"
: "${REPO:?}" "${MASTER:?}" "${REGIONS:?}" "${WWW:?}" "${BASE_URL:?}"

echo "=== [$(date '+%F %T')] osm-maintain start (base=$BASE_URL out=$WWW) ==="

if [ ! -f "$MASTER" ]; then
    echo "osm-maintain: master PBF missing: $MASTER" >&2
    echo "  Bootstrap it first — see deploy/selfhost/README.md (real planet or a stand-in)." >&2
    exit 2
fi
if [ ! -f "$REGIONS" ]; then
    echo "osm-maintain: regions spec missing: $REGIONS" >&2
    exit 2
fi

# Invoke the tool directly with the pyosmium-capable interpreter (PYTHON), rather
# than planet-maintain.sh, whose venv-activation assumes a fwh_osm-local .venv.
"${PYTHON:-python3}" "$REPO/src/osm_geocoder/tools/planet_maintain.py" \
    --master "$MASTER" \
    --out "$WWW" \
    --regions "$REGIONS" \
    --base-url "$BASE_URL" \
    ${STRATEGY:+--strategy "$STRATEGY"}

echo "=== [$(date '+%F %T')] osm-maintain done ==="
