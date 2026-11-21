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

// With a 3-window streak: one confident window doesn't tag; an unconfident
// window resets the count; a quiet (unscored) window doesn't; the third
// confident window in a row tags.
func TestPriorityByFlowStreak(t *testing.T) {
	m, _ := newPriorityManager(100, 0.9, time.Minute, "")
	m.streakNeed = 3
	dl := core.FlowKeyOf(pkt(0, 443, 50002, 0, 1500))
	win := func(i int, conf float64) {
		var fl []flowResult
		if conf > 0 {
			fl = []flowResult{{dl, "netflix", conf, 5000, 7e6}}
		}
		m.flowsClosed(i, fl, float64(i+1)*10)
	}
	win(0, 0.99)
	win(1, 0.99)
	win(2, 0.60) // unconfident: reset
	win(3, 0.99)
	win(4, 0.99)
	if len(m.tagged) != 0 {
		t.Fatalf("tagged before 3 confident windows in a row")
	}
	win(5, 0) // quiet: neither counts nor resets
	win(6, 0.99)
	if len(m.tagged) != 1 {
		t.Fatalf("3rd confident window in a row (around a quiet one) didn't tag")
	}
}

// The rate cap: a connection faster than the cap in a window can't build a
// streak (a bulk download), while one under it is tagged as usual.
func TestPriorityByFlowRateCap(t *testing.T) {
	m, _ := newPriorityManager(100, 0.93, time.Minute, "")
	m.streakNeed, m.maxWindowBytes = 3, 25e6/8*10 // 25 Mbit/s over 10 s windows
	download := core.FlowKeyOf(pkt(0, 443, 50002, 0, 1500))
	video := core.FlowKeyOf(pkt(0, 443, 50003, 0, 1500))
	for i := 0; i < 5; i++ {
		m.flowsClosed(i, []flowResult{
			{download, "youtube_tcp", 0.98, 40000, 56e6}, // ~45 Mbit/s
			{video, "twitch", 0.99, 8000, 11e6},          // ~9 Mbit/s
		}, float64(i+1)*10)
	}
	if m.tagged[download] != nil {
		t.Fatalf("a 45 Mbit/s download was tagged despite the 25 Mbit/s cap")
	}
	if m.tagged[video] == nil {
		t.Fatalf("a 9 Mbit/s video wasn't tagged")
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
