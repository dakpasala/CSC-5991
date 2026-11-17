"""Feature-ablation experiments with a single XGBoost model, to find how
small a feature set can get before accuracy drops.

Why a single XGBoost model and not AutoGluon: the deployed model is already
a single XGBoost (XGBoost_r22_BAG_L1 from the no_applayer run, val 0.984536
vs. the full 70GB stack's 0.985771), and one XGBoost fit takes minutes
instead of the ~2 weeks a best_quality AutoGluon run takes, so many feature
sets can be compared. XGBoost also saves to a JSON format that the Go
evaluator (src/live_capture_go/xgbmodel) loads directly, so the winning model
can go straight to the pure-Go router pipeline.

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
       streaming_only      -- drop every median/p25/p75 column (see "router
                              cost" below): min/max/mean and counts only
       no_total_streaming  -- both of the above at once: the cheapest
                              possible feature extraction
  2. Recursive feature elimination (rfe): starting from --rfe-start (any
     fixed set above, default all), repeatedly train, measure permutation
     importance on val, and drop the weakest --drop-frac of the remaining
     units (at least 1) until --min-features units remain. Importance is
     recomputed after every drop because the features are heavily
     redundant: one-at-a-time permutation importance gives near zero to a
     feature whose siblings carry the same information, and those siblings
     become important once it's gone.
     --rfe-unit feature (default) eliminates individual columns.
     --rfe-unit family eliminates whole distribution families at once (all
     six stats of e.g. down_payload_size, shuffled together for grouped
     permutation importance); scalar columns (counts, frac_udp, ...) are
     each their own unit. Every family step frees a real per-window buffer,
     which single-column steps usually don't.
     The rfe experiment is named "rfe" for the defaults, otherwise
     "rfe_<unit>_from_<start>", so several rfe runs can share one CSV.

Final model (--final <feature list file>): instead of running experiments,
train ONE model on exactly the features listed in the file (one name per
line; blank lines and # comments ignored; column order is kept as written).
Two stages, so test stays untouched:
  1. fit on the fit split with early stopping on val -- this picks the
     number of boosting rounds, and its val accuracy is what gets reported
     as val_accuracy (the only honest val number, since stage 2 trains on
     val);
  2. refit from scratch on fit + val combined, with the round count scaled
     by (fit + val rows) / fit rows, no early stopping. Skip with
     --no-refit to keep the stage-1 model instead.
The final model is scored on test once and saved as
XGBoostModels/<run>/final_<name>.json alongside final_<name>.features.json
(the exact input column order the model expects -- a Go evaluator must feed
values in this order) and classes.json. Ready-made lists for the candidate
options are in benchmarks/feature_sets/.

Router cost: a feature's memory cost on a router is per distribution
FAMILY, not per column. min/max/mean can be updated one packet at a time in
O(1) memory, but median/p25/p75 need every value of the window kept in a
buffer, and one buffer serves all six stats of its family -- dropping
down_payload_size_mean saves nothing while down_payload_size_p25 is still
used. So every result row also records dist_families (distribution
families used at all) and buffered_families (families needing a full
buffer because some median/p25/p75 of theirs is used). The first rfe run
showed why this matters: its recommended 47-feature set still needed all 13
families (10 buffered), while no_total's 78 features needed only 9.

At the end of each rfe run it prints two recommendations among the steps
within --tolerance of the best val accuracy any step reached (all-features
baseline included): the fewest features, and the fewest buffered families.

Outputs (repo-root-relative, like train_autogluon.py):
  benchmarks/xgb_ablation_<window>s_<variant>.csv -- one row per model:
      experiment, step, n_features, val_accuracy, test_accuracy,
      test_macro_f1, predict_us_per_row, best_iteration, fit_seconds,
      dist_families, buffered_families, dropped (features removed at this
      rfe step), features (all used). If an older results CSV lacks a
      column, the file is rewritten with the union of columns rather than
      appended misaligned.
  XGBoostModels/<window>s_<variant>/<experiment>[_step<k>].json -- every
      trained model, in XGBoost's native JSON format (load in Go with
      src/live_capture_go/xgbmodel, or in Python with xgb.XGBClassifier().load_model)

Usage (from the repo root, on the box that has the CSV):
    python src/train_xgboost_ablation.py
    python src/train_xgboost_ablation.py --window-seconds 4
    python src/train_xgboost_ablation.py --experiments rfe --drop-frac 0.1
    python src/train_xgboost_ablation.py --experiments streaming_only,no_total_streaming
    python src/train_xgboost_ablation.py --experiments rfe --rfe-start no_total
    python src/train_xgboost_ablation.py --experiments rfe --rfe-start no_total --rfe-unit family
    python src/train_xgboost_ablation.py --final benchmarks/feature_sets/option_c.txt
    python src/train_xgboost_ablation.py --final my_features.txt --max-depth 6
    python src/train_xgboost_ablation.py --csv session_statistics_10_second_window.csv --variant no_applayer  # strips app-layer cols if present
"""

import argparse
import json
import os
import sys
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
FIXED_EXPERIMENTS = ("all", "no_total", "shape_only", "infra_only", "streaming_only", "no_total_streaming")
DIST_STATS = ("min", "max", "mean", "median", "p25", "p75")
BUFFERED_STATS = ("median", "p25", "p75")  # need the whole window's values kept; min/max/mean don't


def split_stat(feature):
    """("down_payload_size", "p25") for a distribution column, (feature, None)
    for a scalar one."""
    base, _, stat = feature.rpartition("_")
    return (base, stat) if stat in DIST_STATS else (feature, None)


def router_cost(features):
    """(distribution families used, families needing a full buffer)."""
    families, buffered = set(), set()
    for f in features:
        base, stat = split_stat(f)
        if stat is not None:
            families.add(base)
            if stat in BUFFERED_STATS:
                buffered.add(base)
    return len(families), len(buffered)


def read_feature_file(path):
    """Feature names from a --final file: one per line, blank lines and
    # comments ignored, order preserved, duplicates rejected."""
    with open(path) as f:
        names = [line.split("#", 1)[0].strip() for line in f]
    names = [n for n in names if n]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise SystemExit(f"{path}: duplicate features: {duplicates}")
    if not names:
        raise SystemExit(f"{path}: no features listed")
    return names


def elimination_units(features, unit):
    """{unit name: [columns]}. One column per unit for --rfe-unit feature;
    for family, each distribution family is one unit (all its stats that
    are still present) and each scalar column is its own unit."""
    if unit == "feature":
        return {f: [f] for f in features}
    units = {}
    for f in features:
        base, stat = split_stat(f)
        units.setdefault(base if stat is not None else f, []).append(f)
    return units


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
    if experiment == "streaming_only":
        return [f for f in all_features if split_stat(f)[1] not in BUFFERED_STATS]
    if experiment == "no_total_streaming":
        return [f for f in feature_set("no_total", all_features) if split_stat(f)[1] not in BUFFERED_STATS]
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

    def fit_fixed_rounds_on_fit_and_val(self, features, n_rounds):
        """Refit from scratch on fit + val combined, for a fixed number of
        rounds (no early stopping -- val is training data here)."""
        model = xgb.XGBClassifier(
            n_estimators=n_rounds,
            learning_rate=self.args.learning_rate,
            max_depth=self.args.max_depth,
            tree_method="hist",
            n_jobs=self.args.n_jobs,
            random_state=self.args.seed,
        )
        X = pd.concat([self.X("fit", features), self.X("val", features)], ignore_index=True)
        y = np.concatenate([self.y("fit"), self.y("val")])
        start = time.time()
        model.fit(X, y, verbose=False)
        return model, time.time() - start

    def test_metrics(self, model, features):
        X_test = self.X("test", features)
        start = time.time()
        test_pred = model.predict(X_test)
        predict_us = (time.time() - start) / len(X_test) * 1e6
        y_test = self.y("test")
        return {
            "test_accuracy": accuracy_score(y_test, test_pred),
            "test_macro_f1": f1_score(y_test, test_pred, average="macro"),
            "predict_us_per_row": predict_us,
        }

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

    def val_permutation_importance(self, model, features, units):
        """Accuracy drop on (a sample of) val when each unit's columns are
        shuffled -- together, with one shared row permutation, so a family's
        stats stay consistent with each other -- averaged over
        --perm-repeats shuffles."""
        rng = np.random.default_rng(self.args.seed)
        X = self.X("val", features)
        y = self.y("val")
        if len(X) > self.args.perm_rows:
            idx = rng.choice(len(X), self.args.perm_rows, replace=False)
            X, y = X.iloc[idx].reset_index(drop=True), y[idx]
        base = accuracy_score(y, model.predict(X))
        importance = {}
        for name, cols in units.items():
            drops = []
            for _ in range(self.args.perm_repeats):
                shuffled = X.copy()
                shuffled[cols] = shuffled[cols].to_numpy()[rng.permutation(len(shuffled))]
                drops.append(base - accuracy_score(y, model.predict(shuffled)))
            importance[name] = float(np.mean(drops))
        return pd.Series(importance).sort_values(ascending=False)

    def save(self, model, name):
        model.save_model(os.path.join(self.model_dir, f"{name}.json"))


def append_result(path, row):
    new = pd.DataFrame([row])
    if os.path.isfile(path):
        existing_columns = list(pd.read_csv(path, nrows=0).columns)
        if set(existing_columns) == set(new.columns):
            new = new[existing_columns]  # same schema, different order: plain append in the file's order
        else:
            # Older file with a different schema: rewrite with the union of
            # columns instead of appending a misaligned row.
            merged = pd.concat([pd.read_csv(path), new], ignore_index=True)
            # Backfill router-cost columns for rows written before they
            # existed, from each row's own feature list.
            missing = merged["dist_families"].isna()
            if missing.any():
                costs = [router_cost(f.split(";")) for f in merged.loc[missing, "features"]]
                merged.loc[missing, "dist_families"] = [c[0] for c in costs]
                merged.loc[missing, "buffered_families"] = [c[1] for c in costs]
            for col in ("dist_families", "buffered_families"):
                merged[col] = merged[col].astype(int)
            merged.to_csv(path, index=False)
            return
    new.to_csv(path, mode="a", header=not os.path.isfile(path), index=False)


def run_final(args, trainer, all_features, results_path, model_dir):
    features = read_feature_file(args.final)
    unknown = [f for f in features if f not in all_features]
    if unknown:
        raise SystemExit(f"{args.final}: not columns in the dataset: {unknown}")
    name = "final_" + os.path.splitext(os.path.basename(args.final))[0]
    n_families, n_buffered = router_cost(features)
    print(f"{name}: {len(features)} features from {args.final}, {n_families} families ({n_buffered} buffered)")

    # Stage 1: early stopping on val picks the round count.
    model, fit_s = trainer.fit(features)
    stage1 = trainer.evaluate(model, features)
    rounds = model.best_iteration + 1
    print(
        f"  stage 1 (fit split, early stopped): {rounds} rounds, val {stage1['val_accuracy']:.4f}, "
        f"test {stage1['test_accuracy']:.4f}, {fit_s:.0f}s"
    )

    if args.no_refit:
        final_model, final_rounds, final = model, rounds, stage1
    else:
        # Stage 2: refit on fit + val with proportionally more rounds.
        n_fit, n_val = len(trainer.splits["fit"][0]), len(trainer.splits["val"][0])
        final_rounds = max(1, round(rounds * (n_fit + n_val) / n_fit))
        final_model, refit_s = trainer.fit_fixed_rounds_on_fit_and_val(features, final_rounds)
        final = trainer.test_metrics(final_model, features)
        fit_s += refit_s
        print(f"  stage 2 (refit on fit+val): {final_rounds} rounds, test {final['test_accuracy']:.4f}, {refit_s:.0f}s")

    trainer.save(final_model, name)
    with open(os.path.join(model_dir, f"{name}.features.json"), "w") as f:
        json.dump(features, f, indent=1)
    append_result(
        results_path,
        {
            "experiment": name,
            "step": "",
            "n_features": len(features),
            "val_accuracy": round(stage1["val_accuracy"], 6),
            "test_accuracy": round(final["test_accuracy"], 6),
            "test_macro_f1": round(final["test_macro_f1"], 6),
            "predict_us_per_row": round(final["predict_us_per_row"], 6),
            "best_iteration": final_rounds,
            "fit_seconds": round(fit_s, 1),
            "dist_families": n_families,
            "buffered_families": n_buffered,
            "dropped": "",
            "features": ";".join(features),
        },
    )
    print(
        f"\n{name}: test accuracy {final['test_accuracy']:.4f}, macro F1 {final['test_macro_f1']:.4f} "
        f"(val {stage1['val_accuracy']:.4f} from stage 1)\n"
        f"model    -> {model_dir}/{name}.json\n"
        f"features -> {model_dir}/{name}.features.json (input column order)\n"
        f"classes  -> {model_dir}/classes.json\n"
        f"results  -> {results_path}"
    )


def main():
    # Line-buffer stdout so each model's result line shows up immediately
    # under `| tee` / tmux logging; Python block-buffers stdout when it isn't
    # a terminal, which otherwise hides progress for many models at a time.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window-seconds", type=int, default=10)
    parser.add_argument("--variant", choices=["full", "no_applayer"], default="no_applayer")
    parser.add_argument("--csv", default=None, help="override the input CSV (default: session_statistics_<N>_second_window[_no_applayer].csv)")
    parser.add_argument("--experiments", default="all,no_total,shape_only,infra_only,rfe", help="comma-separated subset of: " + ",".join((*FIXED_EXPERIMENTS, "rfe")))
    parser.add_argument("--rfe-start", choices=FIXED_EXPERIMENTS, default="all", help="rfe: feature set to start eliminating from")
    parser.add_argument("--rfe-unit", choices=["feature", "family"], default="feature", help="rfe: eliminate single columns, or whole distribution families")
    parser.add_argument("--test-frac", type=float, default=0.2, help="must match train_autogluon.py's run to share its test sessions")
    parser.add_argument("--val-frac", type=float, default=0.15, help="fraction of TRAINING sessions held out for validation")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--drop-frac", type=float, default=0.2, help="rfe: fraction of remaining units dropped per step")
    parser.add_argument("--min-features", type=int, default=3, help="rfe: stop once this many units (features or families) remain")
    parser.add_argument("--tolerance", type=float, default=0.001, help="recommend the smallest rfe set within this much val accuracy of baseline")
    parser.add_argument("--perm-rows", type=int, default=20000, help="val rows sampled for permutation importance")
    parser.add_argument("--perm-repeats", type=int, default=3)
    parser.add_argument("--max-rounds", type=int, default=2000)
    parser.add_argument("--early-stopping", type=int, default=50)
    parser.add_argument("--learning-rate", type=float, default=0.1)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--n-jobs", type=int, default=-1)
    parser.add_argument("--final", metavar="FEATURE_FILE", default=None, help="train one final model on the features listed in this file instead of running experiments")
    parser.add_argument("--no-refit", action="store_true", help="--final: keep the early-stopped fit-split model instead of refitting on fit+val")
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

    if args.final:
        run_final(args, trainer, all_features, results_path, model_dir)
        return

    def run(experiment, features, step="", dropped=()):
        model, fit_s = trainer.fit(features)
        metrics = trainer.evaluate(model, features)
        name = experiment if step == "" else f"{experiment}_step{step:02d}"
        trainer.save(model, name)
        n_families, n_buffered = router_cost(features)
        row = {
            "experiment": experiment,
            "step": step,
            "n_features": len(features),
            **{k: round(v, 6) for k, v in metrics.items()},
            "best_iteration": model.best_iteration,
            "fit_seconds": round(fit_s, 1),
            "dist_families": n_families,
            "buffered_families": n_buffered,
            "dropped": ";".join(dropped),
            "features": ";".join(features),
        }
        append_result(results_path, row)
        print(
            f"{name:<32} {len(features):>3} features  val {metrics['val_accuracy']:.4f}  "
            f"test {metrics['test_accuracy']:.4f}  macroF1 {metrics['test_macro_f1']:.4f}  "
            f"{fit_s:.0f}s fit, {model.best_iteration} trees, {n_families} families ({n_buffered} buffered)"
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
        rfe_name = "rfe" if (args.rfe_unit, args.rfe_start) == ("feature", "all") else f"rfe_{args.rfe_unit}_from_{args.rfe_start}"
        features = feature_set(args.rfe_start, all_features)
        steps = []
        step = 0
        dropped = ()
        while True:
            model, metrics = run(rfe_name, features, step=step, dropped=dropped)
            n_families, n_buffered = router_cost(features)
            steps.append((step, len(features), metrics["val_accuracy"], metrics["test_accuracy"], n_families, n_buffered))
            if step == 0 and baseline_val is None:
                baseline_val = metrics["val_accuracy"]
            units = elimination_units(features, args.rfe_unit)
            if len(units) <= args.min_features:
                break
            importance = trainer.val_permutation_importance(model, features, units)
            n_drop = max(1, int(len(units) * args.drop_frac))
            n_drop = min(n_drop, len(units) - args.min_features)
            dropped_units = importance.index[-n_drop:]
            dropped = tuple(c for u in dropped_units for c in units[u])
            features = [f for f in features if f not in dropped]
            step += 1

        # Measured against the best val accuracy any step reached, not just
        # step 0: val is noisy enough that mid-elimination steps can beat the
        # all-features baseline, and anchoring on a low baseline would
        # recommend cutting further than the data supports.
        reference_val = max(baseline_val, max(s[2] for s in steps))
        within = [s for s in steps if s[2] >= reference_val - args.tolerance]
        fewest_features = min(within, key=lambda s: (s[1], s[5]))
        cheapest = min(within, key=lambda s: (s[5], s[4], s[1]))
        print(
            f"\n{rfe_name}: best val {reference_val:.4f} (all-features val {baseline_val:.4f}), tolerance {args.tolerance}"
        )
        for label, s in (("fewest features", fewest_features), ("cheapest on a router", cheapest)):
            print(
                f"recommended ({label}): step {s[0]} -- {s[1]} features, {s[4]} families ({s[5]} buffered), "
                f"val {s[2]:.4f}, test {s[3]:.4f}"
            )

    print(f"\nresults -> {results_path}\nmodels  -> {model_dir}/")


if __name__ == "__main__":
    main()
