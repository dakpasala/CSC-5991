"""Feature-ablation experiments with a single XGBoost model, to find how
small a feature set can get before accuracy drops.

Why a single XGBoost model and not AutoGluon: the deployed model is already
a single XGBoost (XGBoost_r22_BAG_L1 from the no_applayer run, val 0.984536
vs. the full 70GB stack's 0.985771), and one XGBoost fit takes minutes
instead of the ~2 weeks a best_quality AutoGluon run takes, so many feature
sets can be compared. XGBoost also saves to a JSON format a Go library
(leaves) can load, so the winning model can go straight to the pure-Go
router pipeline.

Splits (all session-aware, by session_id -- see train_autogluon.py):
  - test: the exact same held-out sessions train_autogluon.py uses
    (stratified, --test-frac 0.2, --seed 0), so results are comparable to
    the AutoGluon run. Test is only ever scored for reporting, never used
    to make a decision.
  - val: --val-frac of the remaining (training) sessions. Every decision --
    early stopping, feature ranking, which features to drop, which feature
    set to recommend -- is made on val. Choosing on test and then reporting
    test would be data snooping (Arp et al. 2022).

Two kinds of experiments, both written to one results CSV:
  1. Fixed feature sets, each a hypothesis worth testing on its own:
       all          -- every feature (the baseline)
       no_total     -- drop all total_* columns; each is computed from the
                       matching up_ and down_ values, so they're redundant
                       by construction, and they cost router memory
       shape_only   -- drop recv_window/rtt/frac_udp, the infrastructure-
                       fingerprint features that dominated the AutoGluon
                       importance. Tests whether services are separable by
                       traffic shape alone (a shortcut-learning check)
       infra_only   -- only recv_window/rtt/frac_udp, the reverse check
  2. Recursive feature elimination (rfe): starting from all features,
     repeatedly train, measure permutation importance on val, and drop the
     weakest --drop-frac (at least 1) until --min-features remain.
     Importance is recomputed after every drop because the features are
     heavily redundant: one-at-a-time permutation importance gives near
     zero to a feature whose siblings carry the same information, and those
     siblings become important once it's gone.

At the end it prints the recommended rfe step: the smallest feature set
whose val accuracy is within --tolerance of the best val accuracy any step
reached (all-features baseline included).

Outputs (repo-root-relative, like train_autogluon.py):
  benchmarks/xgb_ablation_<window>s_<variant>.csv -- one row per model:
      experiment, step, n_features, val_accuracy, test_accuracy,
      test_macro_f1, best_iteration, fit_seconds, predict_us_per_row,
      dropped (features removed at this rfe step), features (all used)
  XGBoostModels/<window>s_<variant>/<experiment>[_step<k>].json -- every
      trained model, in XGBoost's native JSON format (load in Go with
      leaves, or in Python with xgb.XGBClassifier().load_model)

Usage (from the repo root, on the box that has the CSV):
    python src/train_xgboost_ablation.py
    python src/train_xgboost_ablation.py --window-seconds 4
    python src/train_xgboost_ablation.py --experiments rfe --drop-frac 0.1
    python src/train_xgboost_ablation.py --csv session_statistics_10_second_window.csv --variant no_applayer  # strips app-layer cols if present
"""

import argparse
import json
import os
import time

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split

NON_FEATURE_COLUMNS = ("session_id", "window_index", "window_start_s", "window_end_s")
LABEL_COLUMN = "label"

# Same list as src/scripts/strip_application_layer.py. Duplicated rather
# than imported, matching how predict.py duplicates train_autogluon.py's
# constants.
APPLICATION_LAYER_COLUMNS = (
    "request_count",
    *(f"segment_size_bytes_{s}" for s in ("min", "max", "mean", "median", "p25", "p75")),
    *(f"request_duration_ms_{s}" for s in ("min", "max", "mean", "median", "p25", "p75")),
)

INFRA_SUBSTRINGS = ("recv_window", "rtt_ms", "frac_udp")
FIXED_EXPERIMENTS = ("all", "no_total", "shape_only", "infra_only")


def split_sessions(session_labels, frac, seed):
    """Session-aware split, stratified by label when possible -- same call
    (and same fallback) as train_autogluon.py's session_aware_split, so
    with the same frac/seed it picks the same test sessions."""
    try:
        return train_test_split(session_labels.index, test_size=frac, stratify=session_labels, random_state=seed)
    except ValueError as exc:
        print(f"stratified session split failed ({exc}); falling back to unstratified split")
        return train_test_split(session_labels.index, test_size=frac, random_state=seed)


def load_splits(csv_path, variant, test_frac, val_frac, seed):
    df = pd.read_csv(csv_path, dtype={"session_id": str})
    if variant == "no_applayer":
        df = df.drop(columns=[c for c in APPLICATION_LAYER_COLUMNS if c in df.columns])
    print(f"{csv_path}: {len(df)} rows, {df['session_id'].nunique()} sessions")

    labels = df.groupby("session_id")[LABEL_COLUMN].first()
    train_sessions, test_sessions = split_sessions(labels, test_frac, seed)
    fit_sessions, val_sessions = split_sessions(labels.loc[train_sessions], val_frac, seed)

    def part(sessions):
        sub = df[df["session_id"].isin(sessions)]
        return sub.drop(columns=[*NON_FEATURE_COLUMNS, LABEL_COLUMN]), sub[LABEL_COLUMN]

    splits = {"fit": part(fit_sessions), "val": part(val_sessions), "test": part(test_sessions)}
    for name, (X, _) in splits.items():
        print(f"  {name}: {len(X)} rows")
    return splits


def feature_set(experiment, all_features):
    if experiment == "all":
        return list(all_features)
    if experiment == "no_total":
        return [f for f in all_features if not f.startswith("total_")]
    if experiment == "shape_only":
        return [f for f in all_features if not any(s in f for s in INFRA_SUBSTRINGS)]
    if experiment == "infra_only":
        return [f for f in all_features if any(s in f for s in INFRA_SUBSTRINGS)]
    raise ValueError(experiment)


class Trainer:
    def __init__(self, splits, classes, args, model_dir):
        self.splits = splits
        self.encode = {c: i for i, c in enumerate(classes)}
        self.args = args
        self.model_dir = model_dir

    def y(self, name):
        return self.splits[name][1].map(self.encode).to_numpy()

    def X(self, name, features):
        return self.splits[name][0][features].apply(pd.to_numeric, errors="coerce")

    def fit(self, features):
        model = xgb.XGBClassifier(
            n_estimators=self.args.max_rounds,
            learning_rate=self.args.learning_rate,
            max_depth=self.args.max_depth,
            tree_method="hist",
            early_stopping_rounds=self.args.early_stopping,
            eval_metric="mlogloss",
            n_jobs=self.args.n_jobs,
            random_state=self.args.seed,
        )
        start = time.time()
        model.fit(self.X("fit", features), self.y("fit"), eval_set=[(self.X("val", features), self.y("val"))], verbose=False)
        return model, time.time() - start

    def evaluate(self, model, features):
        val_acc = accuracy_score(self.y("val"), model.predict(self.X("val", features)))
        X_test = self.X("test", features)
        start = time.time()
        test_pred = model.predict(X_test)
        predict_us = (time.time() - start) / len(X_test) * 1e6
        y_test = self.y("test")
        return {
            "val_accuracy": val_acc,
            "test_accuracy": accuracy_score(y_test, test_pred),
            "test_macro_f1": f1_score(y_test, test_pred, average="macro"),
            "predict_us_per_row": predict_us,
        }

    def val_permutation_importance(self, model, features):
        """Accuracy drop on (a sample of) val when each feature is shuffled,
        averaged over --perm-repeats shuffles."""
        rng = np.random.default_rng(self.args.seed)
        X = self.X("val", features)
        y = self.y("val")
        if len(X) > self.args.perm_rows:
            idx = rng.choice(len(X), self.args.perm_rows, replace=False)
            X, y = X.iloc[idx].reset_index(drop=True), y[idx]
        base = accuracy_score(y, model.predict(X))
        importance = {}
        for f in features:
            drops = []
            for _ in range(self.args.perm_repeats):
                shuffled = X.copy()
                shuffled[f] = rng.permutation(shuffled[f].to_numpy())
                drops.append(base - accuracy_score(y, model.predict(shuffled)))
            importance[f] = float(np.mean(drops))
        return pd.Series(importance).sort_values(ascending=False)

    def save(self, model, name):
        model.save_model(os.path.join(self.model_dir, f"{name}.json"))


def append_result(path, row):
    pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.isfile(path), index=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window-seconds", type=int, default=10)
    parser.add_argument("--variant", choices=["full", "no_applayer"], default="no_applayer")
    parser.add_argument("--csv", default=None, help="override the input CSV (default: session_statistics_<N>_second_window[_no_applayer].csv)")
    parser.add_argument("--experiments", default="all,no_total,shape_only,infra_only,rfe", help="comma-separated subset of: " + ",".join((*FIXED_EXPERIMENTS, "rfe")))
    parser.add_argument("--test-frac", type=float, default=0.2, help="must match train_autogluon.py's run to share its test sessions")
    parser.add_argument("--val-frac", type=float, default=0.15, help="fraction of TRAINING sessions held out for validation")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--drop-frac", type=float, default=0.2, help="rfe: fraction of remaining features dropped per step")
    parser.add_argument("--min-features", type=int, default=3, help="rfe: stop once this many remain")
    parser.add_argument("--tolerance", type=float, default=0.001, help="recommend the smallest rfe set within this much val accuracy of baseline")
    parser.add_argument("--perm-rows", type=int, default=20000, help="val rows sampled for permutation importance")
    parser.add_argument("--perm-repeats", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=2000)
    parser.add_argument("--early-stopping", type=int, default=50)
    parser.add_argument("--learning-rate", type=float, default=0.1)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--n-jobs", type=int, default=-1)
    args = parser.parse_args()

    experiments = [e.strip() for e in args.experiments.split(",") if e.strip()]
    unknown = set(experiments) - {*FIXED_EXPERIMENTS, "rfe"}
    if unknown:
        raise SystemExit(f"unknown experiments: {sorted(unknown)}")

    suffix = "_no_applayer" if args.variant == "no_applayer" else ""
    csv_path = args.csv or f"session_statistics_{args.window_seconds}_second_window{suffix}.csv"
    if not os.path.isfile(csv_path):
        raise SystemExit(f"not found: {csv_path}")

    run_tag = f"{args.window_seconds}s_{args.variant}"
    os.makedirs("benchmarks", exist_ok=True)
    results_path = f"benchmarks/xgb_ablation_{run_tag}.csv"
    model_dir = f"XGBoostModels/{run_tag}"
    os.makedirs(model_dir, exist_ok=True)
    if os.path.isfile(results_path):
        print(f"note: appending to existing {results_path}")

    splits = load_splits(csv_path, args.variant, args.test_frac, args.val_frac, args.seed)
    classes = sorted(pd.concat([y for _, y in splits.values()]).unique())
    with open(os.path.join(model_dir, "classes.json"), "w") as f:
        json.dump(classes, f)  # class index -> label, needed to decode predictions from the saved JSON models
    all_features = list(splits["fit"][0].columns)
    trainer = Trainer(splits, classes, args, model_dir)

    def run(experiment, features, step="", dropped=()):
        model, fit_s = trainer.fit(features)
        metrics = trainer.evaluate(model, features)
        name = experiment if step == "" else f"{experiment}_step{step:02d}"
        trainer.save(model, name)
        row = {
            "experiment": experiment,
            "step": step,
            "n_features": len(features),
            **{k: round(v, 6) for k, v in metrics.items()},
            "best_iteration": model.best_iteration,
            "fit_seconds": round(fit_s, 1),
            "dropped": ";".join(dropped),
            "features": ";".join(features),
        }
        append_result(results_path, row)
        print(
            f"{name:<22} {len(features):>3} features  val {metrics['val_accuracy']:.4f}  "
            f"test {metrics['test_accuracy']:.4f}  macroF1 {metrics['test_macro_f1']:.4f}  "
            f"{fit_s:.0f}s fit, {model.best_iteration} trees"
        )
        return model, metrics

    baseline_val = None
    for experiment in [e for e in FIXED_EXPERIMENTS if e in experiments]:
        features = feature_set(experiment, all_features)
        if not features:
            print(f"skipping {experiment} (no matching features)")
            continue
        _, metrics = run(experiment, features)
        if experiment == "all":
            baseline_val = metrics["val_accuracy"]

    if "rfe" in experiments:
        features = list(all_features)
        steps = []
        step = 0
        dropped = ()
        while True:
            model, metrics = run("rfe", features, step=step, dropped=dropped)
            steps.append((step, len(features), metrics["val_accuracy"], metrics["test_accuracy"]))
            if step == 0 and baseline_val is None:
                baseline_val = metrics["val_accuracy"]
            if len(features) <= args.min_features:
                break
            importance = trainer.val_permutation_importance(model, features)
            n_drop = max(1, int(len(features) * args.drop_frac))
            n_drop = min(n_drop, len(features) - args.min_features)
            dropped = tuple(importance.index[-n_drop:])
            features = [f for f in features if f not in dropped]
            step += 1

        # Measured against the best val accuracy any step reached, not just
        # step 0: val is noisy enough that mid-elimination steps can beat the
        # all-features baseline, and anchoring on a low baseline would
        # recommend cutting further than the data supports.
        reference_val = max(baseline_val, max(s[2] for s in steps))
        within = [s for s in steps if s[2] >= reference_val - args.tolerance]
        best = min(within, key=lambda s: s[1])
        print(
            f"\nrecommended: rfe step {best[0]} -- {best[1]} features, val {best[2]:.4f} "
            f"(best val {reference_val:.4f}, all-features val {baseline_val:.4f}, tolerance {args.tolerance}), "
            f"test {best[3]:.4f}"
        )

    print(f"\nresults -> {results_path}\nmodels  -> {model_dir}/")


if __name__ == "__main__":
    main()
