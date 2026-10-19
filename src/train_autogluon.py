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

--dynamic-stacking defaults to False (disabled). best_quality enables
AutoGluon's "DyStack" by default: before the real fit, it trains a whole
separate diagnostic ensemble on a data subset just to decide whether
stacking will cause overfitting. With --time-limit unset (unlimited,
the default here), that diagnostic step has no time bound of its own and
can itself take days on a large dataset with the full model zoo actually
training -- confirmed in practice: a run on 17,350 sessions spent 5+ days
stuck in the DyStack sub-fit without the real training ever starting.
Pass --dynamic-stacking to re-enable it if you specifically want that
overfitting check; otherwise this skips straight to the real fit.

--variant full (default) trains on session_statistics_<N>_second_window.csv,
the complete dataset including the 13 real application-layer columns (an
oracle-level upper bound -- a real router can't see decrypted HTTP data).
--variant no_applayer trains on session_statistics_<N>_second_window_no_applayer.csv
instead (produced by src/strip_application_layer.py), the router-realistic
dataset with those 13 columns removed. Model/importance output filenames
are tagged with the variant too, so a no_applayer run never collides with
or overwrites a full run on the same window size and session count.

Usage:
    python src/train_autogluon.py [--window-seconds all] [--time-limit None] [--presets best_quality] [--test-frac 0.2] [--seed 0] [--num-gpus 1] [--variant full]
    python src/train_autogluon.py --window-seconds 10            # just the 10s CSV
    python src/train_autogluon.py --variant no_applayer          # router-realistic dataset instead
    python src/train_autogluon.py --time-limit 600                # cap it if a full run takes too long
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


def train_one_window_size(window_seconds, time_limit, presets, test_frac, seed, num_gpus, variant, dynamic_stacking):
    suffix = "_no_applayer" if variant == "no_applayer" else ""
    csv_path = f"session_statistics_{window_seconds}_second_window{suffix}.csv"
    if not os.path.isfile(csv_path):
        print(f"skipping {csv_path} (not found)")
        return

    df = pd.read_csv(csv_path, dtype={"session_id": str})
    n_sessions = df["session_id"].nunique()
    print(f"\n{'=' * 70}\n{csv_path}: {len(df)} rows, {n_sessions} sessions")
    print(df["label"].value_counts())

    train_df, test_df = session_aware_split(df, test_frac, seed)
    print(f"train: {len(train_df)} rows, test: {len(test_df)} rows")

    # Tag outputs with the actual session count and variant so re-running
    # later on a bigger dataset -- or the other variant -- never collides
    # with (or silently overwrites) an older run; each combination gets its
    # own model directory and importance file, and old runs stick around
    # for comparison.
    run_tag = f"{window_seconds}s_{n_sessions}sessions"
    if variant != "full":
        run_tag += f"_{variant}"

    save_path = f"AutogluonModels/window_{run_tag}"
    predictor = TabularPredictor(
        label=LABEL_COLUMN, path=save_path, eval_metric="accuracy", problem_type="multiclass"
    )
    predictor.fit(
        train_df, time_limit=time_limit, presets=presets, num_gpus=num_gpus, dynamic_stacking=dynamic_stacking
    )

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

    importance_path = f"feature_importance_{run_tag}.csv"
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
    parser.add_argument(
        "--variant",
        choices=["full", "no_applayer"],
        default="full",
        help="full = normal CSVs (incl. application-layer cols); "
        "no_applayer = router-realistic CSVs from strip_application_layer.py",
    )
    parser.add_argument(
        "--dynamic-stacking",
        action="store_true",
        help="re-enable AutoGluon's DyStack overfitting check (disabled by default -- "
        "see module docstring, it can take days unbounded when --time-limit is unset)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.window_seconds.lower() == "all":
        window_sizes = WINDOW_SECONDS
    else:
        window_sizes = [int(args.window_seconds)]

    for window_seconds in window_sizes:
        train_one_window_size(
            window_seconds,
            args.time_limit,
            args.presets,
            args.test_frac,
            args.seed,
            args.num_gpus,
            args.variant,
            args.dynamic_stacking,
        )


if __name__ == "__main__":
    main()
