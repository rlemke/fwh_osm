#!/usr/bin/env bash
# Publish per-region OSM replication diffs — the producer half of the split.
#
# Phase 1 stamped every extract to follow OUR replication URL but never
# published anything there, so each extract has been a frozen snapshot. This
# fills those directories: it cuts each day's planet diff down to every
# region's polygon and publishes it as that region's own sequenced diff.
#
#   publish-replication.sh --status
#   publish-replication.sh --anchor 5051         # one-time baseline
#   publish-replication.sh --stamp-extracts ...  # one-time, REWRITES each PBF
#   publish-replication.sh --days 7              # nightly
#
# See docs/replication-publishing.md and `--help` for details.
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

exec python3 "${SCRIPT_DIR}/publish_replication.py" "$@"
