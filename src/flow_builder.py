"""Group parsed TCP packets into sessions, and sessions into flows.

A session is every packet sharing one frame-comment session ID (can span
several concurrent connections, e.g. parallel requests to different CDN
edges). A flow is one TCP connection within a session, identified by its
4-tuple and treated bidirectionally. Each 4-tuple is assumed to represent a
single connection attempt within a session; a 4-tuple reused for a second,
separate connection later in the same session would be merged into one flow
rather than split -- acceptable for this dataset, worth revisiting if a
future capture shows real connection reuse.
"""

from collections import defaultdict
from dataclasses import dataclass, field

FlowKey = tuple[str, int, str, int]


@dataclass
class Flow:
    session_id: str
    label: str
    client_ip: str
    client_port: int
    server_ip: str
    server_port: int
    packets: list = field(default_factory=list)

    @property
    def key(self) -> FlowKey:
        return (self.client_ip, self.client_port, self.server_ip, self.server_port)


@dataclass
class Session:
    session_id: str
    label: str
    flows: dict = field(default_factory=dict)


def _endpoints(packet):
    return (packet.src_ip, packet.src_port, packet.dst_ip, packet.dst_port)


def _canonical_key(packet):
    a = (packet.src_ip, packet.src_port)
    b = (packet.dst_ip, packet.dst_port)
    return (a, b) if a <= b else (b, a)


def build_sessions(packets):
    """Consume an iterable of TcpPacket and return {session_id: Session}."""
    sessions = {}
    client_sides = {}

    for packet in packets:
        session = sessions.setdefault(
            packet.session_id, Session(packet.session_id, packet.label)
        )
        canon_key = _canonical_key(packet)

        is_pure_syn = packet.flag("syn") and not packet.flag("ack")
        if is_pure_syn or canon_key not in client_sides:
            # A pure SYN authoritatively identifies the client and always wins;
            # otherwise this is a provisional guess, kept only until a real SYN
            # for this 4-tuple is seen (packets aren't read in session order).
            client_sides[canon_key] = (
                packet.src_ip,
                packet.src_port,
                packet.dst_ip,
                packet.dst_port,
            )
        client_ip, client_port, server_ip, server_port = client_sides[canon_key]

        flow = session.flows.setdefault(
            canon_key,
            Flow(
                session_id=packet.session_id,
                label=packet.label,
                client_ip=client_ip,
                client_port=client_port,
                server_ip=server_ip,
                server_port=server_port,
            ),
        )
        # Keep the flow's client/server in sync in case a later pure SYN
        # corrects a provisional guess made from an earlier, non-SYN packet.
        flow.client_ip, flow.client_port = client_ip, client_port
        flow.server_ip, flow.server_port = server_ip, server_port
        flow.packets.append(packet)

    for session in sessions.values():
        for flow in session.flows.values():
            flow.packets.sort(key=lambda p: p.timestamp)

    return sessions


def direction(flow, packet):
    """'up' if packet travels client -> server, else 'down'."""
    is_from_client = (
        packet.src_ip == flow.client_ip and packet.src_port == flow.client_port
    )
    return "up" if is_from_client else "down"
