"""Export all_traffic_time_10.pkl to a labeled CSV ready for ML training.

The source pickle is Python-2-era (pickle.load needs encoding="latin1";
plain pd.read_pickle raises UnicodeDecodeError). See AGENTS.md for the
verified structure: one row per 10-second time window within a session,
251 columns, four services, with a real TCP/QUIC split available for
YouTube (no other service has meaningful non-TCP traffic in this data).

The `index` column is dropped -- it is 0 for every row in the source file,
not a real sequence number. `avg_flow_age` nulls (~8% of rows) are left as
NaN rather than imputed or dropped, since there's no evidence-based way to
fill them in.

`10_EWMA_chunksizes` holds a 10-element array per row (not a scalar), which
would otherwise get written to CSV as a single bracketed, multi-line blob of
text. It's expanded into ten columns `10_EWMA_chunksizes_1..10` instead. Most
rows have the full 10 values; some have fewer (early in a session, before a
full 10-chunk window exists) or a bare `0` (no chunk data for that window at
all) -- missing positions are left as NaN, not fabricated as 0, since a 0
would misleadingly imply an observed chunk of size zero.
"""

import argparse
import pickle

import numpy as np
import pandas as pd

SOURCE_DEFAULT = "all_traffic_time_10.pkl"
OUTPUT_DEFAULT = "streaming_traffic_features.csv"
EWMA_COLUMN = "10_EWMA_chunksizes"
EWMA_LENGTH = 10


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DEFAULT)
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    return parser.parse_args()


def load_source(path):
    with open(path, "rb") as handle:
        return pickle.load(handle, encoding="latin1")


def _ewma_row_values(value):
    values = np.ravel(value).astype(float) if isinstance(value, np.ndarray) else []
    padded = list(values) + [np.nan] * (EWMA_LENGTH - len(values))
    return padded[:EWMA_LENGTH]


def expand_ewma_column(df):
    expanded = pd.DataFrame(
        df[EWMA_COLUMN].apply(_ewma_row_values).tolist(),
        columns=[f"{EWMA_COLUMN}_{i}" for i in range(1, EWMA_LENGTH + 1)],
        index=df.index,
    )
    position = df.columns.get_loc(EWMA_COLUMN)
    df = df.drop(columns=[EWMA_COLUMN])
    for offset, column in enumerate(expanded.columns):
        df.insert(position + offset, column, expanded[column])
    return df


def build_label(df):
    """youtube splits into youtube_quic / youtube_tcp by is_tcp; every other
    service is its own single label, matching what's actually present in
    the data (no other service has meaningful non-TCP traffic here)."""
    is_youtube = df["service"] == "youtube"
    label = df["service"].copy()
    label[is_youtube & df["is_tcp"]] = "youtube_tcp"
    label[is_youtube & ~df["is_tcp"]] = "youtube_quic"
    return label


def main():
    args = parse_args()

    df = load_source(args.source)
    df = df.drop(columns=["index"])
    df = expand_ewma_column(df)
    df.insert(0, "label", build_label(df))

    df.to_csv(args.output, index=False)

    print(f"{len(df)} rows, {len(df.columns)} columns -> {args.output}")
    print("label distribution:")
    print(df["label"].value_counts().to_string())


if __name__ == "__main__":
    main()
