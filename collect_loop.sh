#!/bin/sh
set -u
cd "$(dirname "$0")"

PY=.venv/bin/python

SESSION_SECONDS=20
COOLDOWN_SECONDS=2

# Set CHROME_BINARY/INTERFACE in the environment to override each script's own
# default (needed on Linux, where the macOS defaults don't apply), e.g.:
#   CHROME_BINARY=/usr/bin/google-chrome INTERFACE=eth0 ./collect_loop.sh
EXTRA_ARGS=""
[ -n "${CHROME_BINARY:-}" ] && EXTRA_ARGS="$EXTRA_ARGS --chrome-binary $CHROME_BINARY"
[ -n "${INTERFACE:-}" ] && EXTRA_ARGS="$EXTRA_ARGS --interface $INTERFACE"

run_category() {
    category="$1"
    script="$2"
    plan="plans/${category}.json"
    count=$($PY -c "import json; print(len(json.load(open('$plan'))))")
    total=$(( count * (SESSION_SECONDS + COOLDOWN_SECONDS) + 60 ))
    echo "=== $category ($count domains, ~${total}s budget) ==="
    $PY "src/collect_${script}.py" --use-login-profile --once \
        --session-seconds "$SESSION_SECONDS" --cooldown-seconds "$COOLDOWN_SECONDS" \
        --total-seconds "$total" $EXTRA_ARGS
}

# Small login-heavy categories first, then the ~11k-domain web_browsing sweep
# (which alone takes ~70h), then downloads last. Session cookies go stale in
# ~24h, so anything gated on them needs to run well before web_browsing would
# otherwise get to it. build_plans.py also front-loads login-gated domains
# within each plan file for the same reason.
while true; do
    run_category social_media_browsing social
    run_category video_streaming video
    run_category audio_streaming audio
    run_category web_browsing browsing
    run_category file_download downloads
    echo "=== full sweep complete, looping again (Ctrl+C to stop) ==="
done
