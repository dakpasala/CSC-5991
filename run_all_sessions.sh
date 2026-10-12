#!/usr/bin/env bash
# Sets up the Python environment (creating/populating .venv if needed) and
# then runs the full feature-extraction pipeline across every session
# directory in the current directory and every window size (10/8/4/2/1
# seconds), producing session_statistics_10_second_window.csv through
# session_statistics_1_second_window.csv, written to the current directory.
#
# Run this FROM the directory that holds all the raw session directories
# (this script just needs to know where the repo's src/ and requirements.txt
# are, which it finds relative to its own location).
set -euo pipefail

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

python3 "$SCRIPT_DIR/src/build_session_csv.py" all --window-seconds all
