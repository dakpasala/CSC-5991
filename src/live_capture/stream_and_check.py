"""Ground-truth test of the router's live classifier: play a known video in
headless Chromium on this machine (the Linux box behind the router) while
the router's live-router labels every 10-second window, then score those
labels against what was actually playing.

Runs on the Linux box, NOT the router: the router has no Python or browser,
and its own traffic doesn't cross lan2 anyway. This box's default route is
the router (192.168.1.1), so everything the browser streams passes through
it and is what live-router sees on lan2.

One run = one service:
  1. (with --router) start live-router on the router over ssh, in the
     background, writing its predictions to live_runs/eval_<tag>.csv there;
  2. open the URL in headless Chromium and make sure the <video> is actually
     playing (consent/mature-content gates are clicked through, playback is
     forced if autoplay is blocked), logging playback state every few
     seconds to live_runs/eval_<tag>_timeline.csv here;
  3. after --minutes, close the browser, stop live-router with SIGINT (it
     flushes its last window), and copy the predictions back, plus the raw
     per-window stats (live_runs/eval_<tag>_stats.csv) for diagnosis;
  4. score them: expected label vs. predicted label for every window that
     started after playback began (plus --warmup-windows more, since the
     first windows are partial and join connections mid-stream), printed and
     appended to live_runs/eval_summary.csv.

Expected label: youtube -> youtube_quic (Chromium streams YouTube over QUIC
by default), or youtube_tcp with --disable-quic; twitch -> twitch.
--service idle plays nothing: a baseline of what this box's background
traffic gets labeled as, which is worth knowing before trusting any run,
since everything else this shared box does goes through lan2 too.
Netflix and Amazon aren't supported: both need a logged-in account and
Widevine DRM, which headless Chromium doesn't have.

Without --router, it only plays the video and writes the timeline; start
live-router on the router yourself, then score its predictions afterwards
with --score <predictions.csv> --service ... (no browser is started).

Router access: --router runs `ssh <target>` non-interactively, so this box
needs key login to the router first (OpenWrt's dropbear reads
/etc/dropbear/authorized_keys):
    cat ~/.ssh/id_ed25519.pub | ssh root@192.168.1.1 'cat >> /etc/dropbear/authorized_keys'

Requires: pip install selenium (uses /usr/bin/chromium + /usr/bin/chromedriver).

Usage (on the Linux box, from ~/CSC-5991):
    python src/live_capture/stream_and_check.py --service youtube --url 'https://www.youtube.com/watch?v=...' --minutes 3 --router root@192.168.1.1
    python src/live_capture/stream_and_check.py --service youtube --disable-quic --url ... --router root@192.168.1.1
    python src/live_capture/stream_and_check.py --service twitch --url https://www.twitch.tv/<channel> --router root@192.168.1.1
    python src/live_capture/stream_and_check.py --service idle --minutes 2 --router root@192.168.1.1
    python src/live_capture/stream_and_check.py --service twitch --score live_runs/eval_xyz.csv
"""

import argparse
import csv
import datetime
import os
import shlex
import subprocess
import sys
import time

import pandas as pd

EXPECTED = {"youtube": "youtube_quic", "twitch": "twitch", "idle": None}
REMOTE_DIR = "/root/csc5991"


# --- router control ---------------------------------------------------------


def ssh(target, command, check=True):
    """Run one command on the router. BatchMode: fail instead of prompting
    for a password nobody is there to type."""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"ssh {target} failed ({result.returncode}): {result.stderr.strip()}\n"
            "Key login to the router is required for --router; see the module docstring."
        )
    return result.stdout


def start_live_router(target, model, interface, remote_predictions):
    log = remote_predictions.replace(".csv", ".log")
    stats = remote_predictions.replace(".csv", "_stats.csv")
    # nohup if BusyBox has it; stdin from /dev/null so ssh returns at once.
    # Go re-enables SIGINT even though a background job starts with it
    # ignored (os/signal.Notify), so stop_live_router's kill -INT still gets
    # a clean shutdown with the last window flushed.
    cmd = (
        f"cd {REMOTE_DIR} && N=$(command -v nohup || true); "
        f"$N ./live-router -interface {shlex.quote(interface)} -model-dir {shlex.quote(model)} "
        f"-predictions-output {shlex.quote(remote_predictions)} -output {shlex.quote(stats)} "
        f"> {shlex.quote(log)} 2>&1 < /dev/null & echo $!"
    )
    pid = ssh(target, cmd).strip().splitlines()[-1]
    time.sleep(3)
    if ssh(target, f"kill -0 {pid} 2>/dev/null && echo alive", check=False).strip() != "alive":
        print(ssh(target, f"cat {REMOTE_DIR}/{log}", check=False), file=sys.stderr)
        raise SystemExit("live-router exited right after starting (its log is above)")
    return pid


def stop_live_router(target, pid):
    ssh(target, f"kill -INT {pid}", check=False)
    for _ in range(30):  # it flushes the last window, then exits
        if ssh(target, f"kill -0 {pid} 2>/dev/null && echo alive", check=False).strip() != "alive":
            return
        time.sleep(1)
    print("warning: live-router still running after SIGINT; killing it", file=sys.stderr)
    ssh(target, f"kill {pid}", check=False)


# --- browser ----------------------------------------------------------------


def open_browser(disable_quic, chromium, chromedriver):
    from selenium import webdriver  # noqa: E402 (only needed when actually playing)
    from selenium.webdriver.chrome.service import Service

    opts = webdriver.ChromeOptions()
    opts.binary_location = chromium
    for arg in (
        "--headless=new",
        "--mute-audio",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1920,1080",  # a real-looking viewport, so players pick normal quality
        "--no-first-run",
        "--disable-dev-shm-usage",
        # A fresh headless profile otherwise spends the first minutes
        # downloading components, safe-browsing lists and field trials --
        # megabytes of non-video TCP traffic in the same windows as the video.
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-domain-reliability",
    ):
        opts.add_argument(arg)
    if disable_quic:
        opts.add_argument("--disable-quic")
    return webdriver.Chrome(service=Service(chromedriver), options=opts)


GATE_BUTTON_TEXTS = ("Accept all", "Reject all", "I agree", "Start Watching", "Accept")


def click_through_gates(driver):
    """Consent dialogs (YouTube in some regions) and Twitch's mature-content
    gate both block playback until clicked."""
    from selenium.webdriver.common.by import By

    for text in GATE_BUTTON_TEXTS:
        for el in driver.find_elements(By.XPATH, f"//button[.//text()[normalize-space()='{text}']]"):
            try:
                el.click()
                time.sleep(1)
                return text
            except Exception:
                pass
    return None


VIDEO_STATE_JS = """
const v = document.querySelector('video');
if (!v) return null;
if (v.paused) { v.muted = true; v.play().catch(() => {}); }
return {t: v.currentTime, paused: v.paused, w: v.videoWidth, h: v.videoHeight};
"""


def wait_for_playback(driver, timeout_s=60):
    start = time.time()
    last_t = None
    while time.time() - start < timeout_s:
        click_through_gates(driver)
        state = driver.execute_script(VIDEO_STATE_JS)
        if state and not state["paused"] and last_t is not None and state["t"] > last_t:
            return state
        last_t = state["t"] if state else None
        time.sleep(2)
    raise SystemExit(f"video never started playing within {timeout_s}s (last state: {last_t})")


# --- scoring ------------------------------------------------------------------


def score(predictions_path, expected, skip_before_s, warmup_windows, stats_path=None, min_packets=100):
    """Window accuracy over every window after playback started, and -- when
    the raw stats are available -- over just the active windows (at least
    min_packets packets). Once its buffer is full, YouTube fetches in bursts,
    so many 10s windows hold little but background traffic; 22-29% of the
    training set's YouTube windows have under 100 packets too. Active-window
    accuracy is how a deployed classifier would be judged: it should label
    the streaming, not the silence between bursts."""
    df = pd.read_csv(predictions_path)
    if stats_path and os.path.isfile(stats_path):
        stats = pd.read_csv(stats_path, usecols=["window_index", "total_packet_count", "frac_udp", "down_packet_size_max"])
        df = df.merge(stats, on="window_index", how="left")
    if df.empty:
        raise SystemExit(f"{predictions_path} has no predictions")
    usable = df[df.window_start_s >= skip_before_s].iloc[warmup_windows:]
    if len(usable) > 1:
        usable = usable.iloc[:-1]  # the final window is partial (cut off by SIGINT)
    if usable.empty:
        raise SystemExit("no full windows after playback started; run longer (--minutes)")
    counts = usable.predicted_label.value_counts()
    summary = {
        "windows_total": len(df),
        "windows_scored": len(usable),
        "majority_label": counts.index[0],
        "mean_confidence": round(usable.confidence.mean(), 4),
        "label_counts": "; ".join(f"{k}={v}" for k, v in counts.items()),
    }
    if expected is not None:
        correct = usable.predicted_label == expected
        summary["expected_label"] = expected
        summary["window_accuracy"] = round(correct.mean(), 4)
        summary["mean_confidence_correct"] = round(usable.confidence[correct].mean(), 4) if correct.any() else None
        summary["mean_confidence_wrong"] = round(usable.confidence[~correct].mean(), 4) if (~correct).any() else None
        if expected.startswith("youtube"):
            summary["any_youtube_accuracy"] = round(usable.predicted_label.str.startswith("youtube").mean(), 4)
    if "total_packet_count" in usable.columns:
        active = usable[usable.total_packet_count >= min_packets]
        summary["active_windows"] = f"{len(active)} of {len(usable)} (>= {min_packets} packets)"
        if not active.empty:
            summary["active_label_counts"] = "; ".join(f"{k}={v}" for k, v in active.predicted_label.value_counts().items())
            if expected is not None:
                summary["active_window_accuracy"] = round((active.predicted_label == expected).mean(), 4)
                if expected.startswith("youtube"):
                    summary["active_any_youtube_accuracy"] = round(active.predicted_label.str.startswith("youtube").mean(), 4)
        # Sanity check for router-side packet merging (GRO/GSO): wire frames
        # are at most 1514 bytes, and so is everything in the training data.
        oversized = (usable.down_packet_size_max > 1514).sum()
        summary["windows_with_packets_over_1514B"] = int(oversized)
        if oversized:
            summary["warning"] = "packets larger than an Ethernet frame were captured: GRO/GSO is on at the router; see ethtool -K ... gro off"
    return summary


def print_summary(summary):
    print("\nresult:")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def append_summary(path, row):
    pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.isfile(path), index=False)


# --- main ---------------------------------------------------------------------


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", choices=sorted(EXPECTED), required=True)
    parser.add_argument("--url", help="video/stream URL (not needed for --service idle)")
    parser.add_argument("--minutes", type=float, default=3.0)
    parser.add_argument("--disable-quic", action="store_true", help="force YouTube over TCP; expected label becomes youtube_tcp")
    parser.add_argument("--router", help="ssh target of the router, e.g. root@192.168.1.1; omit to only play + log the timeline")
    parser.add_argument("--model", default="model/final_option_c.json", help="model path on the router, relative to /root/csc5991")
    parser.add_argument("--interface", default="lan2")
    parser.add_argument("--warmup-windows", type=int, default=1, help="extra windows skipped after playback starts")
    parser.add_argument("--min-packets", type=int, default=100, help="packets a window needs to count as active")
    parser.add_argument("--stats", metavar="STATS_CSV", help="with --score: the matching raw window stats, for active-window accuracy")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--chromedriver", default="/usr/bin/chromedriver")
    parser.add_argument("--score", metavar="PREDICTIONS_CSV", help="only score an existing predictions file (no browser, no router)")
    args = parser.parse_args()

    expected = EXPECTED[args.service]
    if args.service == "youtube" and args.disable_quic:
        expected = "youtube_tcp"

    if args.score:
        print_summary(score(args.score, expected, 0, args.warmup_windows, args.stats, args.min_packets))
        return
    if args.service != "idle" and not args.url:
        parser.error("--url is required unless --service idle")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.service}{'_tcp' if args.disable_quic else ''}_{stamp}"
    os.makedirs("live_runs", exist_ok=True)
    timeline_path = f"live_runs/eval_{tag}_timeline.csv"
    local_predictions = f"live_runs/eval_{tag}.csv"
    remote_predictions = f"live_runs/eval_{tag}.csv"

    pid = None
    router_started = time.time()
    if args.router:
        print(f"starting live-router on {args.router} ({args.model}, {args.interface}) ...")
        pid = start_live_router(args.router, args.model, args.interface, remote_predictions)
        router_started = time.time()

    driver = None
    play_started = router_started
    try:
        with open(timeline_path, "w", newline="") as f:
            timeline = csv.writer(f, lineterminator="\n")
            timeline.writerow(["unix_time", "seconds_since_router_start", "video_time_s", "paused", "width", "height"])
            if args.service != "idle":
                print(f"opening {args.url} in headless Chromium{' (QUIC disabled)' if args.disable_quic else ''} ...")
                driver = open_browser(args.disable_quic, args.chromium, args.chromedriver)
                driver.get(args.url)
                state = wait_for_playback(driver)
                play_started = time.time()
                print(f"playing at {state['w']}x{state['h']}, {play_started - router_started:.0f}s after live-router started")
            end = time.time() + args.minutes * 60
            while time.time() < end:
                now = time.time()
                state = driver.execute_script(VIDEO_STATE_JS) if driver else None
                if state:
                    timeline.writerow([f"{now:.3f}", f"{now - router_started:.1f}", f"{state['t']:.1f}", state["paused"], state["w"], state["h"]])
                    f.flush()
                    if state["paused"]:
                        print(f"  warning: video paused at {state['t']:.0f}s")
                time.sleep(5)
    finally:
        if driver:
            driver.quit()
        if pid:
            print("stopping live-router ...")
            stop_live_router(args.router, pid)

    print(f"timeline -> {timeline_path}")
    if not args.router:
        print("no --router: score the router's predictions afterwards with --score <file> --service", args.service)
        return

    with open(local_predictions, "w") as f:
        f.write(ssh(args.router, f"cat {REMOTE_DIR}/{remote_predictions}"))
    print(f"predictions -> {local_predictions} (copied from {args.router}:{REMOTE_DIR}/{remote_predictions})")
    # Raw per-window feature rows too (same ~130 columns as the training CSVs),
    # so a bad run can be diagnosed from the numbers, not just the labels.
    stats_name = remote_predictions.replace(".csv", "_stats.csv")
    with open(stats_name, "w") as f:
        f.write(ssh(args.router, f"cat {REMOTE_DIR}/{stats_name}"))
    print(f"window stats -> {stats_name}")

    summary = score(local_predictions, expected, play_started - router_started, args.warmup_windows, stats_name, args.min_packets)
    print_summary(summary)
    append_summary(
        "live_runs/eval_summary.csv",
        {"tag": tag, "service": args.service, "url": args.url or "", "disable_quic": args.disable_quic,
         "model": args.model, "minutes": args.minutes, **summary},
    )
    print("summary appended -> live_runs/eval_summary.csv")


if __name__ == "__main__":
    main()
