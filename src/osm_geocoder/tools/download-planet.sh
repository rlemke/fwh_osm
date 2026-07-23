#!/usr/bin/env bash
# Wrapper for download_planet.py — see tools/README.md and `download-planet.sh --help`.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
# launchd/cron have a minimal PATH; the tools shell out to osmium/curl.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"
[ -f "${REPO_ROOT}/scripts/_env.sh" ] && source "${REPO_ROOT}/scripts/_env.sh"
[ -f "${REPO_ROOT}/.venv/bin/activate" ] && source "${REPO_ROOT}/.venv/bin/activate"
exec python3 "${SCRIPT_DIR}/download_planet.py" "$@"
