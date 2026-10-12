"""Endian-aware reader for plain (non-pcapng) .pcap files that preserves the
true on-wire packet length, not just the captured/truncated bytes.

dpkt.pcap.Reader's own __iter__ only yields (timestamp, captured_bytes),
discarding the packet record header's `len` field -- the pcap format records
this separately from `caplen` specifically so a snaplen-truncated capture
can still report true packet sizes. Verified against dump.pcap: snaplen=200,
but roughly half of sampled packets have a true length exceeding that, up to
several hundred bytes, and dpkt's per-record header correctly preserves it.
"""

from dataclasses import dataclass

import dpkt


@dataclass(frozen=True)
class RawPacket:
    timestamp: float
    wire_len: int
    data: bytes


def read_packets(path):
    with open(path, "rb") as handle:
        reader = dpkt.pcap.Reader(handle)
        packet_header_cls = reader._Reader__ph
        divisor = reader._divisor
        file_obj = reader._Reader__f

        while True:
            header_bytes = file_obj.read(packet_header_cls.__hdr_len__)
            if not header_bytes:
                return
            header = packet_header_cls(header_bytes)
            data = file_obj.read(header.caplen)
            timestamp = header.tv_sec + (header.tv_usec / divisor)
            yield RawPacket(timestamp, header.len, data)
