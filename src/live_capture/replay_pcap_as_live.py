"""Replay an existing session's dump.pcap as if it were arriving live, in
real wall-clock time -- to test whether the feature-extraction pipeline can
keep up with real-time window boundaries without ever missing one, WITHOUT
needing true network capture (no root/sudo, no raw sockets, nothing that
could touch anyone else's traffic on a shared machine). Works entirely from
an already-recorded pcap, so this needs no special privileges at all.

Two threads, same decoupling idea as the true live-capture pipeline
(src/live_capture_pipeline.py), just without an actual network interface:

  - a REPLAYER thread reads packets from the pcap in order and delivers
    them onto a queue, paced at their ORIGINAL relative timing (scaled by
    --speed; 1.0 = faithful real-time, exactly as fast as it was actually
    captured).
  - the MAIN thread continuously drains that queue (so per-packet flow
    tracking happens close to each packet's simulated arrival time, not
    batched). Window boundaries are decided by PACKET TIMESTAMPS, not
    wall-clock polling: the moment a packet's own timestamp crosses into
    the next window_seconds bucket, the just-completed window is closed,
    its stats computed and written, and accumulation starts fresh for the
    new window -- while the replayer keeps delivering the NEXT window's
    packets onto the queue concurrently the whole time, so a slow
    computation can never cause a miss (packets just queue up and get
    drained the moment the main thread is free again; Queue is unbounded).
    Prints how long each window's computation actually took, so you can
    see directly whether it's comfortably within --window-seconds or
    falling behind.

Reuses window_features._build_one_window_row directly -- the same
per-window row-construction logic the batch pipeline uses, just called
once per window as it completes instead of in one pass over a whole
session's packets.

Two known simplifications versus true incremental processing:
  - Retransmission/out-of-order/RTT detection is recomputed on each flow's
    full history-so-far at every window close (not incrementally updated)
    -- correct, but per-window cost grows as the replay progresses and
    flows accumulate more history. A true production streaming
    implementation would want flow_metrics.py's logic made properly
    incremental instead.
  - An RTT sample whose ACK arrives in a LATER window than when its data
    packet was sent gets attributed to whichever window is open when it's
    detected -- if that's a window that already closed and was written,
    the sample is simply not retroactively added to that earlier row.

Usage:
    python src/live_capture/replay_pcap_as_live.py --pcap sessions/<id>/dump.pcap
    python src/live_capture/replay_pcap_as_live.py --pcap sessions/<id>/dump.pcap --speed 4 --window-seconds 10
"""

import argparse
import os
import queue
import sys
import threading
import time
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))  # shared library modules

from flow_metrics import compute_flow_metrics  # noqa: E402
from flow_tracker import FlowTracker  # noqa: E402
from window_features import _build_one_window_row, _load_and_tag_packets  # noqa: E402

LABEL_PLACEHOLDER = "live_unlabeled"

TAGGED_COLUMNS = [
    "direction", "protocol", "wire_len", "payload_size", "recv_window",
    "timestamp", "flags", "is_new_flow", "flow_key",
]


def replayer_thread(packets, speed, out_queue):
    prev_timestamp = packets[0].timestamp
    for packet in packets:
        delay = (packet.timestamp - prev_timestamp) / speed
        if delay > 0:
            time.sleep(delay)
        prev_timestamp = packet.timestamp
        out_queue.put(packet)
    out_queue.put(None)  # sentinel: replay finished


def close_window(window_tagged, flow_packet_lists, flow_cursor, t0, window_index, window_seconds, session_id):
    packet_df = pd.DataFrame(window_tagged, columns=TAGGED_COLUMNS)

    event_flags_by_flow = {}
    rtt_by_window = defaultdict(list)
    for flow_key in {row["flow_key"] for row in window_tagged}:
        flags, rtts = compute_flow_metrics(flow_packet_lists[flow_key])
        event_flags_by_flow[flow_key] = flags
        for ts, rtt_ms in rtts:
            rtt_by_window[int((ts - t0) // window_seconds)].append(rtt_ms)

    # flow_cursor persists ACROSS window closes (passed in, not created
    # fresh here) -- event_flags_by_flow is recomputed on each flow's
    # ENTIRE history every time, so picking out just this window's new
    # packets means continuing from wherever this flow's cursor left off
    # in a previous window, not restarting at 0.
    is_retransmission_col, is_out_of_order_col = [], []
    for row in window_tagged:
        idx = flow_cursor[row["flow_key"]]
        is_retransmission, is_out_of_order = event_flags_by_flow[row["flow_key"]][idx]
        is_retransmission_col.append(is_retransmission)
        is_out_of_order_col.append(is_out_of_order)
        flow_cursor[row["flow_key"]] += 1
    packet_df["is_retransmission"] = is_retransmission_col
    packet_df["is_out_of_order"] = is_out_of_order_col

    flow_first_window = (
        packet_df[packet_df["is_new_flow"]].groupby(lambda _idx: window_index).size()
        if window_tagged and packet_df["is_new_flow"].any()
        else pd.Series(dtype=int)
    )

    row = _build_one_window_row(
        packet_df, flow_first_window, rtt_by_window, window_index, window_seconds,
        LABEL_PLACEHOLDER, session_id, [], t0,
    )
    return pd.DataFrame([row])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pcap", required=True, help="path to an existing session's dump.pcap")
    parser.add_argument("--window-seconds", type=float, default=10)
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier (1.0 = real-time)")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    os.makedirs("live_runs", exist_ok=True)
    output_path = args.output or f"live_runs/replay_statistics_{int(args.window_seconds)}_second_window.csv"
    session_id = os.path.basename(os.path.dirname(args.pcap)) or "replay"

    packets, t0 = _load_and_tag_packets(args.pcap)
    print(f"loaded {len(packets)} packets from {args.pcap}, replaying at {args.speed}x speed")

    pkt_queue = queue.Queue()
    thread = threading.Thread(target=replayer_thread, args=(packets, args.speed, pkt_queue), daemon=True)
    thread.start()

    tracker = FlowTracker()
    flow_packet_lists = defaultdict(list)
    flow_cursor = defaultdict(int)  # persists across windows -- see close_window's docstring note
    window_tagged = []
    current_window_index = 0
    total_rows_written = 0
    replay_done = False
    window_wall_start = time.time()

    def emit(target_window_index):
        nonlocal window_tagged, total_rows_written, window_wall_start
        compute_start = time.time()
        row_df = close_window(
            window_tagged, flow_packet_lists, flow_cursor, t0, target_window_index, args.window_seconds, session_id
        )
        compute_time = time.time() - compute_start

        file_exists = os.path.isfile(output_path)
        row_df.to_csv(output_path, mode="a", header=not file_exists, index=False)
        total_rows_written += len(row_df)

        budget = args.window_seconds / args.speed
        status = "OK" if compute_time < budget else "FELL BEHIND"
        print(
            f"window {target_window_index}: {len(window_tagged)} packets, computed in {compute_time:.3f}s "
            f"(budget {budget:.1f}s) [{status}]"
        )
        window_tagged = []
        window_wall_start = time.time()

    while not (replay_done and pkt_queue.empty()):
        try:
            packet = pkt_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if packet is None:
            replay_done = True
            continue

        pkt_window_index = int((packet.timestamp - t0) // args.window_seconds)
        while pkt_window_index > current_window_index:
            emit(current_window_index)
            current_window_index += 1

        flow_key, direction, is_new_flow = tracker.process(packet)
        window_tagged.append(
            {
                "direction": direction,
                "protocol": packet.protocol,
                "wire_len": packet.wire_len,
                "payload_size": packet.payload_size,
                "recv_window": packet.window if packet.protocol == "tcp" else float("nan"),
                "timestamp": packet.timestamp,
                "flags": packet.flags,
                "is_new_flow": is_new_flow,
                "flow_key": flow_key,
            }
        )
        flow_packet_lists[flow_key].append((packet, direction))

    emit(current_window_index)  # flush the final, partial window

    print(f"\nreplay complete, {total_rows_written} window rows written to {output_path}")


if __name__ == "__main__":
    main()
