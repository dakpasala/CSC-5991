"""Ethernet/IPv4/TCP field extraction for a single captured packet."""

from dataclasses import dataclass

import dpkt

FLAG_BITS = (
    ("fin", dpkt.tcp.TH_FIN),
    ("syn", dpkt.tcp.TH_SYN),
    ("rst", dpkt.tcp.TH_RST),
    ("psh", dpkt.tcp.TH_PUSH),
    ("ack", dpkt.tcp.TH_ACK),
    ("urg", dpkt.tcp.TH_URG),
)


@dataclass(frozen=True)
class TcpPacket:
    timestamp: float
    session_id: str
    label: str
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    seq: int
    ack: int
    flags: int
    window: int
    wire_len: int
    data_len: int

    def flag(self, name):
        bit = dict(FLAG_BITS)[name]
        return bool(self.flags & bit)


def parse_tcp_packet(raw_packet):
    """Return a TcpPacket, or None if the packet isn't Ethernet/IPv4/TCP or
    lacks a session label."""
    if raw_packet.session_id is None:
        return None
    try:
        eth = dpkt.ethernet.Ethernet(raw_packet.data)
        ip = eth.data
        if not isinstance(ip, dpkt.ip.IP):
            return None
        tcp = ip.data
        if not isinstance(tcp, dpkt.tcp.TCP):
            return None
    except (dpkt.UnpackError, dpkt.NeedData, IndexError):
        return None

    return TcpPacket(
        timestamp=raw_packet.timestamp,
        session_id=raw_packet.session_id,
        label=raw_packet.label,
        src_ip=dpkt.utils.inet_to_str(ip.src),
        dst_ip=dpkt.utils.inet_to_str(ip.dst),
        src_port=tcp.sport,
        dst_port=tcp.dport,
        seq=tcp.seq,
        ack=tcp.ack,
        flags=tcp.flags,
        window=tcp.win,
        wire_len=ip.len + 14,
        data_len=len(tcp.data),
    )
