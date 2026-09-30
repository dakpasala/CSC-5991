"""Print every packet of one session in human-readable form, for spot-checking
build_dataset.py's output against the raw capture.

dpkt is used for the bulk extraction pipeline because it is far faster over
millions of packets; scapy is used here instead because its packet summaries
and layer breakdowns are much more readable for manually verifying a single
session, which is exactly what this script is for.

Usage:
    python src/inspect_session.py traffic.pcapng <session_id>
"""

import argparse

from scapy.all import Ether

from pcapng_reader import read_packets


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcapng_path")
    parser.add_argument("session_id")
    return parser.parse_args()


def main():
    args = parse_args()
    matched = 0
    for raw in read_packets(args.pcapng_path):
        if raw.session_id != args.session_id:
            continue
        matched += 1
        packet = Ether(raw.data)
        print(f"[{raw.timestamp:.6f}] label={raw.label}")
        print(packet.show(dump=True))
    if matched == 0:
        print(f"No packets found for session_id={args.session_id!r}")


if __name__ == "__main__":
    main()
