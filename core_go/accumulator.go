// WindowAccumulator is the shared "what to do with each packet" state
// machine used by both the replay tool and the true live-capture tool --
// only where packets come from differs between them. Mirrors the window-
// closing logic in src/live_capture/replay_pcap_as_live.py exactly,
// including its documented simplifications (retransmission/RTT state
// recomputed on each flow's full history-so-far at every window close; an
// RTT sample whose ACK arrives in a later window than its data packet is
// attributed to whichever window is open when detected, not retroactively
// added to an already-closed earlier row).
package core

import "math"

type taggedEntry struct {
	direction string
	protocol  string
	wireLen   int
	payload   int
	recvWin   float64
	timestamp float64
	flags     uint8
	isNewFlow bool
	flowKey   FlowKey
}

type WindowAccumulator struct {
	T0            float64
	WindowSeconds float64
	SessionID     string
	Label         string

	tracker             *FlowTracker
	flowPacketLists     map[FlowKey][]FlowPacket
	flowCursor          map[FlowKey]int
	windowTagged        []taggedEntry
	currentWindowIndex  int
}

func NewWindowAccumulator(t0, windowSeconds float64, sessionID, label string) *WindowAccumulator {
	return &WindowAccumulator{
		T0:              t0,
		WindowSeconds:   windowSeconds,
		SessionID:       sessionID,
		Label:           label,
		tracker:         NewFlowTracker(),
		flowPacketLists: make(map[FlowKey][]FlowPacket),
		flowCursor:      make(map[FlowKey]int),
	}
}

func recvWindowOrNaN(p *Packet) float64 {
	if p.Protocol != "tcp" {
		return math.NaN()
	}
	return float64(p.Window)
}

// HandlePacket processes one packet (in chronological order) and returns
// any WindowRow(s) that got closed out as a result -- normally none (still
// accumulating the current window) or one (this packet crossed into the
// next window), but can be more than one if a gap in traffic meant several
// window boundaries were crossed at once.
func (a *WindowAccumulator) HandlePacket(p *Packet) []WindowRow {
	var closed []WindowRow

	pktWindowIndex := int((p.Timestamp - a.T0) / a.WindowSeconds)
	for pktWindowIndex > a.currentWindowIndex {
		closed = append(closed, a.closeWindow(a.currentWindowIndex))
		a.currentWindowIndex++
	}

	flowKey, direction, isNewFlow := a.tracker.Process(p)
	a.windowTagged = append(a.windowTagged, taggedEntry{
		direction: direction,
		protocol:  p.Protocol,
		wireLen:   p.WireLen,
		payload:   p.PayloadSize,
		recvWin:   recvWindowOrNaN(p),
		timestamp: p.Timestamp,
		flags:     p.Flags,
		isNewFlow: isNewFlow,
		flowKey:   flowKey,
	})
	a.flowPacketLists[flowKey] = append(a.flowPacketLists[flowKey], FlowPacket{Packet: p, Direction: direction})

	return closed
}

// Flush closes out whatever window is currently accumulating -- call once
// at the end of a capture/replay to get the final, possibly-partial window.
func (a *WindowAccumulator) Flush() WindowRow {
	return a.closeWindow(a.currentWindowIndex)
}

// PacketCount reports how many packets the window currently being
// accumulated has seen so far -- purely informational (e.g. for progress
// logging), not used in any stat computation.
func (a *WindowAccumulator) PacketCount() int {
	return len(a.windowTagged)
}

func (a *WindowAccumulator) closeWindow(targetWindowIndex int) WindowRow {
	touchedFlows := map[FlowKey]struct{}{}
	for _, entry := range a.windowTagged {
		touchedFlows[entry.flowKey] = struct{}{}
	}

	eventFlagsByFlow := make(map[FlowKey][]EventFlags, len(touchedFlows))
	rttByWindow := make(map[int][]float64)
	for flowKey := range touchedFlows {
		flags, rtts := ComputeFlowMetrics(a.flowPacketLists[flowKey])
		eventFlagsByFlow[flowKey] = flags
		for _, sample := range rtts {
			windowIdx := int((sample.Timestamp - a.T0) / a.WindowSeconds)
			rttByWindow[windowIdx] = append(rttByWindow[windowIdx], sample.RTTMs)
		}
	}

	packetRecords := make([]PacketRecord, len(a.windowTagged))
	flowFirstWindowCount := 0
	for i, entry := range a.windowTagged {
		idx := a.flowCursor[entry.flowKey]
		flags := eventFlagsByFlow[entry.flowKey][idx]
		a.flowCursor[entry.flowKey]++

		if entry.isNewFlow {
			flowFirstWindowCount++
		}

		packetRecords[i] = PacketRecord{
			Direction:        entry.direction,
			Protocol:         entry.protocol,
			WireLen:          entry.wireLen,
			PayloadSize:      entry.payload,
			RecvWindow:       entry.recvWin,
			Timestamp:        entry.timestamp,
			Flags:            entry.flags,
			IsNewFlow:        entry.isNewFlow,
			FlowKey:          entry.flowKey,
			IsRetransmission: flags.IsRetransmission,
			IsOutOfOrder:     flags.IsOutOfOrder,
		}
	}

	row := BuildOneWindowRow(
		packetRecords, flowFirstWindowCount, rttByWindow[targetWindowIndex],
		targetWindowIndex, a.WindowSeconds, a.Label, a.SessionID,
	)

	a.windowTagged = nil
	return row
}
