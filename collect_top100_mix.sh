#!/bin/sh
set -u
cd "$(dirname "$0")"

PY=.venv/bin/python
COMMON="--use-login-profile --once --session-seconds 60"

echo "=== web_browsing (52 domains, ~60s each) ==="
$PY src/collect_browsing.py $COMMON --total-seconds 4000

echo "=== social_media_browsing (x.com, instagram.com) ==="
$PY src/collect_social.py $COMMON --total-seconds 200

echo "=== video_streaming (youtube.com, instagram.com reels) ==="
$PY src/collect_video.py $COMMON --total-seconds 200

echo "=== audio_streaming (external demo) ==="
$PY src/collect_audio.py $COMMON --total-seconds 120

echo "=== file_download (external demo) ==="
$PY src/collect_downloads.py $COMMON --total-seconds 120

echo "=== done ==="
