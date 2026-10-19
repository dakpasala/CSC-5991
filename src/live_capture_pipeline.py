"""Live packet capture -> feature extraction, running continuously.

Two independent processes, deliberately decoupled so stats computation can
never cause a dropped packet:

  1. `tcpdump` captures real traffic off a network interface, rotating to a
     brand-new file every --window-seconds (its -G flag). Capturing never
     pauses for anything -- rotation happens at the kernel/tool level,
     completely independent of whether this script has finished processing
     the previous window yet.
  2. This script's consumer loop watches the capture directory. The moment
     a NEWER file appears (proof the previous one is fully closed, since
     tcpdump already rotated away from it), it runs the exact same parser
     used on every pre-recorded session (window_features._parse_and_track_flows
     / _bucket_into_windows / _build_window_rows, completely unmodified) on
     that finished file, appends one row to the output CSV, and deletes the
     processed pcap. If processing ever takes longer than --window-seconds,
     this loop just falls behind and works through the backlog in order --
     tcpdump itself never misses a window regardless, since it was never
     waiting on this script in the first place.

Two real limitations versus the pre-recorded dataset, inherent to live
capture off a NIC, not something this script works around:
  - No requests_history.json (no browser-level telemetry here), so the 13
    application-layer columns (request_count, segment_size_bytes_*,
    request_duration_ms_*) always come out 0/NaN -- this IS the
    router-realistic case the _no_applayer dataset variant represents.
  - No exp.txt, so there's no automatic ground-truth label. The output
    `label` column is just a placeholder ("live_unlabeled") -- validating
    predictions means knowing what traffic you actually generated during
    capture.

Captures at the same snaplen (200 bytes) as the existing dataset, so this
never touches real payload bytes either, consistent with the project's
no-DPI premise throughout.

Requires root (or equivalent capture capabilities) for tcpdump to open the
interface, and tcpdump itself installed on the machine.

Usage:
    sudo python3 src/live_capture_pipeline.py --interface eth0
    sudo python3 src/live_capture_pipeline.py --interface eth0 --window-seconds 10 --output live_stats.csv
"""

import argparse
import glob
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd

from window_features import _build_window_rows, _bucket_into_windows, _parse_and_track_flows

LABEL_PLACEHOLDER = "live_unlabeled"


def start_tcpdump(interface, capture_dir, window_seconds, snaplen):
    os.makedirs(capture_dir, exist_ok=True)
    pattern = os.path.join(capture_dir, "window_%Y%m%d_%H%M%S.pcap")
    cmd = [
        "tcpdump",
        "-i", interface,
        "-s", str(snaplen),
        "-w", pattern,
        "-G", str(int(window_seconds)),
    ]
    print(f"starting capture: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def process_pcap(pcap_path, window_seconds, output_path):
    session_id = os.path.splitext(os.path.basename(pcap_path))[0]

    packet_df, rtt_samples, t0 = _parse_and_track_flows(pcap_path)
    windowed_df, flow_first_window, rtt_by_window = _bucket_into_windows(
        packet_df, rtt_samples, t0, window_seconds
    )
    row_df = _build_window_rows(
        windowed_df, flow_first_window, rtt_by_window, t0, window_seconds, LABEL_PLACEHOLDER, session_id, []
    )

    file_exists = os.path.isfile(output_path)
    row_df.to_csv(output_path, mode="a", header=not file_exists, index=False)
    print(f"{len(row_df)} window(s) from {pcap_path} -> {output_path}")


def consume_loop(capture_dir, window_seconds, output_path, poll_seconds=1.0):
    processed = set()
    while True:
        files = sorted(glob.glob(os.path.join(capture_dir, "window_*.pcap")))
        # Never touch the newest file -- it may still be actively written.
        ready = files[:-1] if len(files) > 1 else []

        for pcap_path in ready:
            if pcap_path in processed:
                continue
            try:
                process_pcap(pcap_path, window_seconds, output_path)
            except Exception as exc:
                print(f"FAILED to process {pcap_path}: {exc}", file=sys.stderr)
            finally:
                processed.add(pcap_path)
                try:
                    os.remove(pcap_path)
                except OSError:
                    pass

        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interface", required=True, help='network interface to capture on, e.g. "eth0"')
    parser.add_argument("--window-seconds", type=float, default=10)
    parser.add_argument("--capture-dir", default="live_captures", help="where rotated pcap files are written")
    parser.add_argument(
        "--output", default=None, help="default: live_statistics_<N>_second_window_<run start timestamp>.csv"
    )
    parser.add_argument("--snaplen", type=int, default=200, help="bytes captured per packet (matches the dataset)")
    args = parser.parse_args()

    run_started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = (
        args.output or f"live_statistics_{int(args.window_seconds)}_second_window_{run_started_at}.csv"
    )

    tcpdump_proc = start_tcpdump(args.interface, args.capture_dir, args.window_seconds, args.snaplen)

    def shutdown(_signum, _frame):
        print("\nstopping capture...")
        tcpdump_proc.terminate()
        tcpdump_proc.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    time.sleep(1)
    if tcpdump_proc.poll() is not None:
        stderr = tcpdump_proc.stderr.read().decode(errors="replace")
        raise SystemExit(f"tcpdump exited immediately -- check interface name and permissions:\n{stderr}")

    print(f"capturing on {args.interface}, {args.window_seconds}s windows -> {output_path} (Ctrl+C to stop)")
    consume_loop(args.capture_dir, args.window_seconds, output_path)


if __name__ == "__main__":
    main()
