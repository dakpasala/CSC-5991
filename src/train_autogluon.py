"""Train an AutoGluon TabularPredictor to classify streaming service from
session-window traffic statistics, once per window-size CSV
(session_statistics_10/8/4/2/1_second_window.csv).

Uses a session-aware train/test split (by session_id, not by row): windows
from the same session are highly correlated, so splitting by row would leak
information between train and test. session_id/window_index/window_start_s/
window_end_s are dropped before training -- identifiers/position, not real
traffic features.

Reports per-class precision/recall/f1 (not just overall accuracy), since a
single accuracy number can hide one trivially-easy class (e.g. youtube_quic,
separable almost entirely via protocol mix) masking how hard the rest are.

Also computes permutation feature importance (predictor.feature_importance)
on the held-out test set, not train -- importance computed on training data
can be inflated if a model partially memorized it. Saved to
feature_importance_<N>_second_window.csv per window size, alongside the
printed table (importance, stddev, p-value, per feature).

Defaults to presets="best_quality" (AutoGluon's most exhaustive search --
multi-layer stacking, k-fold bagging, the widest hyperparameter search per
model type) and no time limit (runs until the search is actually done, not
until a clock cuts it off). This is slower than a quick baseline but tries
every model AutoGluon has, not just whatever fits in a fixed time budget.

Usage:
    python src/train_autogluon.py [--window-seconds all] [--time-limit None] [--presets best_quality] [--test-frac 0.2] [--seed 0] [--num-gpus 1]
    python src/train_autogluon.py --window-seconds 10   # just the 10s CSV
    python src/train_autogluon.py --time-limit 600      # cap it if a full run takes too long
"""

import argparse
import os

import pandas as pd
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from autogluon.tabular import TabularPredictor

WINDOW_SECONDS = (10, 8, 4, 2, 1)
NON_FEATURE_COLUMNS = ("session_id", "window_index", "window_start_s", "window_end_s")
LABEL_COLUMN = "label"


def session_aware_split(df, test_frac, seed):
    session_labels = df.groupby("session_id")["label"].first()
    try:
        train_sessions, test_sessions = train_test_split(
            session_labels.index,
            test_size=test_frac,
            stratify=session_labels,
            random_state=seed,
        )
    except ValueError as exc:
        print(f"stratified session split failed ({exc}); falling back to unstratified split")
        train_sessions, test_sessions = train_test_split(
            session_labels.index, test_size=test_frac, random_state=seed
        )

    train_df = df[df["session_id"].isin(train_sessions)].drop(columns=list(NON_FEATURE_COLUMNS))
    test_df = df[df["session_id"].isin(test_sessions)].drop(columns=list(NON_FEATURE_COLUMNS))
    return train_df, test_df


def train_one_window_size(window_seconds, time_limit, presets, test_frac, seed, num_gpus):
    csv_path = f"session_statistics_{window_seconds}_second_window.csv"
    if not os.path.isfile(csv_path):
        print(f"skipping {csv_path} (not found)")
        return

    df = pd.read_csv(csv_path, dtype={"session_id": str})
    print(f"\n{'=' * 70}\n{csv_path}: {len(df)} rows, {df['session_id'].nunique()} sessions")
    print(df["label"].value_counts())

    train_df, test_df = session_aware_split(df, test_frac, seed)
    print(f"train: {len(train_df)} rows, test: {len(test_df)} rows")

    save_path = f"AutogluonModels/window_{window_seconds}s"
    predictor = TabularPredictor(
        label=LABEL_COLUMN, path=save_path, eval_metric="accuracy", problem_type="multiclass"
    )
    predictor.fit(train_df, time_limit=time_limit, presets=presets, num_gpus=num_gpus)

    y_true = test_df[LABEL_COLUMN]
    y_pred = predictor.predict(test_df.drop(columns=[LABEL_COLUMN]))

    print(f"\nleaderboard ({csv_path}):")
    print(predictor.leaderboard(test_df, silent=True))

    print(f"\nper-class report ({csv_path}):")
    print(classification_report(y_true, y_pred))

    print(f"\ncomputing permutation feature importance on the held-out test set ({csv_path})...")
    importance_df = predictor.feature_importance(test_df)
    print(f"\nfeature importance ({csv_path}):")
    print(importance_df)

    importance_path = f"feature_importance_{window_seconds}_second_window.csv"
    importance_df.to_csv(importance_path)
    print(f"saved to {importance_path}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--time-limit", type=int, default=None, help="seconds AutoGluon spends per window size (default: unlimited)"
    )
    parser.add_argument("--presets", default="best_quality")
    parser.add_argument("--test-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--num-gpus", type=int, default=1, help="GPUs to grant the whole predictor; 0 forces CPU")
    parser.add_argument("--window-seconds", default="all", help='one of 10/8/4/2/1, or "all"')
    return parser.parse_args()


def main():
    args = parse_args()
    if args.window_seconds.lower() == "all":
        window_sizes = WINDOW_SECONDS
    else:
        window_sizes = [int(args.window_seconds)]

    for window_seconds in window_sizes:
        train_one_window_size(
            window_seconds, args.time_limit, args.presets, args.test_frac, args.seed, args.num_gpus
        )


if __name__ == "__main__":
    main()
