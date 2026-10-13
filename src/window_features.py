"""Build the full per-window feature table for one session.

Orchestrates pcap_reader + packet_parser + flow_tracker + flow_metrics +
requests_log, buckets everything into fixed-size time windows, and computes
every column described in AGENTS.md's session-statistics section: direction-
scoped (up/down/total) counts, flag counts, and 6-stat distributions
(min/max/mean/median/p25/p75), window-level flow-novelty and RTT stats, and
request-log-derived application-layer stats.

Parsing the raw pcap and running flow tracking (_parse_and_track_flows) is
the expensive part and is entirely independent of window size -- flow
identity, retransmission/out-of-order/RTT detection, and the youtube_tcp/
youtube_quic label decision don't change based on how packets later get
bucketed into windows. build_session_dataframes() does that expensive work
exactly once per session and reuses the result across every requested
window size, rather than re-parsing the pcap once per window size.
"""

import os
from collections import defaultdict

import numpy as np
import pandas as pd

from flow_metrics import compute_flow_metrics
from flow_tracker import FlowTracker
from packet_parser import parse_packet
from pcap_reader import read_packets
from requests_log import load_relevant_requests

DISTRIBUTION_STATS = ("min", "max", "mean", "median", "p25", "p75")


def _distribution(values):
    if not values:
        return {stat: np.nan for stat in DISTRIBUTION_STATS}
    array = np.asarray(values, dtype=float)
    return {
        "min": array.min(),
        "max": array.max(),
        "mean": array.mean(),
        "median": np.median(array),
        "p25": np.percentile(array, 25),
        "p75": np.percentile(array, 75),
    }


def _load_and_tag_packets(pcap_path):
    """Parse every packet in the pcap and find the session's start time."""
    packets = [p for p in (parse_packet(raw) for raw in read_packets(pcap_path)) if p is not None]
    if not packets:
        raise ValueError(f"no parseable TCP/UDP packets found in {pcap_path}")

    t0 = packets[0].timestamp
    return packets, t0


def _parse_and_track_flows(pcap_path):
    """The expensive, window-size-independent work: parse every packet,
    identify flows, and run retransmission/out-of-order/RTT detection.
    Returns (packet_df, rtt_samples, t0) -- none of this depends on window
    size, so callers can bucket the same result into as many different
    window sizes as needed without repeating this step."""
    packets, t0 = _load_and_tag_packets(pcap_path)

    tracker = FlowTracker()
    tagged = []  # list of dicts, one per packet
    flow_packet_lists = defaultdict(list)  # flow_key -> [(packet, direction), ...]

    for packet in packets:
        flow_key, direction, is_new_flow = tracker.process(packet)
        row = {
            "direction": direction,
            "protocol": packet.protocol,
            "wire_len": packet.wire_len,
            "payload_size": packet.payload_size,
            "recv_window": packet.window if packet.protocol == "tcp" else np.nan,
            "timestamp": packet.timestamp,
            "flags": packet.flags,
            "is_new_flow": is_new_flow,
            "flow_key": flow_key,
        }
        tagged.append(row)
        flow_packet_lists[flow_key].append((packet, direction))

    # Retransmission/out-of-order/RTT: needs each flow's full chronological
    # history, computed separately, then merged back onto the per-packet rows.
    event_flags_by_flow = {}
    rtt_samples = []
    for flow_key, flow_packets in flow_packet_lists.items():
        flags, rtts = compute_flow_metrics(flow_packets)
        event_flags_by_flow[flow_key] = flags
        rtt_samples.extend(rtts)

    flow_cursor = defaultdict(int)
    for row in tagged:
        idx = flow_cursor[row["flow_key"]]
        is_retransmission, is_out_of_order = event_flags_by_flow[row["flow_key"]][idx]
        row["is_retransmission"] = is_retransmission
        row["is_out_of_order"] = is_out_of_order
        flow_cursor[row["flow_key"]] += 1

    return pd.DataFrame(tagged), rtt_samples, t0


def _bucket_into_windows(packet_df, rtt_samples, t0, window_seconds):
    """The cheap, window-size-dependent step: assign a window_index to each
    already-tagged packet (vectorized, not a per-packet Python loop) and
    bucket RTT samples the same way."""
    packet_df = packet_df.copy()
    packet_df["window_index"] = ((packet_df["timestamp"] - t0) // window_seconds).astype(int)

    flow_first_window = packet_df[packet_df["is_new_flow"]].groupby("window_index").size()

    rtt_by_window = defaultdict(list)
    for ts, rtt_ms in rtt_samples:
        rtt_by_window[int((ts - t0) // window_seconds)].append(rtt_ms)

    return packet_df, flow_first_window, rtt_by_window


def _decide_label(label, packet_df):
    if label != "youtube":
        return label
    udp_fraction = (packet_df["protocol"] == "udp").mean()
    return "youtube_quic" if udp_fraction > 0.5 else "youtube_tcp"


TCP_FLAGS = ("syn", "ack", "rst", "psh", "urg")
_FLAG_BIT = {"fin": 0x01, "syn": 0x02, "rst": 0x04, "psh": 0x08, "ack": 0x10, "urg": 0x20}


def _flag_count(flags_series, name):
    bit = _FLAG_BIT[name]
    return int((flags_series.astype(int) & bit).astype(bool).sum())


def _direction_stats(group, prefix, window_seconds):
    stats = {
        f"{prefix}_packet_count": len(group),
        f"{prefix}_byte_count": int(group["wire_len"].sum()),
        f"{prefix}_throughput_bps": group["wire_len"].sum() * 8 / window_seconds,
        f"{prefix}_retransmission_count": int(group["is_retransmission"].sum()),
        f"{prefix}_out_of_order_count": int(group["is_out_of_order"].sum()),
    }
    for flag in TCP_FLAGS:
        stats[f"{prefix}_{flag}_count"] = _flag_count(group["flags"], flag)

    interarrival_ms = group["timestamp"].sort_values().diff().dropna() * 1000
    for metric_name, values in (
        ("payload_size", group["payload_size"].tolist()),
        ("packet_size", group["wire_len"].tolist()),
        ("recv_window", group["recv_window"].dropna().tolist()),
        ("interarrival_time", interarrival_ms.tolist()),
    ):
        for stat, value in _distribution(values).items():
            stats[f"{prefix}_{metric_name}_{stat}"] = value
    return stats


def _request_stats(requests, t0, window_index, window_seconds):
    window_start = t0 + window_index * window_seconds
    window_end = window_start + window_seconds
    in_window = [r for r in requests if window_start <= r.start_s < window_end]

    stats = {"request_count": len(in_window)}
    sizes = [r.content_length for r in in_window if r.content_length is not None]
    durations = [r.duration_ms for r in in_window if r.duration_ms is not None]
    for metric_name, values in (("segment_size_bytes", sizes), ("request_duration_ms", durations)):
        for stat, value in _distribution(values).items():
            stats[f"{metric_name}_{stat}"] = value
    return stats


def _build_window_rows(packet_df, flow_first_window, rtt_by_window, t0, window_seconds, final_label, session_id, requests):
    last_timestamp = packet_df["timestamp"].max()
    num_windows = int((last_timestamp - t0) // window_seconds) + 1

    rows = []
    for window_index in range(num_windows):
        window_packets = packet_df[packet_df["window_index"] == window_index]
        row = {
            "label": final_label,
            "session_id": session_id,
            "window_index": window_index,
            "window_start_s": window_index * window_seconds,
            "window_end_s": (window_index + 1) * window_seconds,
        }

        if len(window_packets) == 0:
            row["num_flows"] = 0
            row["num_new_flows"] = 0
            row["num_continuing_flows"] = 0
            row["frac_udp"] = np.nan
        else:
            row["num_flows"] = window_packets["flow_key"].nunique()
            row["num_new_flows"] = int(flow_first_window.get(window_index, 0))
            row["num_continuing_flows"] = row["num_flows"] - row["num_new_flows"]
            row["frac_udp"] = (window_packets["protocol"] == "udp").mean()

        for prefix, mask in (
            ("up", window_packets["direction"] == "up"),
            ("down", window_packets["direction"] == "down"),
            ("total", pd.Series(True, index=window_packets.index)),
        ):
            row.update(_direction_stats(window_packets[mask], prefix, window_seconds))

        for stat, value in _distribution(rtt_by_window.get(window_index, [])).items():
            row[f"rtt_ms_{stat}"] = value

        row.update(_request_stats(requests, t0, window_index, window_seconds))
        rows.append(row)

    return pd.DataFrame(rows)


def build_session_dataframes(session_dir, window_sizes, label, session_id, requests_path):
    """Compute the per-window feature table for every window size in
    window_sizes, parsing the raw pcap and running flow tracking only once
    -- the expensive part is window-size-independent -- and reusing that
    result across every window size. Returns {window_seconds: DataFrame}."""
    pcap_path = os.path.join(session_dir, "dump.pcap")
    packet_df, rtt_samples, t0 = _parse_and_track_flows(pcap_path)

    final_label = _decide_label(label, packet_df)
    requests = load_relevant_requests(requests_path, final_label) if requests_path else []

    results = {}
    for window_seconds in window_sizes:
        windowed_df, flow_first_window, rtt_by_window = _bucket_into_windows(
            packet_df, rtt_samples, t0, window_seconds
        )
        results[window_seconds] = _build_window_rows(
            windowed_df, flow_first_window, rtt_by_window, t0, window_seconds, final_label, session_id, requests
        )
    return results


def build_session_dataframe(session_dir, window_seconds, label, session_id, requests_path):
    """Single-window-size convenience wrapper around build_session_dataframes."""
    return build_session_dataframes(session_dir, [window_seconds], label, session_id, requests_path)[window_seconds]
