#!/bin/sh
set -u
cd "$(dirname "$0")"

PY=.venv/bin/python

# Easy to change: how long each individual domain gets per pass.
SESSION_SECONDS=20
COOLDOWN_SECONDS=2

run_category() {
    category="$1"
    script="$2"
    plan="plans/${category}.json"
    count=$($PY -c "import json; print(len(json.load(open('$plan'))))")
    total=$(( count * (SESSION_SECONDS + COOLDOWN_SECONDS) + 60 ))
    echo "=== $category ($count domains, ~${total}s budget) ==="
    $PY "src/collect_${script}.py" --use-login-profile --once \
        --session-seconds "$SESSION_SECONDS" --cooldown-seconds "$COOLDOWN_SECONDS" \
        --total-seconds "$total"
}

while true; do
    run_category web_browsing browsing
    run_category social_media_browsing social
    run_category video_streaming video
    run_category audio_streaming audio
    echo "=== full sweep complete, looping again (Ctrl+C to stop) ==="
done
