#!/usr/bin/env bash
# Install the self-hosted OSM extract server + nightly maintain launchd agents
# (Strategy A). Idempotent: re-run after editing config.env or pulling new code.
#
#   deploy/selfhost/install.sh
#
# On first run it seeds ~/.facetwork/osm-selfhost/config.env from the example and
# exits so you can edit it. Re-run to install the two launchd agents:
#   com.facetwork.osm-extract-server  (KeepAlive static server over WWW)
#   com.facetwork.osm-maintain        (nightly advance-master + re-extract)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMEDIR="$HOME/.facetwork/osm-selfhost"
CONFIG="$HOMEDIR/config.env"
LA="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOMEDIR/www" "$LA"

if [ ! -f "$CONFIG" ]; then
    cp "$HERE/config.env.example" "$CONFIG"
    echo "seeded $CONFIG — edit it (MASTER, REGIONS, BASE_URL, PORT), then re-run install.sh"
    exit 0
fi

# shellcheck disable=SC1090
source "$CONFIG"
: "${REPO:?}" "${WWW:?}" "${PORT:?}" "${MAINTAIN_HOUR:?}" "${MAINTAIN_MINUTE:?}"
mkdir -p "$WWW"

PYTHON="$REPO/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
WRAPPER="$HERE/maintain-wrapper.sh"
chmod +x "$WRAPPER" "$HERE"/../../src/osm_geocoder/tools/planet-maintain.sh 2>/dev/null || true

subst() {
    sed -e "s#__PYTHON__#${PYTHON}#g" \
        -e "s#__WWW__#${WWW}#g" \
        -e "s#__PORT__#${PORT}#g" \
        -e "s#__LOGDIR__#${HOMEDIR}#g" \
        -e "s#__WRAPPER__#${WRAPPER}#g" \
        -e "s#__CONFIG__#${CONFIG}#g" \
        -e "s#__HOUR__#${MAINTAIN_HOUR}#g" \
        -e "s#__MINUTE__#${MAINTAIN_MINUTE}#g" \
        "$1"
}

# ⚠️ Linux hosts get cron, not launchd. This installer was macOS-only, and that
# stopped being a footnote on 2026-09-15 when the OSM role moved to a Linux box:
# the nightly re-split could not be installed on the host that now holds the
# planet, so it simply had no home. osm-watchdog alarms when the re-split goes
# stale, which is the only reason the gap was visible at all.
#
# The static server is NOT installed here on Linux — use
# `fw svc osm-extracts --install --container`, which gives nginx (and therefore
# HTTP Range on multi-GB PBFs, which python's http.server has never had).
#
# The cron line is tagged `# fw-timer:<label>` to match the convention in
# facetwork's scripts/lib/_helpers/_timer.sh, so `fw svc ... --status` and a
# human reading `crontab -l` see one consistent format. Deliberately not a
# dependency on that repo: this installer must work from a bare checkout.
if [ "$(uname)" != "Darwin" ]; then
    LABEL="com.facetwork.osm-maintain"
    TAG="# fw-timer:$LABEL"
    LINE="${MAINTAIN_MINUTE} ${MAINTAIN_HOUR} * * * ${WRAPPER} ${CONFIG} >> ${HOMEDIR}/maintain.log 2>&1  ${TAG}"
    TMPCRON="$(mktemp)"
    crontab -l 2>/dev/null | grep -vF "$TAG" | grep -v '^$' > "$TMPCRON" || true
    printf '%s\n' "$LINE" >> "$TMPCRON"
    crontab "$TMPCRON" && rm -f "$TMPCRON"
    echo "installed (cron): $LABEL — ${MAINTAIN_HOUR}:$(printf '%02d' "${MAINTAIN_MINUTE}") daily"
    echo "  wrapper: ${WRAPPER}"
    echo "  log:     ${HOMEDIR}/maintain.log"
    echo
    echo "static server: NOT installed here — run"
    echo "  fw svc osm-extracts --install --container --root ${WWW}"
    exit 0
fi

for svc in com.facetwork.osm-extract-server com.facetwork.osm-maintain; do
    subst "$HERE/$svc.plist.template" > "$LA/$svc.plist"
    launchctl bootout "$DOMAIN/$svc" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$LA/$svc.plist"
    echo "installed + loaded: $svc"
done

echo
echo "static server : http://<this-host>:${PORT}/   (serving ${WWW})"
echo "nightly maintain: ${MAINTAIN_HOUR}:$(printf '%02d' "${MAINTAIN_MINUTE}") daily"
echo "run maintain now: ${WRAPPER} ${CONFIG}"
echo "consumers set  : FW_GEOFABRIK_BASE_URL=${BASE_URL:-http://<this-host>:${PORT}}"
