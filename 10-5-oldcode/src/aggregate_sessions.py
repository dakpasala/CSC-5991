"""Collapse streaming_traffic_features.csv's per-10-second-window rows into
one row per session, split into real-world and lab-experiment sessions.

session_id comes in two distinct forms (verified in AGENTS.md): a 32-char
hex hash for real-world/crowdsourced sessions (tied to a real home_id), and a
descriptive string encoding deliberately-shaped network conditions for
controlled lab experiments (e.g. "netflix-0_rate+20mbps+loss+0.01%+25%.run3").
These get written to two separate output files rather than mixed together,
since they represent genuinely different kinds of traffic (organic networks
vs. artificially shaped ones).

True per-flow rollup (what the professor originally described: full-duration
stats per flow, then median/p25/p75 across a session's flows) isn't possible
from this dataset -- individual flow identity was already discarded when it
was built (verified: a window with exactly one active flow still shows
Min != Max on supposedly-per-flow stats, so these are actually per-packet
stats within the window, not per-flow). This rolls up the finer granularity
that *is* available instead: 10-second time-windows within a session.

Three aggregation rules, chosen per column:
  - cumulative: take the last window's value. Verified by checking both that
    a column never meaningfully decreases within a session AND shows a real
    net increase from first to last window -- "never decreases" alone was
    too weak a test (a flat, rarely-changing column passes it trivially
    without being genuinely cumulative).
  - count: sum across windows, for per-window event/byte/packet/flag counts
    where the session total is the meaningful quantity.
  - distribution: median/p25/p75 across windows, for rate- or
    distribution-shaped measurements (RTT, window size, inter-arrival time,
    chunk sizes, bytes in flight) where no single window's value or a sum
    would mean anything.
"""

import argparse
import re

import pandas as pd

SOURCE_DEFAULT = "streaming_traffic_features.csv"
REAL_OUTPUT_DEFAULT = "session_features_real.csv"
LAB_OUTPUT_DEFAULT = "session_features_lab.csv"

HASH_SESSION_ID = re.compile(r"[0-9a-f]{32}")

IDENTIFIER_COLUMNS = ["label", "service", "is_tcp", "home_id", "video_id", "deployment_session_id"]

CUMULATIVE_COLUMNS = [
    "all_prev_down_chunk_iat_max",
    "all_prev_up_chunk_iat_max",
    "allprev_max_chunksize",
    "cumsum_chunksizes",
    "n_prev_down_chunk",
    "n_prev_up_chunk",
    "video_position",
]

COUNT_COLUMNS = [
    "serverByteCount", "userByteCount",
    "serverPacketCount", "userPacketCount",
    "serverSynFlags", "serverAckFlags", "serverRstFlags", "serverPshFlags", "serverUrgFlags", "serverFinFlags",
    "userSynFlags", "userAckFlags", "userRstFlags", "userPshFlags", "userUrgFlags", "userFinFlags",
    "serverOutOfOrderBytes", "serverOutOfOrderPackets", "userOutOfOrderBytes", "userOutOfOrderPackets",
    "n_chunks_down", "n_chunks_up", "n_bitrate_switches", "n_rebufferings",
    "c_bitrate_switches", "c_rebufferings", "c_resolution_switches",
    "cumsum_diff",
    "serverOneRetransmit", "serverTwoRetransmit", "serverXRetransmit", "serverZeroRetransmit",
    "userOneRetransmit", "userTwoRetransmit", "userXRetransmit", "userZeroRetransmit",
]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DEFAULT)
    parser.add_argument("--real-output", default=REAL_OUTPUT_DEFAULT)
    parser.add_argument("--lab-output", default=LAB_OUTPUT_DEFAULT)
    return parser.parse_args()


def split_session_types(df):
    is_real = df["session_id"].str.fullmatch(HASH_SESSION_ID)
    return df[is_real], df[~is_real]


def aggregate_sessions(df):
    numeric_columns = df.select_dtypes(include="number").columns.tolist()
    distribution_columns = [
        c for c in numeric_columns
        if c not in CUMULATIVE_COLUMNS and c not in COUNT_COLUMNS
    ]

    grouped = df.sort_values("relative_timestamp").groupby("session_id")

    identifiers = grouped[IDENTIFIER_COLUMNS].first()
    num_windows = grouped.size().rename("num_windows")

    cumulative = grouped[CUMULATIVE_COLUMNS].last()
    cumulative.columns = [f"{c}_final" for c in cumulative.columns]

    counts = grouped[COUNT_COLUMNS].sum()
    counts.columns = [f"{c}_total" for c in counts.columns]

    named_aggregations = {
        f"{column}_{stat_name}": pd.NamedAgg(column=column, aggfunc=aggfunc)
        for column in distribution_columns
        for stat_name, aggfunc in (
            ("median", "median"),
            ("p25", lambda s: s.quantile(0.25)),
            ("p75", lambda s: s.quantile(0.75)),
        )
    }
    distributions = grouped.agg(**named_aggregations)

    session_duration_s = (num_windows * 10).rename("session_duration_s")
    session_df = pd.concat(
        [identifiers, num_windows, session_duration_s, cumulative, counts, distributions],
        axis=1,
    )
    return session_df.reset_index()


def main():
    args = parse_args()

    df = pd.read_csv(args.source, dtype={"session_id": str}, low_memory=False)
    real_df, lab_df = split_session_types(df)

    real_sessions = aggregate_sessions(real_df)
    real_sessions.to_csv(args.real_output, index=False)

    lab_sessions = aggregate_sessions(lab_df)
    lab_sessions.to_csv(args.lab_output, index=False)

    print(f"real-world: {len(real_sessions)} sessions -> {args.real_output}")
    print(f"lab experiment: {len(lab_sessions)} sessions -> {args.lab_output}")


if __name__ == "__main__":
    main()
