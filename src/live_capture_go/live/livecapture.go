// Go port of src/live_capture/live_capture_pipeline.py -- captures real
// traffic off a network interface and writes window stats as they close.
//
// Unlike the Python version, this does NOT shell out to tcpdump and poll
// the filesystem for rotated files. Go doesn't have Python's GIL problem,
// so it can safely do genuine concurrent capture-and-process in a single
// process: one goroutine reads packets off the interface and sends them on
// a channel, the main goroutine drains that channel and closes out windows
// via the same core.WindowAccumulator the replay tool uses -- capture never
// blocks on computation, computation never waits on capture, no
// subprocess/file-rotation machinery needed at all. This is architecturally
// closer to what real router software actually looks like.
//
// Same two real limitations as the Python version, inherent to live
// capture off a NIC, not something this works around:
//   - No requests_history.json, so the 13 application-layer columns always
//     come out 0/NaN.
//   - No exp.txt, so there's no automatic ground-truth label (output
//     `label` column is the "live_unlabeled" placeholder).
//
// Captures at the same snaplen (200 bytes) as the existing dataset, so
// this never touches real payload bytes, consistent with the project's
// no-DPI premise throughout.
//
// Packet capture goes through libpcap (capture_pcap.go) wherever cgo is
// available, and through a pure-Go Linux AF_PACKET raw socket
// (capture_linux_nocgo.go) when built with CGO_ENABLED=0 -- that's the
// router build: cross-compiled on a Mac as one static binary, for an OpenWrt
// router with no C compiler. Both truncate to the same 200-byte snaplen
// and report the true original packet length.
//
// Requires root (or equivalent capture capabilities) to open the
// interface -- same privilege requirement as the Python version's tcpdump
// subprocess, just now needed directly by this binary instead.
//
// With -model-dir, every closed window is also labeled and the prediction
// appended to live_runs/live_predictions_..._.csv (see live_capture_go/
// inference): an XGBoost .json model is scored natively in Go, no Python
// needed; an AutoGluon directory is piped to src/inference/predict.py. In that
// mode the stats CSV is skipped unless -output is also given explicitly.
//
// Usage (run from src/, where go.mod lives):
//
//	sudo go run ./live_capture_go/live -interface eth0
//	sudo go run ./live_capture_go/live -interface eth0 -window-seconds 10 -output live_stats.csv
//	sudo go run ./live_capture_go/live -interface eth0 -model-dir ../AutogluonModels/window_10s_19468sessions_no_applayer_xgb22
//	sudo go run ./live_capture_go/live -interface eth0 -model-dir ../XGBoostModels/10s_no_applayer/final_option_c.json
package main

import (
	"encoding/csv"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	core "csc5991/core_go"
	"csc5991/live_capture_go/inference"
)

const labelPlaceholder = "live_unlabeled"
const snaplen = 200

func main() {
	iface := flag.String("interface", "", `network interface to capture on, e.g. "eth0"`)
	windowSeconds := flag.Float64("window-seconds", 10, "window size in seconds")
	output := flag.String("output", "", "stats CSV path (default: timestamped live_statistics_<N>_second_window file; skipped with -model-dir unless given)")
	inferenceFlags := inference.RegisterFlags()
	flag.Parse()

	if *iface == "" {
		fmt.Fprintln(os.Stderr, "usage: live -interface <name> [-window-seconds 10] [-output path.csv] [-model-dir <predictor dir>]")
		os.Exit(1)
	}

	runStartedAt := time.Now().Format("20060102_150405")

	handle, err := openCapture(*iface, snaplen)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error opening interface (needs root/capture capabilities):", err)
		os.Exit(1)
	}
	defer handle.Close()

	var out sinks
	if *output != "" || !inferenceFlags.Enabled() {
		out.statsPath = *output
		if out.statsPath == "" {
			os.MkdirAll("live_runs", 0755)
			out.statsPath = fmt.Sprintf("live_runs/live_statistics_%d_second_window_%s.csv", int(*windowSeconds), runStartedAt)
		}
		f, err := os.OpenFile(out.statsPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error opening output file:", err)
			os.Exit(1)
		}
		defer f.Close()
		out.stats = csv.NewWriter(f)
		if err := out.stats.Write(core.CSVHeader()); err != nil {
			fmt.Fprintln(os.Stderr, "error writing CSV header:", err)
			os.Exit(1)
		}
		out.stats.Flush()
	}
	if inferenceFlags.Enabled() {
		os.MkdirAll("live_runs", 0755)
		cfg := inferenceFlags.Config(fmt.Sprintf("live_runs/live_predictions_%d_second_window_%s.csv", int(*windowSeconds), runStartedAt))
		out.labeler, err = inference.StartLabeler(cfg, core.CSVHeader())
		if err != nil {
			fmt.Fprintln(os.Stderr, "error starting inference:", err)
			os.Exit(1)
		}
		out.predictionsPath = cfg.OutputPath
	}

	sessionID := "live_" + runStartedAt
	var acc *core.WindowAccumulator // created once the first packet's timestamp gives us t0

	pktChan := make(chan *core.Packet, 1024)
	go captureLoop(handle, pktChan)

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	fmt.Printf("capturing on %s, %gs windows -> %s (Ctrl+C to stop)\n", *iface, *windowSeconds, out.describe())

	for {
		select {
		case p, ok := <-pktChan:
			if !ok {
				fmt.Println("\ncapture ended")
				out.finish(acc)
				return
			}
			if acc == nil {
				acc = core.NewWindowAccumulator(p.Timestamp, *windowSeconds, sessionID, labelPlaceholder)
			}
			for _, row := range acc.HandlePacket(p) {
				out.writeRow(row)
			}
		case <-sigChan:
			fmt.Println("\nstopping capture...")
			out.finish(acc)
			return
		}
	}
}

// sinks is where each closed window row goes: the stats CSV, the
// labeler (Go-native XGBoost or the predict.py sidecar), or both -- whichever were enabled by the flags.
type sinks struct {
	stats           *csv.Writer
	statsPath       string
	labeler         inference.Labeler
	predictionsPath string
	rowsWritten     int
}

func (s *sinks) describe() string {
	switch {
	case s.stats != nil && s.labeler != nil:
		return s.statsPath + " + predictions " + s.predictionsPath
	case s.labeler != nil:
		return "predictions " + s.predictionsPath
	default:
		return s.statsPath
	}
}

// captureLoop reads packets off the interface and sends them on a channel
// -- this goroutine never blocks on window computation, matching the
// Python version's "tcpdump never pauses" guarantee, just without needing
// an external process to get it.
func captureLoop(handle captureHandle, out chan<- *core.Packet) {
	for {
		data, ci, err := handle.ReadPacketData()
		if err != nil {
			close(out)
			return
		}
		ts := float64(ci.Timestamp.UnixNano()) / 1e9
		if p := core.ParsePacket(data, ts, ci.Length); p != nil {
			out <- p
		}
	}
}

// finish writes the final partial window, then (if inference is on) waits
// for predict.py to score everything still queued before exiting, so no
// window's prediction is lost on Ctrl+C.
func (s *sinks) finish(acc *core.WindowAccumulator) {
	if acc != nil { // nil means we never saw a single parseable packet
		s.writeRow(acc.Flush())
	}
	if s.labeler != nil {
		fmt.Println("waiting for remaining predictions...")
		if err := s.labeler.Close(); err != nil {
			fmt.Fprintln(os.Stderr, "predict.py exited with error:", err)
		}
	}
	fmt.Printf("%d windows -> %s\n", s.rowsWritten, s.describe())
}

func (s *sinks) writeRow(row core.WindowRow) {
	values := row.Values()
	if s.stats != nil {
		if err := s.stats.Write(values); err != nil {
			fmt.Fprintln(os.Stderr, "error writing CSV row:", err)
			return
		}
		s.stats.Flush()
	}
	if s.labeler != nil {
		s.labeler.Send(values)
	}
	s.rowsWritten++
	fmt.Printf("window %d closed\n", row.WindowIndex)
}
