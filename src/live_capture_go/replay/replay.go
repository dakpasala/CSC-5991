// Go port of src/live_capture/replay_pcap_as_live.py -- replays an existing
// session's dump.pcap as if arriving live, in real wall-clock time, to
// validate the pipeline without needing root or a live interface. Same
// architecture as the Python version, just goroutines+channel instead of
// threads+queue (Go doesn't have Python's GIL problem, so this concurrency
// is exactly what real router software would look like, not a workaround).
//
// Window boundaries are decided by PACKET TIMESTAMPS, not wall-clock
// polling: the moment a packet's timestamp crosses into the next
// window_seconds bucket, the just-completed window is closed, its stats
// computed and written, and accumulation starts fresh -- while the replayer
// goroutine keeps delivering the next window's packets concurrently the
// whole time, so a slow computation can never cause a miss.
//
// Same two known simplifications as the Python version: retransmission/RTT
// detection is recomputed on each flow's full history-so-far at every
// window close (not incrementally updated), and an RTT sample whose ACK
// arrives in a later window than its data packet is attributed to
// whichever window is open when detected, not retroactively added to an
// already-closed earlier row.
//
// Usage (run from src/, where go.mod lives):
//
//	go run ./live_capture_go/replay -pcap ../sessions/<id>/dump.pcap
//	go run ./live_capture_go/replay -pcap ../sessions/<id>/dump.pcap -speed 4 -window-seconds 10
//	go run ./live_capture_go/replay -pcap ../sessions/<id>/dump.pcap -speed 20 -model-dir ../AutogluonModels/window_10s_19468sessions_no_applayer_xgb22
//
// With -model-dir, each closed window is also labeled and appended to
// live_runs/replay_predictions_<N>_second_window.csv (see
// live_capture_go/inference): natively in Go for an XGBoost .json model,
// through predict.py for an AutoGluon directory. The stats CSV is skipped in that mode
// unless -output is given explicitly.
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	core "csc5991/core_go"
	"csc5991/live_capture_go/inference"

	"github.com/google/gopacket/pcapgo"
)

func loadPackets(path string) ([]*core.Packet, float64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, err
	}
	defer f.Close()

	reader, err := pcapgo.NewReader(f)
	if err != nil {
		return nil, 0, fmt.Errorf("opening pcap: %w", err)
	}

	var packets []*core.Packet
	for {
		data, ci, err := reader.ReadPacketData()
		if err != nil {
			break // EOF or any other read error -- end of file either way
		}
		ts := float64(ci.Timestamp.UnixNano()) / 1e9
		if p := core.ParsePacket(data, ts, ci.Length); p != nil {
			packets = append(packets, p)
		}
	}
	if len(packets) == 0 {
		return nil, 0, fmt.Errorf("no parseable TCP/UDP packets found in %s", path)
	}
	return packets, packets[0].Timestamp, nil
}

// replayPackets paces packets out on a channel at their original relative
// timing (scaled by speed), closing the channel when done -- the
// idiomatic Go equivalent of the Python version's None sentinel.
func replayPackets(packets []*core.Packet, speed float64, out chan<- *core.Packet) {
	prevTimestamp := packets[0].Timestamp
	for _, p := range packets {
		delay := (p.Timestamp - prevTimestamp) / speed
		if delay > 0 {
			time.Sleep(time.Duration(delay * float64(time.Second)))
		}
		prevTimestamp = p.Timestamp
		out <- p
	}
	close(out)
}

func main() {
	pcapPath := flag.String("pcap", "", "path to an existing session's dump.pcap")
	windowSeconds := flag.Float64("window-seconds", 10, "window size in seconds")
	speed := flag.Float64("speed", 1.0, "replay speed multiplier (1.0 = real-time)")
	output := flag.String("output", "", "stats CSV path (default: replay_statistics_<N>_second_window.csv; skipped with -model-dir unless given)")
	inferenceFlags := inference.RegisterFlags()
	portDirection := flag.Bool("port-direction", false, "assign a flow first seen from a service port (<1024) as server->client (core.DirectionByServicePort); default false = exact flow_tracker.py rule, matching the training data")
	flag.Parse()
	core.DirectionByServicePort = *portDirection

	if *pcapPath == "" {
		fmt.Fprintln(os.Stderr, "usage: replay -pcap <path to dump.pcap> [-window-seconds 10] [-speed 1.0] [-output path.csv]")
		os.Exit(1)
	}

	outputPath := *output
	if outputPath == "" && !inferenceFlags.Enabled() {
		os.MkdirAll("live_runs", 0755)
		outputPath = fmt.Sprintf("live_runs/replay_statistics_%d_second_window.csv", int(*windowSeconds))
	}
	sessionID := filepath.Base(filepath.Dir(*pcapPath))
	if sessionID == "" || sessionID == "." {
		sessionID = "replay"
	}

	packets, t0, err := loadPackets(*pcapPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	fmt.Printf("loaded %d packets from %s, replaying at %gx speed\n", len(packets), *pcapPath, *speed)

	pktChan := make(chan *core.Packet, 1024)
	go replayPackets(packets, *speed, pktChan)

	var labeler inference.Labeler
	predictionsPath := ""
	if inferenceFlags.Enabled() {
		os.MkdirAll("live_runs", 0755)
		cfg := inferenceFlags.Config(fmt.Sprintf("live_runs/replay_predictions_%d_second_window.csv", int(*windowSeconds)))
		labeler, err = inference.StartLabeler(cfg, core.CSVHeader())
		if err != nil {
			fmt.Fprintln(os.Stderr, "error starting inference:", err)
			os.Exit(1)
		}
		predictionsPath = cfg.OutputPath
	}

	extract := inference.ExtractionFeatures(labeler, *inferenceFlags.AllFeatures)
	if extract != nil {
		fmt.Printf("computing only the model's %d features (-all-features to compute all of them)\n", len(extract))
	}
	rw := newReplayWriter(t0, *windowSeconds, sessionID, outputPath, *speed, labeler, extract)

	for p := range pktChan {
		rw.handlePacket(p)
	}
	rw.flush() // the final, partial window
	rw.close()

	fmt.Printf("\nreplay complete, %d windows", rw.totalRowsWritten)
	if outputPath != "" {
		fmt.Printf(", stats -> %s", outputPath)
	}
	if labeler != nil {
		fmt.Println("\nwaiting for remaining predictions...")
		if err := labeler.Close(); err != nil {
			fmt.Fprintln(os.Stderr, "predict.py exited with error:", err)
		}
		fmt.Printf("predictions -> %s", predictionsPath)
	}
	fmt.Println()
}
