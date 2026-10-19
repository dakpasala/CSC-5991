"""Quick sanity check: how many unique sessions are in a session_statistics
CSV so far, useful for confirming batch progress matches what you expect.

Usage:
    python src/check_progress.py [path_to_csv]
    (defaults to session_statistics_10_second_window.csv, the smallest file)
"""

import sys

import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "session_statistics_10_second_window.csv"

df = pd.read_csv(path, dtype={"session_id": str})

print(f"{path}")
print(f"  total rows: {len(df)}")
print(f"  unique sessions: {df['session_id'].nunique()}")
print(f"  duplicate (session_id, window_index) pairs: {df.duplicated(subset=['session_id', 'window_index']).sum()}")
print()
print("label breakdown (by unique session, not row):")
print(df.drop_duplicates("session_id")["label"].value_counts())
