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
// Requires root (or equivalent capture capabilities) to open the
// interface -- same privilege requirement as the Python version's tcpdump
// subprocess, just now needed directly by this binary instead.
//
// Usage (run from src/, where go.mod lives):
//
//	sudo go run ./live_capture_go/live -interface eth0
//	sudo go run ./live_capture_go/live -interface eth0 -window-seconds 10 -output live_stats.csv
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

	"github.com/google/gopacket/pcap"
)

const labelPlaceholder = "live_unlabeled"
const snaplen = 200

func main() {
	iface := flag.String("interface", "", `network interface to capture on, e.g. "eth0"`)
	windowSeconds := flag.Float64("window-seconds", 10, "window size in seconds")
	output := flag.String("output", "", "output CSV path (default: timestamped live_statistics_<N>_second_window file)")
	flag.Parse()

	if *iface == "" {
		fmt.Fprintln(os.Stderr, "usage: live -interface <name> [-window-seconds 10] [-output path.csv]")
		os.Exit(1)
	}

	runStartedAt := time.Now().Format("20060102_150405")
	outputPath := *output
	if outputPath == "" {
		outputPath = fmt.Sprintf("live_statistics_%d_second_window_%s.csv", int(*windowSeconds), runStartedAt)
	}

	handle, err := pcap.OpenLive(*iface, snaplen, true, pcap.BlockForever)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error opening interface (needs root/capture capabilities):", err)
		os.Exit(1)
	}
	defer handle.Close()

	f, err := os.OpenFile(outputPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error opening output file:", err)
		os.Exit(1)
	}
	defer f.Close()
	w := csv.NewWriter(f)
	if err := w.Write(core.CSVHeader()); err != nil {
		fmt.Fprintln(os.Stderr, "error writing CSV header:", err)
		os.Exit(1)
	}
	w.Flush()

	sessionID := "live_" + runStartedAt
	var acc *core.WindowAccumulator // created once the first packet's timestamp gives us t0

	pktChan := make(chan *core.Packet, 1024)
	go captureLoop(handle, pktChan)

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	fmt.Printf("capturing on %s, %gs windows -> %s (Ctrl+C to stop)\n", *iface, *windowSeconds, outputPath)

	rowsWritten := 0
	for {
		select {
		case p, ok := <-pktChan:
			if !ok {
				finish(acc, w, &rowsWritten)
				fmt.Printf("\ncapture ended, %d window rows written to %s\n", rowsWritten, outputPath)
				return
			}
			if acc == nil {
				acc = core.NewWindowAccumulator(p.Timestamp, *windowSeconds, sessionID, labelPlaceholder)
			}
			for _, row := range acc.HandlePacket(p) {
				writeRow(w, row, &rowsWritten)
			}
		case <-sigChan:
			fmt.Println("\nstopping capture...")
			finish(acc, w, &rowsWritten)
			fmt.Printf("%d window rows written to %s\n", rowsWritten, outputPath)
			return
		}
	}
}

// captureLoop reads packets off the interface and sends them on a channel
// -- this goroutine never blocks on window computation, matching the
// Python version's "tcpdump never pauses" guarantee, just without needing
// an external process to get it.
func captureLoop(handle *pcap.Handle, out chan<- *core.Packet) {
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

func finish(acc *core.WindowAccumulator, w *csv.Writer, rowsWritten *int) {
	if acc == nil {
		return // never saw a single parseable packet
	}
	writeRow(w, acc.Flush(), rowsWritten)
}

func writeRow(w *csv.Writer, row core.WindowRow, rowsWritten *int) {
	if err := w.Write(row.Values()); err != nil {
		fmt.Fprintln(os.Stderr, "error writing CSV row:", err)
		return
	}
	w.Flush()
	*rowsWritten++
	fmt.Printf("window %d written\n", row.WindowIndex)
}
