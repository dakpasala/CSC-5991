package main

import (
	"testing"
	"time"

	core "csc5991/core_go"
)

func pkt(ts float64, srcPort, dstPort int, flags uint8, size int) *core.Packet {
	return &core.Packet{Timestamp: ts, Protocol: "tcp", SrcIP: "203.0.113.5", DstIP: "192.168.1.188",
		SrcPort: srcPort, DstPort: dstPort, Flags: flags, WireLen: size}
}

// feed sends n packets of a flow spread over [t, t+10) and returns its byte count map.
func feed(m *priorityManager, t float64, n int, srcPort int, flags uint8) map[core.FlowKey]int64 {
	bytes := map[core.FlowKey]int64{}
	for i := 0; i < n; i++ {
		p := pkt(t+float64(i)*10/float64(n), srcPort, 50000, flags, 1500)
		k := core.FlowKeyOf(p)
		bytes[k] += int64(p.WireLen)
		m.observe(p, k)
	}
	return bytes
}

func TestPriorityLifecycle(t *testing.T) {
	m, _ := newPriorityManager(100, 0.9, 30*time.Second, "")

	// Quiet window (50 packets): no tag even at full confidence.
	m.windowClosed(0, 50, "youtube_quic", 1.0, feed(m, 0, 50, 443, 0), 10)
	if len(m.tagged) != 0 {
		t.Fatalf("quiet window tagged a flow")
	}
	// Active but unconfident window: no tag.
	m.windowClosed(1, 500, "youtube_quic", 0.6, feed(m, 10, 500, 443, 0), 20)
	if len(m.tagged) != 0 {
		t.Fatalf("low-confidence window tagged a flow")
	}
	// Active, confident window: the flow is tagged.
	m.windowClosed(2, 500, "youtube_quic", 0.99, feed(m, 20, 500, 443, 0), 30)
	if len(m.tagged) != 1 {
		t.Fatalf("active confident window: want 1 tag, got %d", len(m.tagged))
	}
	// Quiet windows within the idle timeout keep the tag.
	m.windowClosed(3, 5, "netflix", 0.5, map[core.FlowKey]int64{}, 40)
	m.windowClosed(4, 5, "netflix", 0.5, map[core.FlowKey]int64{}, 50)
	if len(m.tagged) != 1 {
		t.Fatalf("tag dropped during a quiet stretch shorter than -priority-idle")
	}
	// Idle past the timeout (last packet ~30s, now 70s): released.
	m.windowClosed(5, 0, "", 0, map[core.FlowKey]int64{}, 70)
	if len(m.tagged) != 0 {
		t.Fatalf("idle flow kept its tag")
	}
	// Re-tag, then a TCP FIN releases it immediately.
	m.windowClosed(6, 500, "twitch", 0.95, feed(m, 70, 500, 443, 0), 80)
	if len(m.tagged) != 1 {
		t.Fatalf("re-tag failed")
	}
	feed(m, 80, 1, 443, core.FlagFIN|core.FlagACK)
	if len(m.tagged) != 0 {
		t.Fatalf("FIN did not release the tag")
	}
	if m.ons != 2 || m.offs != 2 {
		t.Fatalf("want 2 on / 2 off events, got %d / %d", m.ons, m.offs)
	}
}

func TestPriorityPicksHeaviestFlows(t *testing.T) {
	m, _ := newPriorityManager(100, 0.9, time.Minute, "")
	bytes := map[core.FlowKey]int64{}
	for port, n := range map[int]int{443: 900, 8443: 80, 853: 20} { // video, minor, tiny
		for k, b := range feed(m, 0, n, port, 0) {
			bytes[k] = b
		}
	}
	m.windowClosed(0, 1000, "youtube_quic", 1.0, bytes, 10)
	if len(m.tagged) != 1 {
		t.Fatalf("want only the dominant flow tagged (90%% of bytes), got %d", len(m.tagged))
	}
	for k := range m.tagged {
		if _, port := serverSide(k); port != 443 {
			t.Fatalf("tagged the wrong flow: server port %d", port)
		}
	}
}
