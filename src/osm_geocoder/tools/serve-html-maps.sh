#!/usr/bin/env bash
# Serve rendered HTML maps with Range-request support (required by PMTiles).
#
# Usage:
#   ./serve-html-maps.sh              # port 8000
#   ./serve-html-maps.sh --port 9000  # custom port
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [ -f "${REPO_ROOT}/scripts/_env.sh" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/scripts/_env.sh"
fi

if [ -f "${REPO_ROOT}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.venv/bin/activate"
fi

exec python3 "${SCRIPT_DIR}/serve-html-maps.py" "$@"
