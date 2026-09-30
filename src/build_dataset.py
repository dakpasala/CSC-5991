"""Build flow-level and session-level labeled feature CSVs from a pcapng capture.

Usage:
    python src/build_dataset.py traffic.pcapng \
        --flows-out flow_features.csv --sessions-out session_features.csv

See AGENTS.md for the session/flow data model and what each feature means.

Session IDs are up to 20 digits -- past int64 range. They are written to the
output CSVs as exact text, but a naive `pd.read_csv(...)` on the *output*
files will silently round them through float64 and corrupt them. Always load
these CSVs with `dtype={"session_id": str}`.
"""

import argparse

import pandas as pd

from feature_extraction import compute_flow_features
from flow_builder import build_sessions
from packet_parser import parse_tcp_packet
from pcapng_reader import read_packets

IDENTIFIER_COLUMNS = {
    "session_id",
    "label",
    "client_ip",
    "client_port",
    "server_ip",
    "server_port",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcapng_path", help="Path to the input .pcapng capture")
    parser.add_argument(
        "--flows-out", default="flow_features.csv",
        help="Output path for the flow-level CSV (default: flow_features.csv)",
    )
    parser.add_argument(
        "--sessions-out", default="session_features.csv",
        help="Output path for the session-level CSV (default: session_features.csv)",
    )
    return parser.parse_args()


def build_flow_dataframe(pcapng_path):
    packets = (
        parsed
        for raw in read_packets(pcapng_path)
        if (parsed := parse_tcp_packet(raw)) is not None
    )
    sessions = build_sessions(packets)

    rows = [
        compute_flow_features(flow)
        for session in sessions.values()
        for flow in session.flows.values()
    ]
    return pd.DataFrame(rows)


def build_session_dataframe(flow_df):
    numeric_columns = [
        column
        for column in flow_df.columns
        if column not in IDENTIFIER_COLUMNS
    ]
    grouped = flow_df.groupby(["session_id", "label"])

    named_aggregations = {
        f"{column}_{stat_name}": pd.NamedAgg(column=column, aggfunc=aggfunc)
        for column in numeric_columns
        for stat_name, aggfunc in (
            ("median", "median"),
            ("p25", lambda s: s.quantile(0.25)),
            ("p75", lambda s: s.quantile(0.75)),
        )
    }
    summary = grouped.agg(**named_aggregations)
    summary["num_flows"] = grouped.size()
    return summary.reset_index()


def main():
    args = parse_args()

    flow_df = build_flow_dataframe(args.pcapng_path)
    flow_df.to_csv(args.flows_out, index=False)

    session_df = build_session_dataframe(flow_df)
    session_df.to_csv(args.sessions_out, index=False)

    print(f"flows: {len(flow_df)} rows -> {args.flows_out}")
    print(f"sessions: {len(session_df)} rows -> {args.sessions_out}")
    print("label distribution (flows):")
    print(flow_df["label"].value_counts().to_string())


if __name__ == "__main__":
    main()
