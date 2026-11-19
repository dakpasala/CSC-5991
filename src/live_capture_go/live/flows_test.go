package main

import (
	"os"
	"testing"
	"time"

	core "csc5991/core_go"
	"csc5991/live_capture_go/inference"

	"github.com/google/gopacket/pcapgo"
)

// The per-connection rule tags a connection by its OWN label: a confident
// streaming label on a matching port/transport tags it; a download whose own
// label is unconfident, or a confident label on the wrong transport, doesn't.
func TestPriorityByFlowOwnLabel(t *testing.T) {
	m, _ := newPriorityManager(100, 0.9, time.Minute, "")
	video := core.FlowKeyOf(udpPkt(0, 443, 50001, 1300))
	download := core.FlowKeyOf(pkt(0, 443, 50002, 0, 1500))
	ssh := core.FlowKeyOf(pkt(0, 22, 50003, 0, 1500))
	m.flowsClosed(0, []flowResult{
		{download, "netflix", 0.62, 9000, 13e6}, // heaviest, but its own label is unsure
		{video, "youtube_quic", 0.99, 600, 8e5},
		{ssh, "netflix", 0.99, 2000, 3e6}, // confident, but port 22
	}, 10)
	if len(m.tagged) != 1 || m.tagged[video] == nil {
		t.Fatalf("want only the video tagged, got %d tags", len(m.tagged))
	}
	// Seen again confidently: still one tag, no duplicate ON event.
	m.flowsClosed(1, []flowResult{{video, "youtube_quic", 0.97, 600, 8e5}}, 20)
	if m.ons != 1 {
		t.Fatalf("re-confirming a tagged connection fired another ON (%d)", m.ons)
	}
}

// End to end on a recorded session (skipped if it isn't present): drive the
// main window accumulator and the flowScorer exactly as live-router does,
// and check the session's main connection gets its own correct label.
func TestFlowScorerOnRecordedSession(t *testing.T) {
	const model = "../../../XGBoostModels/10s_no_applayer/final_shape_streaming.json"
	const pcap = "../../../sessions/a03921b7cdc37b4f42965503297b4be9/dump.pcap" // lab09_youtube, QUIC
	f, err := os.Open(pcap)
	if err != nil {
		t.Skip("recorded session not present")
	}
	defer f.Close()
	native, err := inference.StartNative(model, t.TempDir()+"/pred.csv", core.CSVHeader())
	if err != nil {
		t.Skip("model not present: ", err)
	}
	r, _ := pcapgo.NewReader(f)

	var main core.Accumulator
	var fs *flowScorer
	var t0 float64
	right, total := 0, 0
	closed := func(rows []core.WindowRow) {
		for _, row := range rows {
			for _, res := range fs.windowClosed(row.WindowIndex, row.WindowEndS+t0) {
				if res.bytes > 500000 {
					total++
					if res.label == "youtube_quic" {
						right++
					}
				}
			}
		}
	}
	for {
		data, ci, err := r.ReadPacketData()
		if err != nil {
			break
		}
		p := core.ParsePacket(data, float64(ci.Timestamp.UnixNano())/1e9, ci.Length)
		if p == nil {
			continue
		}
		if main == nil {
			t0 = p.Timestamp
			main, _ = core.NewAccumulatorFor(native.Features(), t0, 10, "s", "")
			fs, _ = newFlowScorer(native, 10, 100, 120, "")
		}
		closed(main.HandlePacket(p))
		fs.observe(p, core.FlowKeyOf(p), t0)
	}
	if total < 30 {
		t.Fatalf("only %d heavy connection-windows scored", total)
	}
	if acc := float64(right) / float64(total); acc < 0.95 {
		t.Fatalf("main connection's own label right in %.2f of %d windows", acc, total)
	}
	t.Logf("main QUIC connection labeled youtube_quic on its own in %d/%d windows", right, total)
}
