"""Benchmark the MEMORY footprint of each feature-extraction stat, averaged
across many real sessions -- this replaced an earlier CPU-cycles framing
(see benchmark_feature_costs.py) after discussion with the professor: for
flow/window-level stats, the thing a router actually has to worry about is
how much state it must hold in memory, not how many cycles a calculation
takes. Printed and saved to benchmarks/feature_extraction_memory_benchmark.csv,
with avg/p95/max bytes per stat -- max/p95 matter alongside the average
since a router has to provision for its worst session/window, not its
typical one.

What "memory to compute this stat" means, per row:
  - Flow identification state (FlowTracker.FlowState): fixed-size, doesn't
    grow with packets -- measured once per flow ("per flow" rows).
  - Retransmission/RTT tracking state (flow_metrics' internal
    _DirectionState.seen_seqs set + the outstanding ack-tracking dict):
    THIS is the interesting one -- seen_seqs adds one entry per
    data-carrying packet for the flow's entire life and is never pruned,
    so it genuinely grows with how much that flow has carried. Measured
    once per flow, at the point the flow finishes (its peak size).
  - "(total per session)" rows for the above two: since neither is ever
    pruned/evicted in this pipeline, the real memory a long-running
    process holds for a session is the SUM across every flow that
    session ever had, not just one flow's average -- that sum only grows
    over a session's life, so its value at the end IS its peak. This is
    the number that actually answers "can a router afford to hold this,"
    not the per-flow average alone.
  - Every distribution-style metric (payload_size, packet_size,
    recv_window, interarrival_time, rtt_ms, segment_size_bytes,
    request_duration_ms): the memory cost of the RAW BUFFERED LIST of
    values that must be held before _distribution() can compute min/max/
    median/percentiles -- these can't be computed with an O(1) running
    accumulator the way a sum or count can, so the full list really has
    to sit in memory until the window closes. Measured once per window.
    (rtt_ms_buffer previously missing from this benchmark despite being
    named in this docstring -- fixed; it's bucketed into windows by ACK
    timestamp exactly like benchmark_feature_costs.py already does.)
  - Every distribution metric above is now split by direction
    (up_*_buffer/down_*_buffer/total_*_buffer), not pooled -- streaming
    traffic is heavily down-dominated, so this is where that asymmetry
    actually shows up (down holds far more packets per window than up).
  - "total_window_buffer (simultaneous, per window)": checked directly
    against window_features._direction_stats's real control flow, not
    assumed -- up/down/total are each built and consumed by one call to
    _direction_stats in turn (its 4 metric lists are a single tuple
    literal, so they DO coexist for that one call, but that call's
    buffers are freed via refcounting before the next direction's call
    begins -- confirmed by reading the actual loop, not guessed). So the
    real simultaneous peak during one window's row-construction is just
    the single largest _direction_stats call's 4 buffers -- always the
    "total" direction, since total's packet set is a superset of
    up's + down's. rtt_ms/segment_size_bytes/request_duration_ms are
    built in separate, later, non-overlapping steps and are added into
    this row as a small conservative pad on top (their lifetimes don't
    truly overlap "total"'s buffers, but they're tiny enough relative to
    it -- hundreds to tens of thousands of bytes vs. total's buffers each
    in the tens of thousands -- that padding them in doesn't materially
    change the peak). up's and down's own buffers are NOT added here;
    they're smaller than total's and never coexist with it.
  - flag_counts / num_flows / frac_udp are NOT given a row: counting is
    streaming-compatible and needs no buffer beyond data already held for
    other reasons, so their marginal memory cost is ~0.

Uses a small hand-written recursive size function (deep_sizeof), not
sys.getsizeof() alone -- sys.getsizeof() on a list only gives the list's
own pointer-array overhead, not the size of the objects it points to,
which would badly understate a list of boxed Python floats/ints.

Single-threaded, sequential across sessions -- same reasoning as the CPU
benchmark: a router doesn't get to parallelize this.

Usage:
    python src/benchmarking/benchmark_feature_memory.py [--source-dir /mnt/NAS/salmo-trutta/traces] [--num-sessions 100] [--window-seconds 10]
"""

import argparse
import os
import sys
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))  # find_controlled_sessions.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))  # shared library modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # build_session_csv.py (main CLI, stays in src/)

from build_session_csv import discover_session_directories, parse_exp_label  # noqa: E402
from find_controlled_sessions import is_controlled_experiment  # noqa: E402
from flow_metrics import SEQ_MODULUS, _DirectionState, _seq_delta, compute_flow_metrics  # noqa: E402
from flow_tracker import FlowTracker  # noqa: E402
from requests_log import load_relevant_requests  # noqa: E402
from window_features import _load_and_tag_packets  # noqa: E402


def deep_sizeof(obj, seen=None):
    """Recursive memory size of obj and everything it references (unlike
    sys.getsizeof alone, which only gives a container's own shallow size).
    Handles the data shapes this pipeline actually uses: dataclasses,
    lists/sets/dicts of primitives."""
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    size = sys.getsizeof(obj)

    if isinstance(obj, dict):
        for key, value in obj.items():
            size += deep_sizeof(key, seen)
            size += deep_sizeof(value, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_sizeof(item, seen)
    elif hasattr(obj, "__dict__"):
        size += deep_sizeof(obj.__dict__, seen)

    return size


def _flow_state_sizes(flow_packets):
    """Mirrors flow_metrics.compute_flow_metrics's exact bookkeeping loop,
    but returns the final internal state objects (for sizing) instead of
    the retransmission/RTT results. Reuses the real _DirectionState/
    _seq_delta rather than re-deriving the logic, so this stays faithful
    to the actual implementation, not an approximation of it."""
    if not flow_packets or flow_packets[0][0].protocol != "tcp":
        return 0

    direction_state = {"up": _DirectionState(), "down": _DirectionState()}
    outstanding = {"up": {}, "down": {}}

    for packet, direction in flow_packets:
        state = direction_state[direction]
        seq = packet.seq
        consumed = packet.payload_size + (1 if packet.flag("syn") else 0)
        if consumed:
            if seq not in state.seen_seqs:
                state.seen_seqs.add(seq)
                if state.base_seq is None:
                    state.base_seq = seq
                relative = _seq_delta(seq, state.base_seq)
                if state.max_relative_seq is None or relative >= state.max_relative_seq:
                    state.max_relative_seq = relative
            expected_ack = _seq_delta(seq + consumed, 0)
            outstanding[direction][expected_ack] = packet.timestamp

        if packet.flag("ack"):
            other = "down" if direction == "up" else "up"
            outstanding[other].pop(_seq_delta(packet.ack, 0), None)

    return (
        deep_sizeof(direction_state["up"])
        + deep_sizeof(direction_state["down"])
        + deep_sizeof(outstanding["up"])
        + deep_sizeof(outstanding["down"])
    )


def benchmark_session(session_dir, window_seconds):
    """Returns a dict of per-session results, or None if it's a controlled
    experiment:
      flow_identification_sizes: [bytes, ...] one per flow
      retransmission_rtt_sizes: [bytes, ...] one per flow
      flow_identification_total: sum of the above (this session's total)
      retransmission_rtt_total: sum of the above (this session's total)
      window_sizes: {stat: [bytes, ...]} one entry per window
    """
    exp_content = open(os.path.join(session_dir, "exp.txt")).read()
    if is_controlled_experiment(exp_content):
        return None
    label = parse_exp_label(exp_content)

    pcap_path = os.path.join(session_dir, "dump.pcap")
    packets, t0 = _load_and_tag_packets(pcap_path)

    tracker = FlowTracker()
    tagged = []
    flow_packet_lists = defaultdict(list)

    for packet in packets:
        flow_key, direction, _is_new_flow = tracker.process(packet)
        tagged.append(
            {
                "direction": direction,
                "protocol": packet.protocol,
                "wire_len": packet.wire_len,
                "payload_size": packet.payload_size,
                "recv_window": packet.window if packet.protocol == "tcp" else np.nan,
                "timestamp": packet.timestamp,
                "flow_key": flow_key,
            }
        )
        flow_packet_lists[flow_key].append((packet, direction))

    # Per-flow state: flow identification (fixed) + retransmission/RTT
    # tracking (grows with the flow's packet count). One measurement per
    # flow, taken at the point the flow is "done" (all its packets seen).
    # Also collect real RTT samples (compute_flow_metrics) here, same loop,
    # so the rtt_ms window buffer below is bucketed from genuine samples,
    # not re-derived separately.
    flow_identification_sizes = []
    retransmission_rtt_sizes = []
    rtt_samples = []
    for flow_key, flow_state in tracker._flows.items():
        flow_identification_sizes.append(deep_sizeof(flow_state))
    for flow_key, flow_packets in flow_packet_lists.items():
        retransmission_rtt_sizes.append(_flow_state_sizes(flow_packets))
        _flags, rtts = compute_flow_metrics(flow_packets)
        rtt_samples.extend(rtts)

    packet_df = pd.DataFrame(tagged)
    packet_df["window_index"] = ((packet_df["timestamp"] - t0) // window_seconds).astype(int)

    rtt_by_window = defaultdict(list)
    for ts, rtt_ms in rtt_samples:
        rtt_by_window[int((ts - t0) // window_seconds)].append(rtt_ms)

    requests_path = os.path.join(session_dir, "requests_history.json")
    requests = load_relevant_requests(requests_path, label) if os.path.isfile(requests_path) else []

    num_windows = int(packet_df["window_index"].max()) + 1
    window_sizes = defaultdict(list)

    for window_index in range(num_windows):
        window_packets = packet_df[packet_df["window_index"] == window_index]
        window_total_bytes = 0

        for prefix, mask in (
            ("up", window_packets["direction"] == "up"),
            ("down", window_packets["direction"] == "down"),
            ("total", pd.Series(True, index=window_packets.index)),
        ):
            group = window_packets[mask]
            interarrival_ms = group["timestamp"].sort_values().diff().dropna() * 1000
            for metric_name, values in (
                ("payload_size", group["payload_size"].tolist()),
                ("packet_size", group["wire_len"].tolist()),
                ("recv_window", group["recv_window"].dropna().tolist()),
                ("interarrival_time", interarrival_ms.tolist()),
            ):
                size = deep_sizeof(values)
                window_sizes[f"{prefix}_{metric_name}_buffer"].append(size)
                # "total" duplicates up+down's packets into its own buffer
                # (that's the real memory cost of building the combined
                # view too) but must only be counted once toward the
                # window's simultaneous total, not three times over.
                if prefix == "total":
                    window_total_bytes += size

        # rtt_ms is window-level, not direction-split (see project rules),
        # so it's measured once per window rather than once per direction.
        rtt_size = deep_sizeof(rtt_by_window.get(window_index, []))
        window_sizes["rtt_ms_buffer"].append(rtt_size)
        window_total_bytes += rtt_size

        window_start = t0 + window_index * window_seconds
        window_end = window_start + window_seconds
        in_window = [r for r in requests if window_start <= r.start_s < window_end]
        sizes = [r.content_length for r in in_window if r.content_length is not None]
        durations = [r.duration_ms for r in in_window if r.duration_ms is not None]
        segment_size = deep_sizeof(sizes)
        duration_size = deep_sizeof(durations)
        window_sizes["segment_size_bytes_buffer"].append(segment_size)
        window_sizes["request_duration_ms_buffer"].append(duration_size)
        window_total_bytes += segment_size + duration_size

        window_sizes["total_window_buffer (simultaneous)"].append(window_total_bytes)

    return {
        "flow_identification_sizes": flow_identification_sizes,
        "retransmission_rtt_sizes": retransmission_rtt_sizes,
        "flow_identification_total": sum(flow_identification_sizes),
        "retransmission_rtt_total": sum(retransmission_rtt_sizes),
        "window_sizes": dict(window_sizes),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="/mnt/NAS/salmo-trutta/traces")
    parser.add_argument("--num-sessions", type=int, default=100)
    parser.add_argument("--window-seconds", type=float, default=10)
    args = parser.parse_args()

    candidates = discover_session_directories(root=args.source_dir, batch_index=1, batch_size=args.num_sessions * 2)

    all_flow_identification = []
    all_retransmission_rtt = []
    all_flow_identification_total = []  # one value per session
    all_retransmission_rtt_total = []  # one value per session
    all_window_sizes = defaultdict(list)
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

        benchmarked += 1
        all_flow_identification.extend(result["flow_identification_sizes"])
        all_retransmission_rtt.extend(result["retransmission_rtt_sizes"])
        all_flow_identification_total.append(result["flow_identification_total"])
        all_retransmission_rtt_total.append(result["retransmission_rtt_total"])
        for stat, values in result["window_sizes"].items():
            all_window_sizes[stat].extend(values)

        print(f"[{benchmarked}/{args.num_sessions}] {session_dir}: {len(result['flow_identification_sizes'])} flows")

    print(f"\nbenchmarked {benchmarked} sessions, skipped {skipped_controlled} controlled, {failed} failed")

    def stats_row(name, values):
        return (name, np.mean(values), np.percentile(values, 95), np.max(values))

    rows = [
        stats_row("flow_identification_state (per flow)", all_flow_identification),
        stats_row("retransmission_rtt_state (per flow)", all_retransmission_rtt),
        stats_row("flow_identification_state (total per session)", all_flow_identification_total),
        stats_row("retransmission_rtt_state (total per session)", all_retransmission_rtt_total),
    ]
    for stat, values in all_window_sizes.items():
        label = stat if "(simultaneous)" in stat else f"{stat} (per window)"
        rows.append(stats_row(label, values))

    result_df = pd.DataFrame(rows, columns=["stat", "avg_bytes", "p95_bytes", "max_bytes"])
    result_df = result_df.sort_values("avg_bytes", ascending=False).reset_index(drop=True)

    pd.set_option("display.max_rows", None)
    pd.set_option("display.float_format", "{:.1f}".format)
    print("\nmemory per stat (bytes) -- avg / p95 / max across sessions-or-windows:")
    print(result_df.to_string(index=False))

    os.makedirs("benchmarks", exist_ok=True)
    out_path = "benchmarks/feature_extraction_memory_benchmark.csv"
    result_df.to_csv(out_path, index=False)
    print(f"\nsaved to {out_path}")


if __name__ == "__main__":
    main()
