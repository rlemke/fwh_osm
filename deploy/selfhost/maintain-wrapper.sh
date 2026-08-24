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

# Record the OUTCOME where a monitor can find it. A failed maintain run is
# otherwise INVISIBLE: the per-region diff publisher keeps the stream current
# every 6h, so `osm-replicate --check` still reports healthy and the watchdog
# stays quiet while the nightly re-split has been erroring for weeks. The
# stream being fine is exactly what hides it.
# Serialise runs. Nothing stopped the 03:30 timer firing on top of a manual
# catch-up, and two of these re-extracting into the SAME tree would corrupt the
# extracts the whole fleet consumes. mkdir is the atomic primitive here —
# macOS has no flock(1).
LOCKDIR="${HOMEDIR:-$HOME/.facetwork/osm-selfhost}/maintain.lock.d"
mkdir -p "$(dirname "$LOCKDIR")" 2>/dev/null || true
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    _owner="$(cat "$LOCKDIR/pid" 2>/dev/null || true)"
    if [ -n "$_owner" ] && kill -0 "$_owner" 2>/dev/null; then
        echo "=== [$(date '+%F %T')] osm-maintain: run $_owner already in progress — exiting ==="
        exit 0
    fi
    # A crashed run must not block the nightly job forever.
    echo "osm-maintain: clearing stale lock (pid ${_owner:-unknown} not running)" >&2
    rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" || { echo "osm-maintain: cannot take lock" >&2; exit 1; }
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT INT TERM

HEALTH="${HOMEDIR:-$HOME/.facetwork/osm-selfhost}/maintain-health.txt"
_record() {   # _record <rc> [note]
    mkdir -p "$(dirname "$HEALTH")" 2>/dev/null || true
    { echo "at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      echo "rc=$1"
      echo "master=${MASTER:-}"
      echo "out=${WWW:-}"
      [ -n "${2:-}" ] && echo "note=$2"
    } > "$HEALTH" 2>/dev/null || true
}

echo "=== [$(date '+%F %T')] osm-maintain start (base=$BASE_URL out=$WWW) ==="

if [ ! -f "$MASTER" ]; then
    echo "osm-maintain: master PBF missing: $MASTER" >&2
    echo "  Bootstrap it first — see deploy/selfhost/README.md (real planet or a stand-in)." >&2
    _record 2 "master PBF missing"
    exit 2
fi
if [ ! -f "$REGIONS" ]; then
    echo "osm-maintain: regions spec missing: $REGIONS" >&2
    _record 2 "regions spec missing"
    exit 2
fi

# Invoke the tool directly with the pyosmium-capable interpreter (PYTHON), rather
# than planet-maintain.sh, whose venv-activation assumes a fwh_osm-local .venv.
"${PYTHON:-python3}" "$REPO/src/osm_geocoder/tools/planet_maintain.py" \
    --master "$MASTER" \
    --out "$WWW" \
    --regions "$REGIONS" \
    --base-url "$BASE_URL" \
    ${STRATEGY:+--strategy "$STRATEGY"} && rc=0 || rc=$?

_record "$rc"
if [ "$rc" -ne 0 ]; then
    echo "=== [$(date '+%F %T')] osm-maintain FAILED (rc=$rc) ===" >&2
    exit "$rc"
fi
echo "=== [$(date '+%F %T')] osm-maintain done ==="
