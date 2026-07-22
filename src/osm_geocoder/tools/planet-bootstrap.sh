#!/usr/bin/env bash
# Split a planet PBF into Geofabrik-style regional extracts (Strategy A, Phase 1).
# See tools/README.md and `planet-bootstrap.sh --help` for details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

# Load .env (no-op if absent; does not override already-set vars).
if [ -f "${REPO_ROOT}/scripts/_env.sh" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/scripts/_env.sh"
fi

# Activate project virtualenv if present.
if [ -f "${REPO_ROOT}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.venv/bin/activate"
fi

exec python3 "${SCRIPT_DIR}/planet_bootstrap.py" "$@"
