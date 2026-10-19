"""Ethernet/IPv4/TCP-or-UDP field extraction for one captured packet.

Payload size is computed from the header-declared lengths (IP total length,
IP header length, TCP/UDP header length), not from how many payload bytes
actually survived the capture's snaplen truncation -- the latter would be
silently wrong for any packet whose true size exceeds the snaplen (verified:
~50% of packets in a sampled check of dump.pcap, see pcap_reader.py).
"""

from dataclasses import dataclass

import dpkt

ETHERNET_HEADER_LEN = 14
UDP_HEADER_LEN = 8

TCP_FLAG_BITS = (
    ("fin", dpkt.tcp.TH_FIN),
    ("syn", dpkt.tcp.TH_SYN),
    ("rst", dpkt.tcp.TH_RST),
    ("psh", dpkt.tcp.TH_PUSH),
    ("ack", dpkt.tcp.TH_ACK),
    ("urg", dpkt.tcp.TH_URG),
)


@dataclass(frozen=True)
class Packet:
    timestamp: float
    protocol: str  # "tcp" or "udp"
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    wire_len: int
    payload_size: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    window: int = 0

    def flag(self, name):
        return bool(self.flags & dict(TCP_FLAG_BITS)[name])


def parse_packet(raw_packet):
    """Return a Packet, or None if it isn't Ethernet/IPv4/TCP-or-UDP."""
    try:
        eth = dpkt.ethernet.Ethernet(raw_packet.data)
        ip = eth.data
        if not isinstance(ip, dpkt.ip.IP):
            return None
        transport = ip.data
    except (dpkt.UnpackError, dpkt.NeedData, IndexError):
        return None

    ip_header_len = ip.hl * 4

    if isinstance(transport, dpkt.tcp.TCP):
        tcp_header_len = transport.off * 4
        payload_size = max(
            raw_packet.wire_len - ETHERNET_HEADER_LEN - ip_header_len - tcp_header_len, 0
        )
        return Packet(
            timestamp=raw_packet.timestamp,
            protocol="tcp",
            src_ip=dpkt.utils.inet_to_str(ip.src),
            dst_ip=dpkt.utils.inet_to_str(ip.dst),
            src_port=transport.sport,
            dst_port=transport.dport,
            wire_len=raw_packet.wire_len,
            payload_size=payload_size,
            seq=transport.seq,
            ack=transport.ack,
            flags=transport.flags,
            window=transport.win,
        )

    if isinstance(transport, dpkt.udp.UDP):
        payload_size = max(
            raw_packet.wire_len - ETHERNET_HEADER_LEN - ip_header_len - UDP_HEADER_LEN, 0
        )
        return Packet(
            timestamp=raw_packet.timestamp,
            protocol="udp",
            src_ip=dpkt.utils.inet_to_str(ip.src),
            dst_ip=dpkt.utils.inet_to_str(ip.dst),
            src_port=transport.sport,
            dst_port=transport.dport,
            wire_len=raw_packet.wire_len,
            payload_size=payload_size,
        )

    return None
