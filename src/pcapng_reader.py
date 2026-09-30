"""Streaming reader for pcapng files that preserves per-packet frame comments.

dpkt's own dpkt.pcapng.Reader discards the comment option attached to each
Enhanced Packet Block, so this reimplements its block-walking loop using the
same private setup (byte order, timestamp divisor/offset) but keeps the
comment string alongside each packet. Verified against traffic.pcapng: matches
tshark's frame.comment output and packet/session counts exactly.
"""

from dataclasses import dataclass

import dpkt

_EPB = dpkt.pcapng.PCAPNG_BT_EPB
_OPT_COMMENT = dpkt.pcapng.PCAPNG_OPT_COMMENT


@dataclass(frozen=True)
class RawPacket:
    timestamp: float
    data: bytes
    session_id: str | None
    label: str | None


def _parse_comment(comment):
    if comment is None:
        return None, None
    session_id, _, label = comment.partition(",")
    return session_id, label or None


def read_packets(path):
    """Yield a RawPacket for every Enhanced Packet Block in a pcapng file."""
    with open(path, "rb") as handle:
        reader = dpkt.pcapng.Reader(handle)
        file_obj = reader._Reader__f
        little_endian = reader._Reader__le
        ts_offset = reader._tsoffset
        ts_divisor = reader._divisor
        unpack = dpkt.pcapng.struct_unpack
        block_cls = (
            dpkt.pcapng.EnhancedPacketBlockLE
            if little_endian
            else dpkt.pcapng.EnhancedPacketBlock
        )

        while True:
            header = file_obj.read(8)
            if len(header) < 8:
                return
            block_type, block_len = unpack(
                "<II" if little_endian else ">II", header
            )
            body = header + file_obj.read(block_len - 8)
            if block_type != _EPB:
                continue

            block = block_cls(body)
            timestamp = ts_offset + (
                ((block.ts_high << 32) | block.ts_low) / ts_divisor
            )
            comment = next(
                (opt.text for opt in block.opts if opt.code == _OPT_COMMENT),
                None,
            )
            session_id, label = _parse_comment(comment)
            yield RawPacket(timestamp, block.pkt_data, session_id, label)
