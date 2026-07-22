#!/usr/bin/env bash
# Maintain a self-hosted regional extract set: advance master + re-extract
# (Strategy A, Phase 2). See tools/README.md and `planet-maintain.sh --help`.
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

exec python3 "${SCRIPT_DIR}/planet_maintain.py" "$@"
