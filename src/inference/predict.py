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
  {"window_index": <int>, "window_start_s": <float>, "window_end_s": <float>,
   "label": "<predicted class>", "confidence": <max prob>,
   "probabilities": {"<class>": <prob>, ...}}
The three window_* keys are only present if the input row had those
columns (every Go/Python stats row does).

--output <path.csv> additionally appends one row per scored window to a
predictions CSV (window_index, window_start_s, window_end_s,
predicted_label, confidence, prob_<class>...), flushed after every row so
it's durable the moment a window is scored. This is how the Go live/
replay tools' -model-dir mode records predictions: Go pipes rows in over
stdin, this process owns the predictions file.

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
    python src/inference/predict.py --model-dir AutogluonModels/window_10s_19468sessions_no_applayer_xgb22 --output live_runs/predictions.csv < rows.csv
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


WINDOW_COLUMNS = ("window_index", "window_start_s", "window_end_s")


def _score_rows(predictor, df):
    features = _prepare_features(df)
    # predict_proba once and take the argmax, instead of predict() +
    # predict_proba() -- halves the per-window model cost, which matters
    # in streaming mode where every window pays it individually.
    probs = predictor.predict_proba(features)
    labels = probs.idxmax(axis=1)
    window_meta = df[[c for c in WINDOW_COLUMNS if c in df.columns]].apply(pd.to_numeric, errors="coerce")
    for i in range(len(features)):
        result = {}
        for col in window_meta.columns:
            value = window_meta.iloc[i][col]
            result[col] = int(value) if col == "window_index" else float(value)
        row_probs = {str(cls): float(probs.iloc[i][cls]) for cls in probs.columns}
        result["label"] = str(labels.iloc[i])
        result["confidence"] = max(row_probs.values())
        result["probabilities"] = row_probs
        yield result


class PredictionsWriter:
    """Appends one CSV row per scored window, flushed immediately. The
    header (which needs the class list) is written lazily on the first
    result, and only if the file didn't already exist -- same append-to-
    accumulating-file convention as the stats CSVs."""

    def __init__(self, path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._needs_header = not os.path.isfile(path)
        self._file = open(path, "a", newline="")
        self._writer = csv.writer(self._file)
        self._classes = None

    def write(self, result):
        if self._classes is None:
            self._classes = sorted(result["probabilities"])
            if self._needs_header:
                self._writer.writerow(
                    [*WINDOW_COLUMNS, "predicted_label", "confidence", *(f"prob_{c}" for c in self._classes)]
                )
        self._writer.writerow(
            [
                *(result.get(c, "") for c in WINDOW_COLUMNS),
                result["label"],
                f"{result['confidence']:.4f}",
                *(f"{result['probabilities'][c]:.4f}" for c in self._classes),
            ]
        )
        self._file.flush()

    def close(self):
        self._file.close()


def _emit(result, predictions_writer):
    print(json.dumps(result), flush=True)
    if predictions_writer is not None:
        predictions_writer.write(result)


def run_batch(predictor, input_path, predictions_writer):
    df = pd.read_csv(input_path, dtype={"session_id": str})
    for result in _score_rows(predictor, df):
        _emit(result, predictions_writer)


def run_streaming(predictor, predictions_writer):
    reader = csv.DictReader(sys.stdin)
    for row in reader:
        df = pd.DataFrame([row])
        for result in _score_rows(predictor, df):
            _emit(result, predictions_writer)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", required=True, help="path to a saved TabularPredictor directory")
    parser.add_argument(
        "--input", default=None, help="CSV file to score in batch mode; omit to read one row at a time from stdin"
    )
    parser.add_argument(
        "--output", default=None, help="also append one row per scored window to this predictions CSV"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.model_dir):
        raise SystemExit(f"--model-dir not found: {args.model_dir}")

    from autogluon.tabular import TabularPredictor  # noqa: E402 (lazy: see module docstring)

    print(f"loading predictor from {args.model_dir} ...", file=sys.stderr)
    predictor = TabularPredictor.load(args.model_dir)
    print("predictor loaded, ready to score", file=sys.stderr)

    predictions_writer = PredictionsWriter(args.output) if args.output else None
    try:
        if args.input:
            run_batch(predictor, args.input, predictions_writer)
        else:
            run_streaming(predictor, predictions_writer)
    finally:
        if predictions_writer is not None:
            predictions_writer.close()


if __name__ == "__main__":
    main()
