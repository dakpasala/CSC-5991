"""Benchmark the MEMORY footprint of each feature-extraction stat, averaged
across many real sessions -- this replaced an earlier CPU-cycles framing
(see benchmark_feature_costs.py) after discussion with the professor: for
flow/window-level stats, the thing a router actually has to worry about is
how much state it must hold in memory, not how many cycles a calculation
takes. Two columns out: stat name, average bytes held to compute it,
printed and saved to feature_extraction_memory_benchmark.csv.

What "memory to compute this stat" means, per row:
  - Flow identification state (FlowTracker.FlowState): fixed-size, doesn't
    grow with packets -- measured once per flow.
  - Retransmission/RTT tracking state (flow_metrics' internal
    _DirectionState.seen_seqs set + the outstanding ack-tracking dict):
    THIS is the interesting one -- seen_seqs adds one entry per
    data-carrying packet for the flow's entire life and is never pruned,
    so it genuinely grows with how much that flow has carried. Measured
    once per flow, at the point the flow finishes (its peak size).
  - Every distribution-style metric (payload_size, packet_size,
    recv_window, interarrival_time, rtt_ms, segment_size_bytes,
    request_duration_ms): the memory cost of the RAW BUFFERED LIST of
    values that must be held before _distribution() can compute min/max/
    median/percentiles -- these can't be computed with an O(1) running
    accumulator the way a sum or count can, so the full list really has
    to sit in memory until the window closes. Measured once per window.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # core pipeline modules live in src/

from build_session_csv import discover_session_directories, parse_exp_label  # noqa: E402
from find_controlled_sessions import is_controlled_experiment  # noqa: E402
from flow_metrics import SEQ_MODULUS, _DirectionState, _seq_delta  # noqa: E402
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
    """Returns (flow_state_sizes: [bytes, ...], window_sizes: {stat: [bytes, ...]})
    for this one session, or None if it's a controlled experiment."""
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
    flow_identification_sizes = []
    retransmission_rtt_sizes = []
    for flow_key, flow_state in tracker._flows.items():
        flow_identification_sizes.append(deep_sizeof(flow_state))
    for flow_key, flow_packets in flow_packet_lists.items():
        retransmission_rtt_sizes.append(_flow_state_sizes(flow_packets))

    packet_df = pd.DataFrame(tagged)
    packet_df["window_index"] = ((packet_df["timestamp"] - t0) // window_seconds).astype(int)

    requests_path = os.path.join(session_dir, "requests_history.json")
    requests = load_relevant_requests(requests_path, label) if os.path.isfile(requests_path) else []

    num_windows = int(packet_df["window_index"].max()) + 1
    window_sizes = defaultdict(list)

    for window_index in range(num_windows):
        window_packets = packet_df[packet_df["window_index"] == window_index]

        for _prefix, mask in (
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
                window_sizes[f"{metric_name}_buffer"].append(deep_sizeof(values))

        window_start = t0 + window_index * window_seconds
        window_end = window_start + window_seconds
        in_window = [r for r in requests if window_start <= r.start_s < window_end]
        sizes = [r.content_length for r in in_window if r.content_length is not None]
        durations = [r.duration_ms for r in in_window if r.duration_ms is not None]
        window_sizes["segment_size_bytes_buffer"].append(deep_sizeof(sizes))
        window_sizes["request_duration_ms_buffer"].append(deep_sizeof(durations))

    return flow_identification_sizes, retransmission_rtt_sizes, dict(window_sizes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="/mnt/NAS/salmo-trutta/traces")
    parser.add_argument("--num-sessions", type=int, default=100)
    parser.add_argument("--window-seconds", type=float, default=10)
    args = parser.parse_args()

    candidates = discover_session_directories(root=args.source_dir, batch_index=1, batch_size=args.num_sessions * 2)

    all_flow_identification = []
    all_retransmission_rtt = []
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

        flow_id_sizes, retrans_sizes, window_sizes = result
        benchmarked += 1
        all_flow_identification.extend(flow_id_sizes)
        all_retransmission_rtt.extend(retrans_sizes)
        for stat, values in window_sizes.items():
            all_window_sizes[stat].extend(values)

        print(f"[{benchmarked}/{args.num_sessions}] {session_dir}: {len(flow_id_sizes)} flows")

    print(f"\nbenchmarked {benchmarked} sessions, skipped {skipped_controlled} controlled, {failed} failed")

    rows = [
        ("flow_identification_state (per flow)", np.mean(all_flow_identification)),
        ("retransmission_rtt_state (per flow)", np.mean(all_retransmission_rtt)),
    ]
    for stat, values in all_window_sizes.items():
        rows.append((f"{stat} (per window)", np.mean(values)))

    result_df = pd.DataFrame(rows, columns=["stat", "avg_bytes"]).sort_values("avg_bytes", ascending=False)
    result_df = result_df.reset_index(drop=True)

    pd.set_option("display.max_rows", None)
    pd.set_option("display.float_format", "{:.1f}".format)
    print("\naverage memory per stat (bytes):")
    print(result_df.to_string(index=False))

    out_path = "feature_extraction_memory_benchmark.csv"
    result_df.to_csv(out_path, index=False)
    print(f"\nsaved to {out_path}")


if __name__ == "__main__":
    main()
