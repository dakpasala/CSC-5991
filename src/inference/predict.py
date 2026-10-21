"""Run inference with a trained AutoGluon TabularPredictor against feature
rows shaped like session_statistics_<N>_second_window[_no_applayer].csv --
the Python side of a Go-calls-Python sidecar (see AGENTS.md's inference
section): a Go live-capture/replay tool does the per-packet work and hands
off one finished window's feature row at a time; this process stays
loaded and scores each one as it arrives.

The predictor is loaded exactly ONCE, up front -- that can take real time
for a best_quality bagged+stacked directory (the no_applayer run's
AutogluonModels directory was 9.6GB+ while still training), so reloading
it per row would dominate the whole request. That's the whole reason this
is a long-lived process reading a stream, not a fresh subprocess spawned
per window.

Two modes:
  --input <path.csv>    batch mode: scores every row in the file, prints
                         one JSON line per row to stdout, then exits. Good
                         for one-off testing against a CSV you already have.
  (no --input)           streaming mode: reads one CSV row at a time from
                         stdin (first line must be the header), scores it
                         the instant it arrives, prints one JSON line to
                         stdout per row, flushed immediately. This is the
                         mode a long-lived Go sidecar caller should use --
                         plain csv.DictReader over stdin, not pandas'
                         chunked file reading, so there's no ambiguity
                         about internal buffering delaying a row's output.

Output, one JSON object per line ("JSON Lines", so a Go reader can decode
incrementally without needing a row count up front):
  {"label": "<predicted class>", "probabilities": {"<class>": <prob>, ...}}

Drops the same non-feature columns train_autogluon.py does
(session_id/window_index/window_start_s/window_end_s/label) if present,
so this can be fed either bare feature rows or full
session_statistics_*.csv rows without pre-stripping them yourself.
Feature values are coerced to numeric (empty cells -> NaN, matching a
QUIC-dominant window's structurally-empty TCP columns) since stdin rows
arrive as plain strings via csv.DictReader, with no dtype information of
their own the way a pd.read_csv call on a real file would infer.

autogluon.tabular is imported lazily (inside main(), not at module level)
so this file stays importable -- and its CLI help / argument parsing
testable -- on a machine that hasn't installed the (large, slow-to-import)
autogluon package at all.

Usage:
    python src/inference/predict.py --model-dir AutogluonModels/window_10s_19468sessions_no_applayer --input session_statistics_10_second_window_no_applayer.csv
    python src/inference/predict.py --model-dir AutogluonModels/window_10s_19468sessions_no_applayer < rows.csv
"""

import argparse
import csv
import json
import os
import sys

import pandas as pd

# Duplicated from train_autogluon.py rather than imported from it: that
# file imports sklearn/autogluon.tabular unconditionally at module level,
# so importing even just its two constants would drag in both heavy
# dependencies here too -- defeating the point of this file's own lazy
# autogluon import (see module docstring). These two values are small and
# stable; if the real feature-table shape ever changes, both files need
# updating together regardless of which one "owns" the constant.
NON_FEATURE_COLUMNS = ("session_id", "window_index", "window_start_s", "window_end_s")
LABEL_COLUMN = "label"


def _prepare_features(df):
    """Drops identifier/label columns if present, then coerces whatever's
    left to numeric -- every real feature column in this project's schema
    is numeric (counts, byte/time stats, percentiles), so this is safe to
    apply uniformly rather than naming each column."""
    to_drop = [c for c in (*NON_FEATURE_COLUMNS, LABEL_COLUMN) if c in df.columns]
    features = df.drop(columns=to_drop) if to_drop else df
    return features.apply(pd.to_numeric, errors="coerce")


def _score_rows(predictor, df):
    features = _prepare_features(df)
    labels = predictor.predict(features)
    probs = predictor.predict_proba(features)
    for i in range(len(features)):
        row_probs = {str(cls): float(probs.iloc[i][cls]) for cls in probs.columns}
        yield {"label": str(labels.iloc[i]), "probabilities": row_probs}


def run_batch(predictor, input_path):
    df = pd.read_csv(input_path, dtype={"session_id": str})
    for result in _score_rows(predictor, df):
        print(json.dumps(result), flush=True)


def run_streaming(predictor):
    reader = csv.DictReader(sys.stdin)
    for row in reader:
        df = pd.DataFrame([row])
        for result in _score_rows(predictor, df):
            print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", required=True, help="path to a saved TabularPredictor directory")
    parser.add_argument(
        "--input", default=None, help="CSV file to score in batch mode; omit to read one row at a time from stdin"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.model_dir):
        raise SystemExit(f"--model-dir not found: {args.model_dir}")

    from autogluon.tabular import TabularPredictor  # noqa: E402 (lazy: see module docstring)

    print(f"loading predictor from {args.model_dir} ...", file=sys.stderr)
    predictor = TabularPredictor.load(args.model_dir)
    print("predictor loaded, ready to score", file=sys.stderr)

    if args.input:
        run_batch(predictor, args.input)
    else:
        run_streaming(predictor)


if __name__ == "__main__":
    main()
