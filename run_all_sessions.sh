#!/usr/bin/env bash
# Usage: ./run_all_sessions.sh <batch-index> [batch-size]
#
# Sets up the Python environment (creating/populating .venv if needed) and
# then runs the full feature-extraction pipeline directly against the NAS
# source directory (no local cp needed), one batch at a time, across every
# window size (10/8/4/2/1 seconds). <batch-index> selects which chunk of the
# sorted NAS directory listing to process this run -- batch 1 is the first
# batch-size (default 100) directories, batch 2 the next batch-size, etc.
# Offset = (batch-index - 1) * batch-size, so e.g. with the default size of
# 100, batch-index 6 covers directories 500-600, not 2.
# Output CSVs accumulate in the current directory.
#
# Override the source directory with SOURCE_DIR=/some/other/path if needed.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <batch-index> [batch-size]" >&2
    exit 1
fi

BATCH_INDEX="$1"
BATCH_SIZE="${2:-100}"
SOURCE_DIR="${SOURCE_DIR:-/mnt/NAS/salmo-trutta/traces}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "No .venv found, creating one at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Checking requirements.txt dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

echo "Processing batch $BATCH_INDEX (size $BATCH_SIZE) from $SOURCE_DIR..."
python3 "$SCRIPT_DIR/src/build_session_csv.py" all \
    --source-dir "$SOURCE_DIR" \
    --batch-index "$BATCH_INDEX" \
    --batch-size "$BATCH_SIZE" \
    --window-seconds all
