"""Identify flows (5-tuple, direction-canonicalized) across a stream of
packets, and determine which side of each flow is the client.

For TCP, the client is whoever sent the pure SYN (no ACK) for that 5-tuple --
authoritative when seen, so it always overrides an earlier provisional guess.
UDP has no handshake, so the client is taken to be whoever sent that 5-tuple's
first packet; this is a real assumption (there's no equivalent of a SYN to
confirm it), documented here rather than presented as certain.
"""

from dataclasses import dataclass

FlowKey = tuple[str, int, str, int, str]  # (ip_a, port_a, ip_b, port_b, protocol)


@dataclass
class FlowState:
    client_ip: str
    client_port: int
    server_ip: str
    server_port: int


class FlowTracker:
    def __init__(self):
        self._flows: dict[FlowKey, FlowState] = {}

    @staticmethod
    def _canonical_key(packet) -> FlowKey:
        a = (packet.src_ip, packet.src_port)
        b = (packet.dst_ip, packet.dst_port)
        lo, hi = (a, b) if a <= b else (b, a)
        return (lo[0], lo[1], hi[0], hi[1], packet.protocol)

    def process(self, packet):
        """Call once per packet, in chronological order. Returns
        (flow_key, direction, is_new_flow) where direction is "up" if the
        packet travels client -> server, else "down". Deliberately
        window-size-independent -- flow identity doesn't depend on how the
        caller later buckets packets into time windows, so this can be
        computed once per session and reused across every window size."""
        key = self._canonical_key(packet)
        is_pure_syn = packet.protocol == "tcp" and packet.flag("syn") and not packet.flag("ack")

        state = self._flows.get(key)
        if state is None:
            state = FlowState(
                client_ip=packet.src_ip,
                client_port=packet.src_port,
                server_ip=packet.dst_ip,
                server_port=packet.dst_port,
            )
            self._flows[key] = state
            is_new_flow = True
        else:
            is_new_flow = False
            if is_pure_syn:
                # Authoritative: a real SYN always wins over an earlier guess.
                state.client_ip, state.client_port = packet.src_ip, packet.src_port
                state.server_ip, state.server_port = packet.dst_ip, packet.dst_port

        direction = (
            "up" if (packet.src_ip, packet.src_port) == (state.client_ip, state.client_port)
            else "down"
        )
        return key, direction, is_new_flow
