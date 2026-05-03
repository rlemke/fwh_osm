#!/usr/bin/env bash
# Download All US States — convenience startup script
#
# Sets up HDFS + MongoDB with external data directories under ~/data,
# starts the AgentFlow stack, compiles the workflow, and submits it.
#
# Usage:
#   examples/osm-geocoder/tests/real/scripts/run_osm_cache_states.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXAMPLE_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"

# Load shared env if available
if [ -f "$PROJECT_DIR/scripts/_env.sh" ]; then
    source "$PROJECT_DIR/scripts/_env.sh"
fi

HDFS_NAMENODE_DIR="${HDFS_NAMENODE_DIR:-$HOME/data/hdfs/namenode}"
HDFS_DATANODE_DIR="${HDFS_DATANODE_DIR:-$HOME/data/hdfs/datanode}"
MONGODB_DATA_DIR="${MONGODB_DATA_DIR:-}"
GEOFABRIK_MIRROR="${AFL_GEOFABRIK_MIRROR:-}"

# ---------------------------------------------------------------------------
# 1. Create data directories
# ---------------------------------------------------------------------------
echo "=== Creating data directories ==="
mkdir -p "$HDFS_NAMENODE_DIR" "$HDFS_DATANODE_DIR"
echo "  HDFS NameNode: $HDFS_NAMENODE_DIR"
echo "  HDFS DataNode: $HDFS_DATANODE_DIR"
if [ -n "$MONGODB_DATA_DIR" ]; then
    mkdir -p "$MONGODB_DATA_DIR"
    echo "  MongoDB:       $MONGODB_DATA_DIR"
else
    echo "  MongoDB:       (Docker volume)"
fi
echo ""

# ---------------------------------------------------------------------------
# 2. Bootstrap Docker stack via scripts/setup
# ---------------------------------------------------------------------------
echo "=== Starting AgentFlow stack ==="
SETUP_ARGS=(
    --hdfs
    --hdfs-namenode-dir "$HDFS_NAMENODE_DIR"
    --hdfs-datanode-dir "$HDFS_DATANODE_DIR"
    --osm-agents 3
)
if [ -n "$MONGODB_DATA_DIR" ]; then
    SETUP_ARGS+=(--mongodb-data-dir "$MONGODB_DATA_DIR")
fi
if [ -n "$GEOFABRIK_MIRROR" ]; then
    SETUP_ARGS+=(--mirror "$GEOFABRIK_MIRROR")
fi
"$PROJECT_DIR/scripts/setup" "${SETUP_ARGS[@]}"
echo ""

# ---------------------------------------------------------------------------
# 3. Wait for services to be ready
# ---------------------------------------------------------------------------
echo "=== Waiting for services ==="
echo "Waiting for MongoDB..."
for i in $(seq 1 30); do
    if docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T mongodb mongosh --eval "db.runCommand({ping:1})" &>/dev/null; then
        echo "  MongoDB is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "  WARNING: MongoDB did not become ready in 30s — continuing anyway."
    fi
    sleep 1
done
echo ""

# ---------------------------------------------------------------------------
# 4. Compile the AFL workflow
# ---------------------------------------------------------------------------
echo "=== Compiling osm_cache_states.afl ==="
AFL_FILE="$REAL_DIR/ffl/osm_cache_states.ffl"
OUTPUT_FILE="$REAL_DIR/osm_cache_states.json"

cd "$PROJECT_DIR"
source .venv/bin/activate 2>/dev/null || true

afl --primary "$AFL_FILE" \
    --library "$EXAMPLE_DIR/ffl/osmtypes.ffl" \
    --library "$EXAMPLE_DIR/ffl/osmoperations.ffl" \
    --library "$EXAMPLE_DIR/ffl/osmcache.ffl" \
    -o "$OUTPUT_FILE"

echo "  Compiled to: $OUTPUT_FILE"
echo ""

# ---------------------------------------------------------------------------
# 5. Submit the workflow
# ---------------------------------------------------------------------------
echo "=== Submitting DownloadBatchStates workflow ==="
export AFL_MONGODB_URL="mongodb://afl-mongodb:27017"
python -m afl.runtime.submit \
    --primary "$AFL_FILE" \
    --library "$EXAMPLE_DIR/ffl/osmtypes.ffl" \
    --library "$EXAMPLE_DIR/ffl/osmoperations.ffl" \
    --library "$EXAMPLE_DIR/ffl/osmcache.ffl" \
    --workflow "osm.UnitedStates.cache.DownloadBatchStates"
echo ""

# ---------------------------------------------------------------------------
# 6. Done
# ---------------------------------------------------------------------------
echo "=== Done ==="
echo ""
echo "Access the dashboard at: http://localhost:8080"
echo ""
echo "Useful commands:"
echo "  docker compose ps              # List running services"
echo "  docker compose logs -f         # Follow logs"
echo "  docker compose down            # Stop everything"
echo ""
