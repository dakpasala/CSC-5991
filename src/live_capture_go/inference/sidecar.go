// Package inference runs src/inference/predict.py as a long-lived child
// process and pipes finished window rows to it over stdin, so the live/
// replay tools can label each window as it closes. Go keeps doing all the
// per-packet work; Python only loads the AutoGluon predictor once and
// scores one row per window.
//
// Rows travel as CSV text over the pipe (header first, then one line per
// window) -- the exact format predict.py's streaming mode already reads, so
// no intermediate stats file is needed. predict.py owns the predictions
// file (its --output flag); its JSON-lines stdout is read back here only to
// print a one-line label per window to the terminal.
//
// Send never blocks the caller: rows go onto a buffered channel drained by
// a dedicated writer goroutine. That matters in the live tool, where the
// main goroutine blocking on a slow pipe (e.g. while Python is still
// loading the model at startup) would back up the packet channel and make
// the kernel drop packets.
//
// The child runs in its own process group so Ctrl+C reaches only the Go
// process. Go then closes the pipe, and predict.py scores whatever rows are
// still queued and exits on EOF, rather than being killed mid-window by
// the same SIGINT.
package inference

import (
	"bufio"
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"syscall"
)

type Config struct {
	Python     string // interpreter with autogluon.tabular installed, e.g. ../.venv-infer/bin/python
	Script     string // path to predict.py
	ModelDir   string // saved TabularPredictor directory, or an XGBoost .json model
	OutputPath string // predictions CSV that predict.py appends to
}

// Flags are the inference-related command-line flags shared by the live
// and replay tools. Defaults assume the documented "run from src/" usage.
type Flags struct {
	ModelDir *string
	Python   *string
	Script   *string
	Output   *string
	// AllFeatures makes the tools compute every stats column even when a
	// Go-native model needs only some (the pre-selective behavior), e.g. to
	// compare resource use, or to keep a complete -output stats CSV.
	AllFeatures *bool
}

// RegisterFlags must be called before flag.Parse().
func RegisterFlags() *Flags {
	return &Flags{
		ModelDir:    flag.String("model-dir", "", "model to label every window with: an XGBoost .json (train_xgboost_ablation.py --final), scored natively in Go, or an AutoGluon predictor directory, served by predict.py"),
		Python:      flag.String("python", "../.venv-infer/bin/python", "Python interpreter with autogluon.tabular installed (AutoGluon -model-dir only)"),
		Script:      flag.String("predict-script", "inference/predict.py", "path to predict.py (AutoGluon -model-dir only)"),
		AllFeatures: flag.Bool("all-features", false, "compute every stats column, not just the Go-native model's features"),
		Output:      flag.String("predictions-output", "", "predictions CSV path (used with -model-dir; default: live_runs/<name>_predictions_..._.csv)"),
	}
}

func (f *Flags) Enabled() bool {
	return *f.ModelDir != ""
}

// Config resolves the flags into a Config, using defaultOutput if
// -predictions-output wasn't given.
func (f *Flags) Config(defaultOutput string) Config {
	output := *f.Output
	if output == "" {
		output = defaultOutput
	}
	return Config{Python: *f.Python, Script: *f.Script, ModelDir: *f.ModelDir, OutputPath: output}
}

type Sidecar struct {
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	rows     chan []string
	writerOK chan struct{} // closed once the writer goroutine has finished
	readerOK chan struct{} // closed once predict.py's stdout hits EOF
}

type prediction struct {
	WindowIndex *int    `json:"window_index"`
	Label       string  `json:"label"`
	Confidence  float64 `json:"confidence"`
}

// Start launches predict.py and sends the CSV header immediately. The
// predictor load happens asynchronously on the Python side; rows sent in
// the meantime simply queue in the channel/pipe until it's ready.
func Start(cfg Config, header []string) (*Sidecar, error) {
	cmd := exec.Command(cfg.Python, cfg.Script, "--model-dir", cfg.ModelDir, "--output", cfg.OutputPath)
	cmd.Stderr = os.Stderr // predict.py's "loading predictor..." progress goes straight to the terminal
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("starting %s %s: %w", cfg.Python, cfg.Script, err)
	}

	s := &Sidecar{
		cmd:      cmd,
		stdin:    stdin,
		rows:     make(chan []string, 4096),
		writerOK: make(chan struct{}),
		readerOK: make(chan struct{}),
	}
	s.rows <- header
	go s.writeLoop()
	go s.readLoop(stdout)
	return s, nil
}

// Send queues one window row (core.WindowRow.Values(), same column order as
// the header passed to Start). Never blocks unless 4096 rows are already
// queued, i.e. Python has been stuck for hours of windows.
func (s *Sidecar) Send(values []string) {
	s.rows <- values
}

func (s *Sidecar) writeLoop() {
	defer close(s.writerOK)
	defer s.stdin.Close()
	w := csv.NewWriter(s.stdin)
	for values := range s.rows {
		w.Write(values)
		w.Flush() // one window per line, delivered immediately -- no batching across windows
		if err := w.Error(); err != nil {
			fmt.Fprintln(os.Stderr, "inference: predict.py stopped accepting rows:", err)
			for range s.rows {
				// keep draining so Send never blocks after the child dies
			}
			return
		}
	}
}

func (s *Sidecar) readLoop(stdout io.Reader) {
	defer close(s.readerOK)
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		var p prediction
		if err := json.Unmarshal(scanner.Bytes(), &p); err != nil {
			fmt.Println("inference:", scanner.Text()) // not a prediction line; pass it through as-is
			continue
		}
		if p.WindowIndex != nil {
			fmt.Printf("  -> window %d predicted: %s (%.2f)\n", *p.WindowIndex, p.Label, p.Confidence)
		} else {
			fmt.Printf("  -> predicted: %s (%.2f)\n", p.Label, p.Confidence)
		}
	}
}

// Close stops accepting rows, waits for predict.py to score everything
// still queued and exit, and returns its exit error, if any.
func (s *Sidecar) Close() error {
	close(s.rows)
	<-s.writerOK
	<-s.readerOK
	return s.cmd.Wait()
}
