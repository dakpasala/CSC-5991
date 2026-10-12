"""Compute the flow-level feature table from a single Flow's packets.

Everything here operates on one flow's full packet list, over its entire
duration -- no time-windowing, per the project's scope (see AGENTS.md).

Network- and transport-layer features (throughput, flag counts, RTT,
retransmissions, window size, bytes in flight) are computed directly from
packet headers and are exact, not inferred. Application-layer features
(segment sizes, request/completion inter-arrivals, pending/downloaded/
requested counts) have no real HTTP/QUIC framing to read from, since these
are header-only captures -- they are inferred from bursts of downstream data
packets, documented in `infer_segments` below. Treat them as an approximation,
not ground truth, and revisit the thresholds once a capture with real payload
is available.
"""

from dataclasses import dataclass

from flow_builder import direction

SEGMENT_GAP_THRESHOLD_S = 0.05
SEQ_MODULUS = 2**32


@dataclass
class SegmentEvent:
    start: float
    end: float
    byte_count: int


def _split_by_direction(flow):
    up, down = [], []
    for packet in flow.packets:
        (up if direction(flow, packet) == "up" else down).append(packet)
    return up, down


def _interarrivals_ms(packets):
    return _gaps_ms([p.timestamp for p in packets])


def _gaps_ms(timestamps):
    if len(timestamps) < 2:
        return []
    return [(b - a) * 1000 for a, b in zip(timestamps, timestamps[1:])]


def _mean(values):
    return sum(values) / len(values) if values else None


def _seq_delta(later, earlier):
    """Sequence-number difference handling 32-bit wraparound."""
    return (later - earlier) % SEQ_MODULUS


def _flag_counts(packets):
    counts = {name: 0 for name, _ in (
        ("syn", None), ("ack", None), ("rst", None), ("psh", None), ("urg", None)
    )}
    for packet in packets:
        for name in counts:
            if packet.flag(name):
                counts[name] += 1
    return counts


def _rtt_samples_ms(up, down):
    """Match each direction's data/SYN byte against the other direction's ACK
    number to estimate round-trip time. A SYN consumes one sequence number,
    so this naturally covers the handshake as well as later data transfer."""
    samples = []
    for sent, acks in ((up, down), (down, up)):
        outstanding = {}
        for packet in sent:
            consumed = packet.data_len + (1 if packet.flag("syn") else 0)
            if consumed:
                outstanding[_seq_delta(packet.seq + consumed, 0)] = packet.timestamp
        for packet in acks:
            if not packet.flag("ack"):
                continue
            sent_ts = outstanding.pop(_seq_delta(packet.ack, 0), None)
            if sent_ts is not None:
                samples.append((packet.timestamp - sent_ts) * 1000)
    return samples


def _retransmissions_and_out_of_order(packets):
    seen_seqs = set()
    retransmissions = 0
    out_of_order = 0
    max_seq_relative = None
    base_seq = packets[0].seq if packets else 0
    for packet in packets:
        seq = packet.seq
        if seq in seen_seqs:
            retransmissions += 1
            continue
        seen_seqs.add(seq)
        relative = _seq_delta(seq, base_seq)
        if max_seq_relative is not None and relative < max_seq_relative:
            out_of_order += 1
        else:
            max_seq_relative = relative
    return retransmissions, out_of_order


def _bytes_in_flight_max(sent, acked_by):
    max_sent_relative = 0
    max_acked_relative = 0
    peak = 0
    base_seq = sent[0].seq if sent else 0
    ack_iter = iter(sorted(acked_by, key=lambda p: p.timestamp))
    next_ack = next(ack_iter, None)
    for packet in sorted(sent, key=lambda p: p.timestamp):
        end = _seq_delta(packet.seq + max(packet.data_len, 1), base_seq)
        max_sent_relative = max(max_sent_relative, end)
        while next_ack is not None and next_ack.timestamp <= packet.timestamp:
            if next_ack.flag("ack"):
                max_acked_relative = max(
                    max_acked_relative, _seq_delta(next_ack.ack, base_seq)
                )
            next_ack = next(ack_iter, None)
        peak = max(peak, max_sent_relative - max_acked_relative)
    return peak


def infer_segments(down_packets, gap_threshold_s=SEGMENT_GAP_THRESHOLD_S):
    """Group downstream data-carrying packets into bursts separated by idle
    gaps, treating each burst as one inferred "segment" download. Requires
    real payload bytes (data_len > 0) to produce anything -- on a header-only,
    handshake-only capture this correctly returns an empty list."""
    data_packets = [p for p in down_packets if p.data_len > 0]
    if not data_packets:
        return []
    segments = []
    current_start = data_packets[0].timestamp
    current_end = data_packets[0].timestamp
    current_bytes = data_packets[0].data_len
    for packet in data_packets[1:]:
        if packet.timestamp - current_end > gap_threshold_s:
            segments.append(SegmentEvent(current_start, current_end, current_bytes))
            current_start = packet.timestamp
            current_bytes = 0
        current_end = packet.timestamp
        current_bytes += packet.data_len
    segments.append(SegmentEvent(current_start, current_end, current_bytes))
    return segments


def compute_flow_features(flow):
    up, down = _split_by_direction(flow)
    all_packets = flow.packets
    duration_s = all_packets[-1].timestamp - all_packets[0].timestamp
    duration_s = duration_s if duration_s > 0 else None

    up_bytes = sum(p.wire_len for p in up)
    down_bytes = sum(p.wire_len for p in down)
    up_goodput_bytes = sum(p.data_len for p in up)
    down_goodput_bytes = sum(p.data_len for p in down)

    up_flags = _flag_counts(up)
    down_flags = _flag_counts(down)

    up_retrans, up_ooo = _retransmissions_and_out_of_order(up)
    down_retrans, down_ooo = _retransmissions_and_out_of_order(down)

    rtt_samples = _rtt_samples_ms(up, down)
    segments = infer_segments(down)
    segment_sizes = [s.byte_count for s in segments]
    completion_gaps = _gaps_ms([s.end for s in segments])

    up_requests = [p for p in up if p.data_len > 0]
    request_gaps = _interarrivals_ms(up_requests)
    num_requested = len(up_requests)
    num_downloaded = len(segments)

    def bps(byte_total):
        return (byte_total * 8 / duration_s) if duration_s else None

    return {
        "session_id": flow.session_id,
        "label": flow.label,
        "client_ip": flow.client_ip,
        "client_port": flow.client_port,
        "server_ip": flow.server_ip,
        "server_port": flow.server_port,
        "duration_s": duration_s,
        # network layer
        "packet_count_up": len(up),
        "packet_count_down": len(down),
        "byte_count_up": up_bytes,
        "byte_count_down": down_bytes,
        "throughput_up_bps": bps(up_bytes),
        "throughput_down_bps": bps(down_bytes),
        "packet_interarrival_up_mean_ms": _mean(_interarrivals_ms(up)),
        "packet_interarrival_down_mean_ms": _mean(_interarrivals_ms(down)),
        # transport layer
        "syn_count_up": up_flags["syn"],
        "syn_count_down": down_flags["syn"],
        "ack_count_up": up_flags["ack"],
        "ack_count_down": down_flags["ack"],
        "rst_count_up": up_flags["rst"],
        "rst_count_down": down_flags["rst"],
        "psh_count_up": up_flags["psh"],
        "psh_count_down": down_flags["psh"],
        "urg_count_up": up_flags["urg"],
        "urg_count_down": down_flags["urg"],
        "recv_window_up_mean": _mean([p.window for p in up]),
        "recv_window_down_mean": _mean([p.window for p in down]),
        "idle_time_up_max_ms": max(_interarrivals_ms(up), default=None),
        "idle_time_down_max_ms": max(_interarrivals_ms(down), default=None),
        "goodput_up_bps": bps(up_goodput_bytes),
        "goodput_down_bps": bps(down_goodput_bytes),
        "bytes_per_packet_up_mean": _mean([p.wire_len for p in up]),
        "bytes_per_packet_down_mean": _mean([p.wire_len for p in down]),
        "round_trip_time_ms": _mean(rtt_samples),
        "bytes_in_flight_up_max": _bytes_in_flight_max(up, down),
        "bytes_in_flight_down_max": _bytes_in_flight_max(down, up),
        "retransmission_count_up": up_retrans,
        "retransmission_count_down": down_retrans,
        "out_of_order_count_up": up_ooo,
        "out_of_order_count_down": down_ooo,
        # application layer (inferred, see infer_segments docstring)
        "segment_size_mean_bytes": _mean(segment_sizes),
        "segment_size_last10_mean_bytes": _mean(segment_sizes[-10:]),
        "segment_size_cumulative_bytes": sum(segment_sizes) or None,
        "segment_request_interarrival_mean_ms": _mean(request_gaps),
        "segment_completion_interarrival_mean_ms": _mean(completion_gaps),
        "num_requested_segments": num_requested,
        "num_downloaded_segments": num_downloaded,
        "num_pending_requests": max(num_requested - num_downloaded, 0),
    }
