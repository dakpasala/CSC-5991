// xgbeval scores every row of a stats CSV with a plain XGBoost JSON model
// (train_xgboost_ablation.py --final), entirely in Go, and writes the same
// predictions CSV format predict.py --output does. Two uses: checking that
// the Go evaluator matches Python's predict_proba row for row (run both on
// the same CSV and diff), and batch-scoring captured stats on the router,
// where there's no Python.
//
// Usage (run from src/, where go.mod lives):
//
//	go run ./live_capture_go/xgbeval -model ../XGBoostModels/10s_no_applayer/final_option_c.json -input ../test_sample_no_applayer.csv -output preds.csv
//	go run ./live_capture_go/xgbeval -model ... -input ... -output preds.csv -precision 9   # full precision for comparisons
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"csc5991/live_capture_go/inference"
	"csc5991/live_capture_go/xgbmodel"
)

func main() {
	modelPath := flag.String("model", "", "XGBoost .json model (with classes.json beside it)")
	input := flag.String("input", "", "stats CSV to score (header row first)")
	output := flag.String("output", "", "predictions CSV to write (appended if it exists)")
	precision := flag.Int("precision", 4, "decimal places for confidence/probabilities")
	flag.Parse()
	if *modelPath == "" || *input == "" || *output == "" {
		fmt.Fprintln(os.Stderr, "usage: xgbeval -model <model.json> -input <stats.csv> -output <predictions.csv> [-precision 4]")
		os.Exit(1)
	}

	start := time.Now()
	clf, err := xgbmodel.LoadClassifier(*modelPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "loaded %s: %d features, %d trees, %d nodes in %.2fs\n",
		*modelPath, len(clf.Features), clf.NumTrees(), clf.NumNodes(), time.Since(start).Seconds())

	f, err := os.Open(*input)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	defer f.Close()
	r := csv.NewReader(f)
	header, err := r.Read()
	if err != nil {
		fmt.Fprintln(os.Stderr, "error reading header:", err)
		os.Exit(1)
	}
	idx, err := clf.ColumnIndex(header)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	winIdx := [3]int{-1, -1, -1}
	for i, h := range header {
		for j, w := range [3]string{"window_index", "window_start_s", "window_end_s"} {
			if h == w {
				winIdx[j] = i
			}
		}
	}

	pw, err := inference.NewPredictionsWriter(*output, clf.Classes, *precision)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	defer pw.Close()

	rows := 0
	var scoring time.Duration
	for {
		values, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "error reading row:", err)
			os.Exit(1)
		}
		t := time.Now()
		p := clf.ScoreRow(values, idx)
		scoring += time.Since(t)
		var win [3]string
		for j, i := range winIdx {
			if i >= 0 {
				win[j] = values[i]
			}
		}
		if err := pw.Write(win, p); err != nil {
			fmt.Fprintln(os.Stderr, "error writing prediction:", err)
			os.Exit(1)
		}
		rows++
	}
	fmt.Fprintf(os.Stderr, "scored %d rows -> %s (%.1f µs per row)\n", rows, *output, scoring.Seconds()/float64(max(rows, 1))*1e6)
}
