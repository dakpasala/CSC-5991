"""Benchmark the CPU cost of each feature-extraction calculation, averaged
across many real sessions -- directly relevant to what a router (one CPU
core, no parallelism, no process pool) could realistically compute in real
time. Two columns out: stat name, average CPU time to compute it (us),
printed and saved to feature_extraction_cost_benchmark.csv.

Per-packet costs (scale with packet arrival rate -- what actually has to
keep up with live traffic): raw header parsing, flow identification,
retransmission/out-of-order/RTT detection. Reported as average time PER
PACKET.

Per-window costs (computed once per --window-seconds, so amortized over
however many packets arrived in that window): every distribution-style
statistic (payload size, packet size, recv_window, interarrival time, RTT,
and the two request-log distributions), flag counts, and the flow/
protocol-mix counters. Reported as the TOTAL time actually incurred once
per window in the real pipeline (summed across up/down/total, not averaged
per call) -- that's the real cost the deployed pipeline pays every window,
since it really does compute each of these 3 times per window.

Measures CPU time (time.process_time()), not wall-clock -- excludes any
idle/wait time and isn't affected by whatever else is running on the
machine. Literal CPU-cycle counts require `perf stat` (Linux only); this
is the portable next-best proxy, and the only thing that changes if you
want true cycles later is wrapping this script's invocation with
`perf stat -e cycles,instructions`, not anything in the script itself.

Single-threaded throughout, deliberately -- a router doesn't have a
process pool to lean on, so this measures exactly what one CPU core pays,
same as the real per-session pipeline already does internally (the
ProcessPoolExecutor in build_session_csv.py only parallelizes ACROSS
sessions, never within processing one).

Usage:
    python src/benchmarking/benchmark_feature_costs.py [--source-dir /mnt/NAS/salmo-trutta/traces] [--num-sessions 100] [--window-seconds 10]
"""

import argparse
import os
import sys
import time
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # core pipeline modules live in src/

from build_session_csv import discover_session_directories, parse_exp_label  # noqa: E402
from find_controlled_sessions import is_controlled_experiment  # noqa: E402
from flow_metrics import compute_flow_metrics  # noqa: E402
from flow_tracker import FlowTracker  # noqa: E402
from requests_log import load_relevant_requests  # noqa: E402
from window_features import TCP_FLAGS, _distribution, _flag_count, _load_and_tag_packets  # noqa: E402

DISTRIBUTION_METRICS = ("payload_size", "packet_size", "recv_window", "interarrival_time")
REQUEST_METRICS = ("segment_size_bytes", "request_duration_ms")


def benchmark_session(session_dir, window_seconds):
    """Returns (cost_dict in total CPU seconds per stat, packet_count,
    num_windows) for this one session, or None if it's a controlled
    experiment (skipped, same as the real pipeline)."""
    exp_content = open(os.path.join(session_dir, "exp.txt")).read()
    if is_controlled_experiment(exp_content):
        return None
    label = parse_exp_label(exp_content)

    pcap_path = os.path.join(session_dir, "dump.pcap")

    t_start = time.process_time()
    packets, t0 = _load_and_tag_packets(pcap_path)
    packet_parsing_time = time.process_time() - t_start

    tracker = FlowTracker()
    tagged = []
    flow_packet_lists = defaultdict(list)

    flow_tracking_time = 0.0
    for packet in packets:
        t_start = time.process_time()
        flow_key, direction, _is_new_flow = tracker.process(packet)
        flow_tracking_time += time.process_time() - t_start
        tagged.append(
            {
                "direction": direction,
                "protocol": packet.protocol,
                "wire_len": packet.wire_len,
                "payload_size": packet.payload_size,
                "recv_window": packet.window if packet.protocol == "tcp" else np.nan,
                "timestamp": packet.timestamp,
                "flags": packet.flags,
                "flow_key": flow_key,
            }
        )
        flow_packet_lists[flow_key].append((packet, direction))

    retrans_rtt_time = 0.0
    rtt_samples = []
    for flow_key, flow_packets in flow_packet_lists.items():
        t_start = time.process_time()
        _flags, rtts = compute_flow_metrics(flow_packets)
        retrans_rtt_time += time.process_time() - t_start
        rtt_samples.extend(rtts)

    packet_df = pd.DataFrame(tagged)
    packet_df["window_index"] = ((packet_df["timestamp"] - t0) // window_seconds).astype(int)

    rtt_by_window = defaultdict(list)
    for ts, rtt_ms in rtt_samples:
        rtt_by_window[int((ts - t0) // window_seconds)].append(rtt_ms)

    requests_path = os.path.join(session_dir, "requests_history.json")
    requests = load_relevant_requests(requests_path, label) if os.path.isfile(requests_path) else []

    num_windows = int(packet_df["window_index"].max()) + 1
    window_costs = defaultdict(float)

    for window_index in range(num_windows):
        window_packets = packet_df[packet_df["window_index"] == window_index]

        t_start = time.process_time()
        window_packets["flow_key"].nunique()
        if len(window_packets):
            (window_packets["protocol"] == "udp").mean()
        window_costs["flow_counts_frac_udp"] += time.process_time() - t_start

        for _prefix, mask in (
            ("up", window_packets["direction"] == "up"),
            ("down", window_packets["direction"] == "down"),
            ("total", pd.Series(True, index=window_packets.index)),
        ):
            group = window_packets[mask]

            t_start = time.process_time()
            for flag in TCP_FLAGS:
                _flag_count(group["flags"], flag)
            window_costs["flag_counts"] += time.process_time() - t_start

            interarrival_ms = group["timestamp"].sort_values().diff().dropna() * 1000
            for metric_name, values in (
                ("payload_size", group["payload_size"].tolist()),
                ("packet_size", group["wire_len"].tolist()),
                ("recv_window", group["recv_window"].dropna().tolist()),
                ("interarrival_time", interarrival_ms.tolist()),
            ):
                t_start = time.process_time()
                _distribution(values)
                window_costs[f"{metric_name}_distribution"] += time.process_time() - t_start

        t_start = time.process_time()
        _distribution(rtt_by_window.get(window_index, []))
        window_costs["rtt_ms_distribution"] += time.process_time() - t_start

        window_start = t0 + window_index * window_seconds
        window_end = window_start + window_seconds
        in_window = [r for r in requests if window_start <= r.start_s < window_end]

        t_start = time.process_time()
        len(in_window)
        window_costs["request_count"] += time.process_time() - t_start

        sizes = [r.content_length for r in in_window if r.content_length is not None]
        durations = [r.duration_ms for r in in_window if r.duration_ms is not None]
        for metric_name, values in (("segment_size_bytes", sizes), ("request_duration_ms", durations)):
            t_start = time.process_time()
            _distribution(values)
            window_costs[f"{metric_name}_distribution"] += time.process_time() - t_start

    per_packet_costs = {
        "packet_parsing": packet_parsing_time,
        "flow_tracking": flow_tracking_time,
        "retransmission_rtt_detection": retrans_rtt_time,
    }
    return per_packet_costs, dict(window_costs), len(packets), num_windows


PER_PACKET_STATS = ("packet_parsing", "flow_tracking", "retransmission_rtt_detection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="/mnt/NAS/salmo-trutta/traces")
    parser.add_argument("--num-sessions", type=int, default=100)
    parser.add_argument("--window-seconds", type=float, default=10)
    args = parser.parse_args()

    # Grab extra in case some are controlled experiments (skipped, same as
    # the real pipeline) -- stop once num_sessions real ones are benchmarked.
    candidates = discover_session_directories(root=args.source_dir, batch_index=1, batch_size=args.num_sessions * 2)

    per_packet_avgs = defaultdict(list)  # stat -> [per-session avg-per-packet, ...]
    per_window_avgs = defaultdict(list)  # stat -> [per-session avg-per-window, ...]
    benchmarked = 0
    skipped_controlled = 0
    failed = 0

    for session_dir in candidates:
        if benchmarked >= args.num_sessions:
            break
        try:
            result = benchmark_session(session_dir, args.window_seconds)
        except Exception:
            failed += 1
            print(f"FAILED: {session_dir}")
            traceback.print_exc()
            continue

        if result is None:
            skipped_controlled += 1
            continue

        per_packet_costs, window_costs, packet_count, num_windows = result
        benchmarked += 1
        for stat, total_seconds in per_packet_costs.items():
            per_packet_avgs[stat].append(total_seconds / packet_count)
        for stat, total_seconds in window_costs.items():
            per_window_avgs[stat].append(total_seconds / num_windows)

        print(f"[{benchmarked}/{args.num_sessions}] {session_dir}: {packet_count} packets, {num_windows} windows")

    print(f"\nbenchmarked {benchmarked} sessions, skipped {skipped_controlled} controlled, {failed} failed")

    rows = []
    for stat in PER_PACKET_STATS:
        avg_seconds = np.mean(per_packet_avgs[stat])
        rows.append((f"{stat} (per packet)", avg_seconds * 1e6))
    for stat, values in per_window_avgs.items():
        avg_seconds = np.mean(values)
        rows.append((f"{stat} (per window)", avg_seconds * 1e6))

    result_df = pd.DataFrame(rows, columns=["stat", "avg_time_us"]).sort_values("avg_time_us", ascending=False)
    result_df = result_df.reset_index(drop=True)

    pd.set_option("display.max_rows", None)
    pd.set_option("display.float_format", "{:.3f}".format)
    print("\naverage CPU time per stat (microseconds):")
    print(result_df.to_string(index=False))

    out_path = "feature_extraction_cost_benchmark.csv"
    result_df.to_csv(out_path, index=False)
    print(f"\nsaved to {out_path}")


if __name__ == "__main__":
    main()
