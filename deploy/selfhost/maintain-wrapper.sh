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

# The re-split runs as the osm.planet.RefreshContinents WORKFLOW, not as osmium
# called from here. Until 2026-10-01 this wrapper ran planet_maintain.py
# directly: an ~11 GB osmium the runtime could not see, sharing the planet host
# with country cuts that sized themselves against the whole machine. The global
# OOM killer fired 14 times in 30 minutes and finally killed this job. As a
# workflow the same work (advance the planet, re-cut the continents into the
# served tree, publish them) is placed, retried, cancellable and on the
# dashboard like everything else.
#
# This still WAITS for the outcome, because maintain-health.txt is what the
# watchdog reads: recording "submitted" as success would hide a failed run the
# same way the stream hid one before this file existed.
FW_BIN="${FW_BIN:-$HOME/facetwork/fw}"
FFL="${FFL:-$REPO/src/osm_geocoder/handlers/planet/ffl/osmplanet.ffl}"
WAIT_H="${FW_OSM_MAINTAIN_WAIT_HOURS:-10}"
if [ ! -x "$FW_BIN" ]; then
    echo "osm-maintain: fw not found at $FW_BIN (set FW_BIN)" >&2
    _record 2 "fw not found"
    exit 2
fi

# One refresh at a time: RefreshChain does this same work as its first tier.
# Same query as osm-admin-regen's guard: the field is NESTED (workflow.name) --
# a flat workflow_name matches nothing and the guard would silently never fire.
inflight="$("${PYTHON:-python3}" - <<'PYEOF' 2>/dev/null
import os, sys
try:
    import pymongo
    db = pymongo.MongoClient(os.environ.get("FW_MONGODB_URL") or "mongodb://localhost:27017",
                             serverSelectionTimeoutMS=8000).get_database(
        os.environ.get("FW_MONGODB_DATABASE") or "facetwork")
    r = db.runners.find_one({"workflow.name": {"$in": ["osm.planet.RefreshChain",
                                                      "osm.planet.RefreshContinents"]},
                             "state": {"$nin": ["completed", "failed", "terminated"]}})
except Exception:
    print("?")
    sys.exit(0)
if r:
    print(f"{r['workflow']['name']} {r.get('uuid', '')}")
PYEOF
)"
if [ "$inflight" = "?" ]; then
    echo "[maintain] WARNING: could not check for an in-flight refresh (no pymongo / Mongo unreachable) - submitting anyway"
elif [ -n "$inflight" ]; then
    echo "[maintain] skipping: $inflight is already in flight and does this work"
    _record 0 "skipped: in flight $inflight"
    exit 0
fi

so="$("$FW_BIN" ffl run --primary "$FFL" --workflow osm.planet.RefreshContinents \
        --inputs '{"bucket": "'"${BUCKET:-osm-extracts}"'"}' 2>&1)" && rc=0 || rc=$?
runner="$(printf '%s' "$so" | grep -oE '[0-9a-f]{8}-[0-9a-f-]{27}' | head -1)"
if [ "$rc" -ne 0 ] || [ -z "$runner" ]; then
    printf '%s\n' "$so" >&2
    _record 1 "submit failed"
    echo "=== [$(date '+%F %T')] osm-maintain FAILED (submit) ===" >&2
    exit 1
fi
echo "[maintain] submitted osm.planet.RefreshContinents $runner"

deadline=$(( $(date +%s) + WAIT_H * 3600 ))
state=""
while :; do
    state="$("$FW_BIN" maint workflow-stats "$runner" --json 2>/dev/null \
        | "${PYTHON:-python3}" -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)"
    case "$state" in completed|failed|cancelled|terminated) break ;; esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "[maintain] $runner still ${state:-unknown} after ${WAIT_H}h" >&2
        _record 124 "still running after ${WAIT_H}h: $runner"
        exit 124
    fi
    sleep 60
done
echo "[maintain] $runner $state"
[ "$state" = completed ] && rc=0 || rc=1
_record "$rc" "runner=$runner state=$state"
if [ "$rc" -ne 0 ]; then
    echo "=== [$(date '+%F %T')] osm-maintain FAILED ($state) ===" >&2
    exit "$rc"
fi
echo "=== [$(date '+%F %T')] osm-maintain done ==="

# The re-split just rewrote every served continent, so the store status report is
# now describing the previous tree. Refresh it, into the tree itself so it sits
# next to the extracts it describes.
#
# Best-effort by design: this leg must NOT be able to turn a successful re-split
# into a failed one. A missing `fw` (this wrapper also runs where the framework
# repo is not checked out) is a skip, not an error.
if [ -x "$FW_BIN" ]; then
    if "$FW_BIN" svc osm-report --publish --tree-dir "$WWW" >/dev/null 2>&1; then
        echo "    store status report refreshed"
    else
        echo "    store status report NOT refreshed — the re-split itself was fine"
    fi
fi
