#!/usr/bin/env bash
# Build the Go live-capture tool for the lab router and copy it there with
# the model, in one step. Run from the repo root on the Mac:
#
#   ./deploy_router.sh
#   MODEL=XGBoostModels/10s_no_applayer/final_option_d.json ./deploy_router.sh
#
# The router (OpenWrt 25.12, MediaTek Filogic, aarch64 Cortex-A53, ~1GB RAM,
# no Python/gcc/git) gets a single static binary: CGO_ENABLED=0 makes the
# live tool capture through a pure-Go AF_PACKET socket instead of libpcap
# (see src/live_capture_go/live/capture_linux_nocgo.go), and an XGBoost .json
# -model-dir is scored in Go (src/live_capture_go/xgbmodel), so nothing else
# needs installing there.
#
# Expects an ssh config entry "prs-router" (root@192.168.1.1 via ProxyJump
# prs). Files are streamed over plain ssh rather than scp, since OpenWrt's
# dropbear may have no sftp-server for scp to use. One ssh connection is
# reused for every step, so the password is asked for only once.
set -euo pipefail

ROUTER=${ROUTER:-prs-router}
MODEL=${MODEL:-XGBoostModels/10s_no_applayer/final_option_c.json}
REMOTE_DIR=${REMOTE_DIR:-/root/csc5991}   # on the persistent overlay, not RAM-backed /tmp
INTERFACE=${INTERFACE:-lan2}

model_dir=$(dirname "$MODEL")
model_name=$(basename "$MODEL" .json)
for f in "$MODEL" "$model_dir/$model_name.features.json" "$model_dir/classes.json"; do
  [[ -f $f ]] || { echo "missing $f (train it with: python src/train_xgboost_ablation.py --final ...)" >&2; exit 1; }
done

echo "building live tool for linux/arm64 (static, no cgo)..."
mkdir -p build
(cd src && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o ../build/live-router ./live_capture_go/live)
ls -lh build/live-router | awk '{print "  build/live-router:", $5}'

control=$(mktemp -u "${TMPDIR:-/tmp}/csc5991-router.XXXXXX")
ssh_opts=(-o ControlMaster=auto -o "ControlPath=$control" -o ControlPersist=120)
trap 'ssh "${ssh_opts[@]}" -O exit "$ROUTER" 2>/dev/null || true' EXIT

copy() { # copy <local file> <remote path>
  ssh "${ssh_opts[@]}" "$ROUTER" "cat > '$2'" < "$1"
}

echo "copying to $ROUTER:$REMOTE_DIR ..."
ssh "${ssh_opts[@]}" "$ROUTER" "mkdir -p '$REMOTE_DIR/model' '$REMOTE_DIR/live_runs'"
copy build/live-router "$REMOTE_DIR/live-router"
copy "$MODEL" "$REMOTE_DIR/model/$model_name.json"
copy "$model_dir/$model_name.features.json" "$REMOTE_DIR/model/$model_name.features.json"
copy "$model_dir/classes.json" "$REMOTE_DIR/model/classes.json"
ssh "${ssh_opts[@]}" "$ROUTER" "chmod +x '$REMOTE_DIR/live-router' && ls -l '$REMOTE_DIR' '$REMOTE_DIR/model'"

cat <<EOF

Deployed. To capture and classify live on the router:

  ssh $ROUTER
  cd $REMOTE_DIR && ./live-router -interface $INTERFACE -model-dir model/$model_name.json

Predictions go to $REMOTE_DIR/live_runs/live_predictions_10_second_window_<timestamp>.csv
(add -output live_runs/stats.csv to also keep the raw window stats). Ctrl+C stops it cleanly.
Run it inside tmux/screen, or with nohup, if it should outlive the ssh session.
EOF
