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
     seconds to live_runs/eval_<tag>_timeline.csv here. If the video position
     stops advancing for 15s, the stall is logged with a screenshot and the
     page text (live_runs/eval_<tag>_stall<N>.png/.txt, to see what the site
     showed), a play/skip is attempted, and after 30s the page is reloaded
     (at most 3 times). Early test runs all died ~30s in, which is what made
     most of their windows unscoreable;
  3. after --minutes, close the browser, stop live-router with SIGINT (it
     flushes its last window), and copy the predictions back, plus the raw
     per-window stats (live_runs/eval_<tag>_stats.csv) for diagnosis and
     live-router's per-window CPU/timing/memory measurements
     (live_runs/eval_<tag>_perf.csv, summarized next to the accuracy);
  4. score them: expected label vs. predicted label for every window that
     started after playback began (plus --warmup-windows more, since the
     first windows are partial and join connections mid-stream), and
     separately for active windows (>= --min-packets packets) and playing
     windows (the timeline shows the video advancing during them) -- the
     playing-window numbers are the ones that measure the classifier, the
     others also count silence and stalls. Printed, appended to
     live_runs/eval_summary.csv, and written per window to
     live_runs/eval_<tag>_scored.csv.

While it runs, the router's predictions are fetched every --live-every
seconds and printed one line per 10s window, next to what the browser was
doing during that window (video position, playing or not, resolution) and
the window's packet count and frac_udp -- also saved to
live_runs/eval_<tag>_live.log. A video that ends before --minutes is up is
reloaded from the start, so a run can be longer than its video.

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
    # One shared connection (ControlMaster) instead of a new ssh handshake per
    # call: the live per-window printout polls the router every few seconds.
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/sac-ssh-%C", "-o", "ControlPersist=60",
         target, command],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"ssh {target} failed ({result.returncode}): {result.stderr.strip()}\n"
            "Key login to the router is required for --router; see the module docstring."
        )
    return result.stdout


def start_live_router(target, model, interface, remote_predictions, max_seconds, priority):
    log = remote_predictions.replace(".csv", ".log")
    stats = remote_predictions.replace(".csv", "_stats.csv")
    perf = remote_predictions.replace(".csv", "_perf.csv")
    # Dry-run QoS decisions (nothing on the router changes), logged as events
    # the live printout shows next to the window predictions.
    prio = remote_predictions.replace(".csv", "_priority.csv")
    priority_flags = f"-priority -priority-log {shlex.quote(prio)} " if priority else ""
    # nohup if BusyBox has it; stdin from /dev/null so ssh returns at once.
    # Go re-enables SIGINT even though a background job starts with it
    # ignored (os/signal.Notify), so stop_live_router's kill -INT still gets
    # a clean shutdown with the last window flushed.
    cmd = (
        f"cd {REMOTE_DIR} && N=$(command -v nohup || true); "
        f"$N ./live-router -interface {shlex.quote(interface)} -model-dir {shlex.quote(model)} "
        f"-predictions-output {shlex.quote(remote_predictions)} -output {shlex.quote(stats)} -perf-output {shlex.quote(perf)} "
        # Router-side safety net: live-router stops itself even if this script
        # dies or the terminal closes. An orphaned instance once ran for ~6h
        # and peaked at 462MB of the router's ~1GB (no swap).
        f"-max-duration {int(max_seconds)}s "
        # The router's own traffic on lan2 -- e.g. this script's ssh polling
        # for the live printout -- isn't client traffic, and leaves the
        # router as GSO super-packets (1,750-2,150 B seen) that tripped the
        # >1,514 B check and added stray TCP packets to every window.
        f"-exclude-own-traffic {priority_flags}"
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
    # Headless Chromium advertises itself ("HeadlessChrome" in the user
    # agent, navigator.webdriver = true); sites treat that as a bot, and
    # YouTube playback died ~30s into every early test run.
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(service=Service(chromedriver), options=opts)
    ua = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
    driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {"userAgent": ua})
    return driver


GATE_BUTTON_TEXTS = ("Accept all", "Reject all", "I agree", "Start Watching", "Accept", "Skip", "Skip Ad", "Skip Ads")


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
return {t: v.currentTime, paused: v.paused, ended: v.ended, w: v.videoWidth, h: v.videoHeight};
"""


def save_stall_diagnostics(driver, prefix):
    """Screenshot + visible page text, to see what the site put up instead
    of the video (bot check, sign-in wall, ad, error message...)."""
    try:
        driver.save_screenshot(prefix + ".png")
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        with open(prefix + ".txt", "w") as f:
            f.write(driver.current_url + "\n\n" + text[:5000])
        first = " ".join(text.split())[:160]
        print(f"  stall diagnostics -> {prefix}.png/.txt (page says: {first!r})")
    except Exception as exc:
        print(f"  could not save stall diagnostics: {exc}")


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


def same_page(url):
    """URL identity for "is the browser still on our video": YouTube keeps
    the video id in the first query parameter and may append others (&t=,
    &pp=); trailing slashes vary."""
    return url.split("&")[0].rstrip("/")


# --- live per-window printout ---------------------------------------------------


class LivePrinter:
    """Fetches the router's new prediction and stats rows and prints one line
    per closed window, joined with what the browser was doing during it."""

    def __init__(self, target, remote_predictions, expected, log_path):
        self.target = target
        self.pred = f"{REMOTE_DIR}/{remote_predictions}"
        self.stats = f"{REMOTE_DIR}/{remote_predictions.replace('.csv', '_stats.csv')}"
        self.prio = f"{REMOTE_DIR}/{remote_predictions.replace('.csv', '_priority.csv')}"
        self.prio_header, self.n_prio = None, 0
        self.expected = expected
        self.log = open(log_path, "w")
        self.timeline = []  # (seconds_since_router_start, video_time, playing, width, height)
        self.n_pred = self.n_stats = 0
        self.pred_header = self.stats_header = None
        self.stats_rows = {}
        self.correct = self.scored = 0

    def record(self, sec, t, playing, w, h):
        self.timeline.append((sec, t, playing, w, h))

    def _fetch(self, path, header, n):
        # header + every row after the n already seen
        out = ssh(self.target, f"[ -f {path} ] && {{ head -1 {path}; tail -n +{n + 2} {path}; }} || true", check=False)
        rows = list(csv.reader(out.splitlines()))
        if not rows:
            return header, []
        return rows[0], rows[1:]

    def poll(self):
        self.stats_header, new_stats = self._fetch(self.stats, self.stats_header, self.n_stats)
        self.n_stats += len(new_stats)
        if self.stats_header:
            for r in new_stats:
                row = dict(zip(self.stats_header, r))
                self.stats_rows[row.get("window_index")] = row
        self.pred_header, new_pred = self._fetch(self.pred, self.pred_header, self.n_pred)
        self.n_pred += len(new_pred)
        for r in new_pred:
            self._print(dict(zip(self.pred_header, r)))
        self.prio_header, new_prio = self._fetch(self.prio, self.prio_header, self.n_prio)
        self.n_prio += len(new_prio)
        for r in new_prio:
            self._print_priority(dict(zip(self.prio_header, r)))

    def _print_priority(self, e):
        if e.get("event") == "on":
            line = (f"    ▶ PRIORITY ON  [{e['label']}] {e['protocol']} flow {e['flow_id']} (server port {e['server_port']}), "
                    f"{int(e['window_bytes']) / 1e6:.1f} MB in window {e['window_index']}, confidence {float(e['confidence']):.2f}")
        else:
            line = f"    ■ PRIORITY OFF [{e['label']}] {e['protocol']} flow {e['flow_id']} (server port {e['server_port']}): {e['reason']}"
        print(line)
        self.log.write(line + "\n")
        self.log.flush()

    def _print(self, p):
        idx = p["window_index"]
        start, end = float(p["window_start_s"]), float(p["window_end_s"])
        polls = [x for x in self.timeline if start <= x[0] < end]
        playing = any(x[2] for x in polls)
        last = polls[-1] if polls else None
        if last and last[1] is not None:
            m, sec = divmod(int(last[1]), 60)
            video = f"video {m}:{sec:02d}, {'playing' if playing else 'NOT playing'}, {last[3]}x{last[4]}"
        else:
            video = "no video" if self.expected is None else "video not loaded"
        st = self.stats_rows.get(idx, {})
        pkts = st.get("total_packet_count", "?")
        udp = st.get("frac_udp", "")
        udp = f"{float(udp):.2f}" if udp not in ("", None) else "-"
        mark = ""
        if self.expected is not None:
            ok = p["predicted_label"] == self.expected
            mark = "✓" if ok else "✗"
            if playing:
                self.scored += 1
                self.correct += ok
        running = f" | running {self.correct}/{self.scored} playing windows correct" if self.scored else ""
        line = (f"[window {int(idx):3d} | {start:5.0f}-{end:5.0f}s] {p['predicted_label']:13s} {float(p['confidence']):.2f} {mark} "
                f"| {pkts:>6} pkts, udp {udp} | {video}{running}")
        print(line)
        self.log.write(line + "\n")
        self.log.flush()

    def close(self):
        self.log.close()


# --- scoring ------------------------------------------------------------------


def playing_windows(df, timeline_path):
    """Window index -> whether the timeline saw the video advancing at some
    poll inside that window (polls are every 5s, so ~2 per 10s window)."""
    t = pd.read_csv(timeline_path)
    if "playing" not in t.columns or t.empty:
        return None
    t = t[t.playing.astype(str) == "True"]
    starts = t.seconds_since_router_start.to_numpy()
    return df.apply(lambda w: bool(((starts >= w.window_start_s) & (starts < w.window_end_s)).any()), axis=1)


def score(predictions_path, expected, skip_before_s, warmup_windows, stats_path=None, min_packets=100, timeline_path=None, scored_path=None):
    """Window accuracy over every window after playback started, and -- when
    the raw stats are available -- over just the active windows (at least
    min_packets packets). Once its buffer is full, YouTube fetches in bursts,
    so many 10s windows hold little but background traffic; 22-29% of the
    training set's YouTube windows have under 100 packets too. Active-window
    accuracy is how a deployed classifier would be judged: it should label
    the streaming, not the silence between bursts."""
    df = pd.read_csv(predictions_path)
    if stats_path and os.path.isfile(stats_path):
        # Always present: live-router computes these even when the model's
        # selective extraction skips the rest (core_go diagnosticColumns).
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
    if timeline_path and os.path.isfile(timeline_path):
        flags = playing_windows(usable, timeline_path)
        if flags is not None:
            usable = usable.assign(playing=flags)
            playing = usable[usable.playing]
            summary["playing_windows"] = f"{len(playing)} of {len(usable)} (video advancing during the window)"
            if not playing.empty:
                summary["playing_label_counts"] = "; ".join(f"{k}={v}" for k, v in playing.predicted_label.value_counts().items())
                if expected is not None:
                    summary["playing_window_accuracy"] = round((playing.predicted_label == expected).mean(), 4)
                    if expected.startswith("youtube"):
                        summary["playing_any_youtube_accuracy"] = round(playing.predicted_label.str.startswith("youtube").mean(), 4)
    if scored_path:
        usable.to_csv(scored_path, index=False)
    return summary


def perf_summary(perf_path):
    """Resource use on the router over the run, from live-router's
    -perf-output: what the classifier costs, next to how accurate it was."""
    p = pd.read_csv(perf_path)
    if p.empty:
        return {}
    steady = p.iloc[1:] if len(p) > 1 else p  # window 0 includes startup warm-up
    out = {
        "cpu_percent_mean": round(steady.cpu_percent.mean(), 2),
        "cpu_percent_max": round(steady.cpu_percent.max(), 2),
        "window_close_ms_mean": round(steady.window_close_ms.mean(), 3),
        "window_close_ms_max": round(steady.window_close_ms.max(), 3),
        "packet_handling_us_per_packet": round(steady.packet_handling_us.sum() / max(steady.packets_in_window.sum(), 1), 3),
        "heap_alloc_mb_max": round(p.heap_alloc_mb.max(), 2),
        "max_rss_mb": round(p.max_rss_mb.max(), 2),
        "packets_retained_end": int(p.packets_retained.iloc[-1]),
        "flows_retained_end": int(p.flows_retained.iloc[-1]),
    }
    if "rss_mb" in p and p.rss_mb.notna().any():
        out["rss_mb_end"] = round(p.rss_mb.dropna().iloc[-1], 2)
    if p.inference_us.notna().any():
        out["inference_us_mean"] = round(steady.inference_us.mean(), 1)
        out["inference_us_max"] = round(steady.inference_us.max(), 1)
    return out


def print_summary(summary):
    print("\nresult:")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def append_summary(path, row):
    """Add one run's row, rewriting the file with the union of columns when
    the new row has fields the file doesn't (the summary gained fields as
    this script grew; a plain append would misalign every later row)."""
    new = pd.DataFrame([row])
    if os.path.isfile(path):
        try:
            existing = pd.read_csv(path)
        except pd.errors.ParserError:
            raise SystemExit(f"{path} is malformed (written by an older version of this script); "
                             "rebuild it with --rebuild-summary")
        if list(existing.columns) != list(new.columns):
            pd.concat([existing, new], ignore_index=True).to_csv(path, index=False)
            return
    new.to_csv(path, mode="a", header=not os.path.isfile(path), index=False)


def rebuild_summary(path, warmup_windows, min_packets):
    """Recreate the summary from every run's own files in live_runs/, e.g.
    after an older version of this script left it malformed. Each run is
    re-scored with the current scoring (playing/active windows where its
    timeline/stats exist); the playback offset is taken from the timeline's
    first poll, so it can differ by a few seconds from the original run."""
    import glob, re

    rows = []
    for pred in sorted(glob.glob("live_runs/eval_*.csv")):
        m = re.fullmatch(r"live_runs/eval_((idle|youtube|twitch)(_tcp)?_\d{8}_\d{6})\.csv", pred)
        if not m:
            continue  # _stats/_timeline/_scored/_perf companions, and the summary itself
        tag, service, tcp = m.group(1), m.group(2), bool(m.group(3))
        expected = "youtube_tcp" if (service == "youtube" and tcp) else EXPECTED[service]
        timeline, stats, perf = (pred.replace(".csv", suffix) for suffix in ("_timeline.csv", "_stats.csv", "_perf.csv"))
        offset = 0.0
        if os.path.isfile(timeline):
            t = pd.read_csv(timeline)
            if not t.empty:
                offset = float(t.seconds_since_router_start.iloc[0])
        try:
            summary = score(pred, expected, offset, warmup_windows, stats, min_packets, timeline)
        except SystemExit as exc:
            print(f"  skipping {tag}: {exc}")
            continue
        if os.path.isfile(perf):
            summary.update(perf_summary(perf))
        rows.append({"tag": tag, "service": service, "disable_quic": tcp, **summary})
        print(f"  {tag}: playing_window_accuracy={summary.get('playing_window_accuracy', '-')}, window_accuracy={summary.get('window_accuracy', '-')}")
    if os.path.isfile(path):
        os.replace(path, path + ".bak")
        print(f"old file kept as {path}.bak")
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"rebuilt {path} from {len(rows)} runs")


# --- main ---------------------------------------------------------------------


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", choices=sorted(EXPECTED))
    parser.add_argument("--url", help="video/stream URL (not needed for --service idle)")
    parser.add_argument("--minutes", type=float, default=3.0)
    parser.add_argument("--disable-quic", action="store_true", help="force YouTube over TCP; expected label becomes youtube_tcp")
    parser.add_argument("--router", help="ssh target of the router, e.g. root@192.168.1.1; omit to only play + log the timeline")
    parser.add_argument("--model", default="model/final_option_c.json", help="model path on the router, relative to /root/csc5991")
    parser.add_argument("--interface", default="lan2")
    parser.add_argument("--warmup-windows", type=int, default=1, help="extra windows skipped after playback starts")
    parser.add_argument("--min-packets", type=int, default=100, help="packets a window needs to count as active")
    parser.add_argument("--no-priority", dest="priority", action="store_false",
                        help="with --router: don't run live-router's dry-run priority decisions (-priority)")
    parser.add_argument("--live-every", type=float, default=10, help="with --router: seconds between live per-window printouts (0 = off)")
    parser.add_argument("--stats", metavar="STATS_CSV", help="with --score: the matching raw window stats, for active-window accuracy")
    parser.add_argument("--timeline", metavar="TIMELINE_CSV", help="with --score: the matching playback timeline, for playing-window accuracy")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--chromedriver", default="/usr/bin/chromedriver")
    parser.add_argument("--rebuild-summary", action="store_true", help="recreate live_runs/eval_summary.csv from every run's saved files, then exit")
    parser.add_argument("--score", metavar="PREDICTIONS_CSV", help="only score an existing predictions file (no browser, no router)")
    args = parser.parse_args()

    if args.rebuild_summary:
        rebuild_summary("live_runs/eval_summary.csv", args.warmup_windows, args.min_packets)
        return
    if not args.service:
        parser.error("--service is required")
    expected = EXPECTED[args.service]
    if args.service == "youtube" and args.disable_quic:
        expected = "youtube_tcp"

    if args.score:
        print_summary(score(args.score, expected, 0, args.warmup_windows, args.stats, args.min_packets, args.timeline))
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
    # live-router's window times count from its first captured packet, which
    # arrives right after launch -- so measure from just before the launch,
    # not after start_live_router's 3s liveness check.
    router_started = time.time()
    if args.router:
        print(f"starting live-router on {args.router} ({args.model}, {args.interface}) ...")
        router_started = time.time()
        # Playback can take up to ~60s to start and stalls can add reloads,
        # so leave generous slack beyond --minutes before the safety net fires.
        pid = start_live_router(args.router, args.model, args.interface, remote_predictions, args.minutes * 60 + 300, args.priority)

    driver = None
    play_started = router_started
    printer = None
    if args.router and args.live_every > 0:
        printer = LivePrinter(args.router, remote_predictions, expected, f"live_runs/eval_{tag}_live.log")
        print("live per-window predictions below (✓/✗ = matches the expected label; running count is over playing windows)")
    try:
        with open(timeline_path, "w", newline="") as f:
            timeline = csv.writer(f, lineterminator="\n")
            timeline.writerow(["unix_time", "seconds_since_router_start", "video_time_s", "paused", "width", "height", "playing"])
            if args.service != "idle":
                print(f"opening {args.url} in headless Chromium{' (QUIC disabled)' if args.disable_quic else ''} ...")
                driver = open_browser(args.disable_quic, args.chromium, args.chromedriver)
                driver.get(args.url)
                try:
                    state = wait_for_playback(driver)
                except SystemExit:
                    # Same diagnostics as a mid-run stall, so a run that never
                    # starts (ad, raid, gate, stream offline) shows why.
                    save_stall_diagnostics(driver, f"live_runs/eval_{tag}_nostart")
                    raise
                play_started = time.time()
                print(f"playing at {state['w']}x{state['h']}, {play_started - router_started:.0f}s after live-router started")
            end = time.time() + args.minutes * 60
            last_t, last_advance, stalls, reloads, diagnosed = None, time.time(), 0, 0, False
            restarts, last_poll = 0, 0.0
            while time.time() < end:
                now = time.time()
                state = driver.execute_script(VIDEO_STATE_JS) if driver else None
                if driver:
                    t = state["t"] if state else None
                    playing = bool(state) and not state["paused"] and last_t is not None and t > last_t
                    timeline.writerow([f"{now:.3f}", f"{now - router_started:.1f}",
                                       f"{t:.1f}" if t is not None else "", state["paused"] if state else "",
                                       state["w"] if state else "", state["h"] if state else "", playing])
                    f.flush()
                    if printer:
                        printer.record(now - router_started, t, playing, state["w"] if state else 0, state["h"] if state else 0)
                    last_t = t
                    if (state and state.get("ended")) or same_page(driver.current_url) != same_page(args.url):
                        # The video finished (or YouTube autoplay moved on to
                        # another one): start it again so long runs keep
                        # measuring the same, known content.
                        restarts += 1
                        print(f"  video ended at {now - router_started:.0f}s; restarting it (restart {restarts})")
                        driver.get(args.url)
                        try:
                            wait_for_playback(driver, timeout_s=45)
                        except SystemExit as exc:
                            print(f"  {exc}")
                        last_t, last_advance, diagnosed = None, time.time(), False
                    elif playing:
                        last_advance, diagnosed = now, False
                    elif now - last_advance >= 15:
                        if not diagnosed:
                            stalls += 1
                            print(f"  video stalled {now - last_advance:.0f}s at {now - router_started:.0f}s (video time {t})")
                            save_stall_diagnostics(driver, f"live_runs/eval_{tag}_stall{stalls}")
                            diagnosed = True
                        click_through_gates(driver)
                        if now - last_advance >= 30 and reloads < 3:
                            reloads += 1
                            print(f"  reloading the page (reload {reloads}/3) ...")
                            driver.get(args.url)
                            try:
                                wait_for_playback(driver, timeout_s=45)
                                print(f"  playing again at {time.time() - router_started:.0f}s")
                            except SystemExit as exc:
                                print(f"  {exc}")
                            last_t, last_advance, diagnosed = None, time.time(), False
                if printer and time.time() - last_poll >= args.live_every:
                    printer.poll()
                    last_poll = time.time()
                time.sleep(5)
    finally:
        if driver:
            driver.quit()
        if pid:
            print("stopping live-router ...")
            stop_live_router(args.router, pid)

    if printer:
        printer.poll()  # the windows that closed while stopping, incl. the final partial one
        printer.close()
        print(f"live per-window log -> live_runs/eval_{tag}_live.log")
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
    perf_name = remote_predictions.replace(".csv", "_perf.csv")
    with open(perf_name, "w") as f:
        f.write(ssh(args.router, f"cat {REMOTE_DIR}/{perf_name}"))
    print(f"router resource use -> {perf_name}")
    if args.priority:
        prio_name = remote_predictions.replace(".csv", "_priority.csv")
        with open(prio_name, "w") as f:
            f.write(ssh(args.router, f"cat {REMOTE_DIR}/{prio_name} 2>/dev/null", check=False))
        print(f"priority decisions (dry run) -> {prio_name}")

    scored_path = local_predictions.replace(".csv", "_scored.csv")
    summary = score(local_predictions, expected, play_started - router_started, args.warmup_windows, stats_name,
                    args.min_packets, timeline_path, scored_path)
    perf = perf_summary(perf_name)
    print_summary(summary)
    print("\nrouter resource use (live-router process):")
    for k, v in perf.items():
        print(f"  {k}: {v}")
    summary = {**summary, **perf}
    append_summary(
        "live_runs/eval_summary.csv",
        {"tag": tag, "service": args.service, "url": args.url or "", "disable_quic": args.disable_quic,
         "model": args.model, "minutes": args.minutes, **summary},
    )
    print(f"per-window scoring -> {scored_path}")
    print("summary appended -> live_runs/eval_summary.csv")


if __name__ == "__main__":
    main()
