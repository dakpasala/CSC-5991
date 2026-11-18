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

func udpPkt(ts float64, srcPort, dstPort, size int) *core.Packet {
	p := pkt(ts, srcPort, dstPort, 0, size)
	p.Protocol = "udp"
	return p
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
	m.windowClosed(0, 50, "netflix", 1.0, feed(m, 0, 50, 443, 0), 10)
	if len(m.tagged) != 0 {
		t.Fatalf("quiet window tagged a flow")
	}
	// Active but unconfident window: no tag.
	m.windowClosed(1, 500, "netflix", 0.6, feed(m, 10, 500, 443, 0), 20)
	if len(m.tagged) != 0 {
		t.Fatalf("low-confidence window tagged a flow")
	}
	// Active, confident window: the flow is tagged.
	m.windowClosed(2, 500, "netflix", 0.99, feed(m, 20, 500, 443, 0), 30)
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
	for port, n := range map[int]int{443: 900, 80: 80, 853: 20} { // video, minor, tiny
		for k, b := range feed(m, 0, n, port, 0) {
			bytes[k] = b
		}
	}
	m.windowClosed(0, 1000, "netflix", 1.0, bytes, 10)
	if len(m.tagged) != 1 {
		t.Fatalf("want only the dominant flow tagged (90%% of bytes), got %d", len(m.tagged))
	}
	for k := range m.tagged {
		if _, port := serverSide(k); port != 443 {
			t.Fatalf("tagged the wrong flow: server port %d", port)
		}
	}
}

// A big SSH transfer sharing a window with a QUIC video must not be tagged,
// even when it outweighs the video in bytes (seen live: 11 MB of git/rsync
// over port 22 tagged as "youtube_quic").
func TestPriorityIgnoresNonServiceFlows(t *testing.T) {
	m, _ := newPriorityManager(100, 0.9, time.Minute, "")
	bytes := map[core.FlowKey]int64{}
	add := func(p *core.Packet) {
		k := core.FlowKeyOf(p)
		bytes[k] += int64(p.WireLen)
		m.observe(p, k)
	}
	for i := 0; i < 2000; i++ {
		add(pkt(float64(i)/200, 22, 50001, 0, 1500)) // SSH: 3 MB, the heaviest flow
	}
	for i := 0; i < 600; i++ {
		add(udpPkt(float64(i)/60, 443, 50002, 1300)) // the QUIC video: 0.8 MB
	}
	for i := 0; i < 400; i++ {
		add(pkt(float64(i)/40, 443, 50003, 0, 1500)) // a TCP HTTPS flow: wrong transport for youtube_quic
	}
	m.windowClosed(0, 3000, "youtube_quic", 1.0, bytes, 10)
	if len(m.tagged) != 1 {
		t.Fatalf("want exactly the QUIC video tagged, got %d tags", len(m.tagged))
	}
	for k := range m.tagged {
		if k.Protocol != "udp" {
			t.Fatalf("tagged a %s flow for youtube_quic", k.Protocol)
		}
	}
	// A window labeled from an SSH transfer alone tags nothing.
	m2, _ := newPriorityManager(100, 0.9, time.Minute, "")
	b2 := map[core.FlowKey]int64{}
	for i := 0; i < 2000; i++ {
		p := pkt(float64(i)/200, 22, 50001, 0, 1500)
		b2[core.FlowKeyOf(p)] += int64(p.WireLen)
	}
	m2.windowClosed(0, 2000, "netflix", 0.98, b2, 10)
	if len(m2.tagged) != 0 {
		t.Fatalf("an SSH-only window tagged %d flows", len(m2.tagged))
	}
}
