"""Per-flow retransmission, out-of-order, and RTT detection.

These need a flow's full chronological packet history, not just one window's
worth -- a packet in window 5 can be a retransmission of something sent in
window 2. compute_flow_metrics takes one flow's complete (packet, direction)
list for the whole session and tags each packet individually, so the caller
can attribute each event to the correct window afterward.

UDP carries no TCP sequence/ack numbers, so none of this applies to it --
those flows simply produce no retransmission/out-of-order/RTT signal, same
as a real no-DPI observer would see for QUIC's encrypted internals.

Sequence-number arithmetic uses 32-bit modular subtraction throughout, to
handle wraparound correctly on long-running flows.
"""

SEQ_MODULUS = 2**32


def _seq_delta(later, earlier):
    return (later - earlier) % SEQ_MODULUS


class _DirectionState:
    def __init__(self):
        self.seen_seqs = set()
        self.base_seq = None
        self.max_relative_seq = None


def compute_flow_metrics(flow_packets):
    """flow_packets: list of (Packet, direction) tuples for one flow, sorted
    by timestamp. Returns (event_flags, rtt_samples):
      event_flags: list, same order/length as flow_packets, of
        (is_retransmission, is_out_of_order) tuples.
      rtt_samples: list of (ack_timestamp, rtt_ms) tuples.
    """
    if not flow_packets or flow_packets[0][0].protocol != "tcp":
        return [(False, False) for _ in flow_packets], []

    direction_state = {"up": _DirectionState(), "down": _DirectionState()}
    outstanding = {"up": {}, "down": {}}  # expected_ack_seq -> sent_timestamp
    event_flags = []
    rtt_samples = []

    for packet, direction in flow_packets:
        state = direction_state[direction]
        seq = packet.seq
        is_retransmission = False
        is_out_of_order = False

        # A pure ACK (no payload, not a SYN) doesn't consume sequence space,
        # so its sender legitimately resends the exact same seq on every
        # such packet -- that's normal, not a retransmission. Only packets
        # that actually consume sequence space are meaningful to check here.
        consumed = packet.payload_size + (1 if packet.flag("syn") else 0)
        if consumed:
            if seq in state.seen_seqs:
                is_retransmission = True
            else:
                state.seen_seqs.add(seq)
                if state.base_seq is None:
                    state.base_seq = seq
                relative = _seq_delta(seq, state.base_seq)
                if state.max_relative_seq is not None and relative < state.max_relative_seq:
                    is_out_of_order = True
                else:
                    state.max_relative_seq = relative

            # RTT: record what this packet implies the sender expects to be
            # acked, then look for the other direction acking it.
            expected_ack = _seq_delta(seq + consumed, 0)
            outstanding[direction][expected_ack] = packet.timestamp

        event_flags.append((is_retransmission, is_out_of_order))

        if packet.flag("ack"):
            other = "down" if direction == "up" else "up"
            sent_ts = outstanding[other].pop(_seq_delta(packet.ack, 0), None)
            if sent_ts is not None:
                rtt_samples.append((packet.timestamp, (packet.timestamp - sent_ts) * 1000))

    return event_flags, rtt_samples
