"""Split each session_statistics_<N>_second_window.csv into organic and
controlled/lab-condition subsets, using the session IDs saved in
controlled_session_ids.txt (produced by find_controlled_sessions.py).

Rows whose session_id is in that list move OUT into a separate
session_statistics_<N>_second_window_controlled.csv (for separate research
later); the original file is left holding only organic sessions.

Writes the organic data to a temp file and atomically replaces the original
with it (os.replace), rather than overwriting in place -- if anything goes
wrong mid-write, the original file is untouched rather than half-written.

Usage:
    python src/filter_controlled_sessions.py
"""

import os

import pandas as pd

WINDOW_SECONDS = (10, 8, 4, 2, 1)
CONTROLLED_IDS_PATH = "controlled_session_ids.txt"


def main():
    with open(CONTROLLED_IDS_PATH) as handle:
        controlled_ids = {line.strip() for line in handle if line.strip()}
    print(f"loaded {len(controlled_ids)} controlled session IDs from {CONTROLLED_IDS_PATH}")

    for window_seconds in WINDOW_SECONDS:
        path = f"session_statistics_{window_seconds}_second_window.csv"
        if not os.path.isfile(path):
            print(f"skipping {path} (not found)")
            continue

        controlled_path = f"session_statistics_{window_seconds}_second_window_controlled.csv"
        tmp_path = path + ".tmp"

        df = pd.read_csv(path, dtype={"session_id": str})
        is_controlled = df["session_id"].isin(controlled_ids)

        controlled_df = df[is_controlled]
        organic_df = df[~is_controlled]

        controlled_df.to_csv(controlled_path, index=False)
        organic_df.to_csv(tmp_path, index=False)
        os.replace(tmp_path, path)

        print(
            f"{path}: {len(df)} rows ({df['session_id'].nunique()} sessions) -> "
            f"{len(organic_df)} organic rows ({organic_df['session_id'].nunique()} sessions) kept in place, "
            f"{len(controlled_df)} controlled rows ({controlled_df['session_id'].nunique()} sessions) "
            f"moved to {controlled_path}"
        )


if __name__ == "__main__":
    main()
