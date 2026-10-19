"""Strip the 13 application-layer columns (request_count,
segment_size_bytes_*, request_duration_ms_*) out of each
session_statistics_<N>_second_window.csv, writing the result to a new
session_statistics_<N>_second_window_no_applayer.csv per window size.

These columns come from requests_history.json -- real, decrypted HTTP data
the browser can see because it's the TLS endpoint. A real deployed router
has no way to see this without actively decrypting traffic (full deep
packet inspection), so this produces the "router-realistic" dataset: a
model trained on it can only use what a real no-DPI observer could
actually see, separate from the oracle-level dataset that assumes full
application-layer visibility.

Does not touch the original CSVs at all -- purely additive, so you keep
both the full and stripped datasets around to train and compare.

Usage:
    python src/strip_application_layer.py
"""

import os

import pandas as pd

WINDOW_SECONDS = (10, 8, 4, 2, 1)

APPLICATION_LAYER_COLUMNS = [
    "request_count",
    "segment_size_bytes_min",
    "segment_size_bytes_max",
    "segment_size_bytes_mean",
    "segment_size_bytes_median",
    "segment_size_bytes_p25",
    "segment_size_bytes_p75",
    "request_duration_ms_min",
    "request_duration_ms_max",
    "request_duration_ms_mean",
    "request_duration_ms_median",
    "request_duration_ms_p25",
    "request_duration_ms_p75",
]


def main():
    for window_seconds in WINDOW_SECONDS:
        path = f"session_statistics_{window_seconds}_second_window.csv"
        if not os.path.isfile(path):
            print(f"skipping {path} (not found)")
            continue

        out_path = f"session_statistics_{window_seconds}_second_window_no_applayer.csv"

        df = pd.read_csv(path, dtype={"session_id": str})
        present = [col for col in APPLICATION_LAYER_COLUMNS if col in df.columns]
        missing = [col for col in APPLICATION_LAYER_COLUMNS if col not in df.columns]
        if missing:
            print(f"  warning: {path} is missing expected columns: {missing}")

        stripped = df.drop(columns=present)
        stripped.to_csv(out_path, index=False)

        print(
            f"{path}: {df.shape[1]} cols -> {out_path}: {stripped.shape[1]} cols "
            f"({len(present)} application-layer columns dropped, {len(df)} rows unchanged)"
        )


if __name__ == "__main__":
    main()
