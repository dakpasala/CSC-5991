package inference

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"time"

	"csc5991/live_capture_go/xgbmodel"
)

// Labeler is what the live/replay tools hand each closed window row to:
// either the Python sidecar (Sidecar) or the in-process XGBoost evaluator
// (Native). Send never blocks for long; Close finishes any queued work.
type Labeler interface {
	Send(values []string)
	Close() error
}

// StartLabeler starts the right labeler for cfg.ModelDir: an XGBoost .json
// model is scored natively in Go (no Python needed -- the router path), and
// anything else is treated as an AutoGluon directory served by the
// predict.py sidecar.
func StartLabeler(cfg Config, header []string) (Labeler, error) {
	if strings.HasSuffix(cfg.ModelDir, ".json") {
		return StartNative(cfg.ModelDir, cfg.OutputPath, header)
	}
	return Start(cfg, header)
}

// Native labels windows in-process with a plain XGBoost JSON model
// (train_xgboost_ablation.py --final) -- no Python, no xgboost library, no
// cgo -- which is what the router build uses. Scoring one window is
// microseconds of tree walking, so it runs synchronously inside Send rather
// than through a channel/goroutine like Sidecar needs.
type Native struct {
	clf       *xgbmodel.Classifier
	idx       []int
	winIdx    [3]int // positions of window_index, window_start_s, window_end_s in the header
	writer    *PredictionsWriter
	lastScore time.Duration
	last      xgbmodel.Prediction
}

// StartNative loads the model and opens the predictions CSV. header is the
// column order of the rows Send will receive (core.CSVHeader()).
func StartNative(modelPath, outputPath string, header []string) (*Native, error) {
	clf, err := xgbmodel.LoadClassifier(modelPath)
	if err != nil {
		return nil, fmt.Errorf("loading %s: %w", modelPath, err)
	}
	idx, err := clf.ColumnIndex(header)
	if err != nil {
		return nil, err
	}
	n := &Native{clf: clf, idx: idx, winIdx: [3]int{-1, -1, -1}}
	for i, h := range header {
		for j, w := range windowColumns {
			if h == w {
				n.winIdx[j] = i
			}
		}
	}
	// Parsing the JSON (50MB for the option-C model) leaves a lot of garbage;
	// hand it back to the OS now rather than whenever the GC gets to it, since
	// the router has no swap and the trees themselves only need ~13MB.
	debug.FreeOSMemory()
	n.writer, err = NewPredictionsWriter(outputPath, clf.Classes, 4)
	if err != nil {
		return nil, err
	}
	fmt.Fprintf(os.Stderr, "XGBoost model loaded in Go: %d features, %d trees, %d nodes\n", len(clf.Features), clf.NumTrees(), clf.NumNodes())
	return n, nil
}

// Features is the model's input columns, in order.
func (n *Native) Features() []string { return n.clf.Features }

// ExtractionFeatures is the feature list the accumulator should compute
// for this labeler: the Go-native model's inputs, or nil (compute every
// column) for the Python sidecar, whose AutoGluon model's inputs aren't
// known here -- or when allFeatures forces the full set.
func ExtractionFeatures(l Labeler, allFeatures bool) []string {
	if n, ok := l.(*Native); ok && !allFeatures {
		return n.Features()
	}
	return nil
}

// Score labels a row without recording it -- used for per-connection rows,
// which are not written to the predictions CSV.
func (n *Native) Score(values []string) xgbmodel.Prediction { return n.clf.ScoreRow(values, n.idx) }

// LastPrediction is the label/confidence Send produced for the most recent window.
func (n *Native) LastPrediction() xgbmodel.Prediction { return n.last }

// LastScore is how long the most recent Send spent in the model itself
// (feature lookup + tree walk + softmax), excluding file/terminal output.
func (n *Native) LastScore() time.Duration { return n.lastScore }

func (n *Native) Send(values []string) {
	start := time.Now()
	p := n.clf.ScoreRow(values, n.idx)
	n.lastScore = time.Since(start)
	n.last = p
	var win [3]string
	for j, i := range n.winIdx {
		if i >= 0 {
			win[j] = values[i]
		}
	}
	if err := n.writer.Write(win, p); err != nil {
		fmt.Fprintln(os.Stderr, "inference: writing prediction:", err)
	}
	fmt.Printf("  -> window %s predicted: %s (%.2f)\n", win[0], p.Label, p.Confidence)
}

func (n *Native) Close() error {
	return n.writer.Close()
}

var windowColumns = [3]string{"window_index", "window_start_s", "window_end_s"}

// PredictionsWriter appends one CSV row per scored window, in the same
// format predict.py --output writes (window_index, window_start_s,
// window_end_s, predicted_label, confidence, prob_<class>...), flushed
// after every row. The header is written only if the file is new.
type PredictionsWriter struct {
	f         *os.File
	w         *csv.Writer
	precision int
}

func NewPredictionsWriter(path string, classes []string, precision int) (*PredictionsWriter, error) {
	if dir := filepath.Dir(path); dir != "." {
		os.MkdirAll(dir, 0755)
	}
	_, statErr := os.Stat(path)
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}
	pw := &PredictionsWriter{f: f, w: csv.NewWriter(f), precision: precision}
	if os.IsNotExist(statErr) {
		header := append(windowColumns[:], "predicted_label", "confidence")
		for _, c := range classes {
			header = append(header, "prob_"+c)
		}
		pw.w.Write(header)
		pw.w.Flush()
	}
	return pw, pw.w.Error()
}

// Write formats window_index as an integer and the times as floats, the
// way predict.py does, so files from either side compare line for line.
func (pw *PredictionsWriter) Write(win [3]string, p xgbmodel.Prediction) error {
	row := []string{formatWindowIndex(win[0]), formatWindowTime(win[1]), formatWindowTime(win[2]), p.Label, pw.format(p.Confidence)}
	for _, v := range p.Probabilities {
		row = append(row, pw.format(v))
	}
	pw.w.Write(row)
	pw.w.Flush()
	return pw.w.Error()
}

func (pw *PredictionsWriter) format(v float64) string {
	return strconv.FormatFloat(v, 'f', pw.precision, 64)
}

func formatWindowIndex(s string) string {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return s
	}
	return strconv.FormatInt(int64(v), 10)
}

func formatWindowTime(s string) string {
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return s
	}
	out := strconv.FormatFloat(v, 'f', -1, 64)
	for _, c := range out {
		if c == '.' || c == 'e' {
			return out
		}
	}
	return out + ".0" // match Python's float repr ("10.0", not "10")
}

func (pw *PredictionsWriter) Close() error {
	pw.w.Flush()
	return pw.f.Close()
}
