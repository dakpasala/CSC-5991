"""Filter streaming_traffic_features.csv down to real-world sessions only,
dropping the 684 controlled lab-experiment sessions (see AGENTS.md's "The
real dataset" section for why those don't belong in a dataset meant to
represent organic network conditions).

Uses the same session_id pattern as aggregate_sessions.py to identify
real-world sessions, so the two scripts can't silently drift apart on what
counts as "real."
"""

import argparse

import pandas as pd

from aggregate_sessions import HASH_SESSION_ID

SOURCE_DEFAULT = "streaming_traffic_features.csv"
OUTPUT_DEFAULT = "streaming_traffic_features_real.csv"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DEFAULT)
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    return parser.parse_args()


def main():
    args = parse_args()

    df = pd.read_csv(args.source, dtype={"session_id": str}, low_memory=False)
    is_real = df["session_id"].str.fullmatch(HASH_SESSION_ID)
    real_df = df[is_real]
    real_df.to_csv(args.output, index=False)

    print(f"{len(df)} rows -> {len(real_df)} real-world rows -> {args.output}")
    print(f"dropped {len(df) - len(real_df)} lab-experiment rows")


if __name__ == "__main__":
    main()
