#!/usr/bin/env bash
# Run from inside the directory that holds all the session directories.
# Prints the name of the FIRST session directory whose exp.txt is a real
# (non-controlled-experiment) Amazon run -- i.e. contains "amazon.run"
# literally, not something like "amazon-50%..." (a lab/condition run).
set -euo pipefail

for dir in */; do
    dir="${dir%/}"
    [ -f "$dir/exp.txt" ] || continue

    cd "$dir"
    if grep -qiF "amazon.run" exp.txt; then
        cd ..
        echo "$dir"
        exit 0
    else
        cd ..
    fi
done
