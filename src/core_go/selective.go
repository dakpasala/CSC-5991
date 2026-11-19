// SelectiveAccumulator computes only the features a model actually uses,
// updating them one packet at a time, for the router deployment. It
// produces the same values as WindowAccumulator for every column it
// computes (verified column by column in replay), and leaves every other
// column empty in Values().
//
// What it saves, relative to WindowAccumulator:
//   - No per-window packet buffer: WindowAccumulator keeps every packet of
//     the window (windowTagged) until the window closes; here each packet
//     updates counters and is dropped.
//   - Distributions only the model's min/max/mean of: a running
//     min/max/sum/count instead of a buffer of every value (O(1) memory).
//     A full buffer is kept only for a family whose median/p25/p75 the
//     model uses, and nothing at all for a family it doesn't use.
//   - Retransmission/out-of-order/RTT: tracked incrementally per flow (the
//     same state flow_metrics.py builds, advanced once per packet) instead of
//     storing each flow's entire packet history and replaying it at every
//     window close, which is O(history) time and memory that never shrinks.
//     Skipped entirely when the model uses none of those features.
//
// Two documented differences from WindowAccumulator, both below the
// precision that can change a prediction: a streaming mean is summed in
// arrival order rather than sorted order (identical for integer-valued
// metrics, last-bit differences possible for inter-arrival times), and
// streaming inter-arrival gaps are taken between consecutive packets in
// arrival order, where WindowAccumulator sorts timestamps first (identical
// unless the capture delivers packets out of timestamp order).
package core

import (
	"fmt"
	"math"
	"strings"
)

// Accumulator is the interface the live and replay tools use, satisfied by
// both WindowAccumulator (all features) and SelectiveAccumulator.
type Accumulator interface {
	HandlePacket(p *Packet) []WindowRow
	Flush() WindowRow
	PacketCount() int
	RetainedState() (flows, entries int)
}

var distStats = []string{"min", "max", "mean", "median", "p25", "p75"}
var bufferedStats = map[string]bool{"median": true, "p25": true, "p75": true}
var distMetrics = []string{"payload_size", "packet_size", "recv_window", "interarrival_time"}
var directions = []string{"up", "down", "total"}

// featureNeeds is what a feature list requires the accumulator to compute.
type featureNeeds struct {
	columns map[string]bool // every computed column, for Values() masking
	seq     bool            // any retransmission/out-of-order/RTT feature
	dists   map[string]bool // family ("down_payload_size", "rtt_ms") -> needs a full buffer
}

func parseFeatureNeeds(features []string) (*featureNeeds, error) {
	known := map[string]bool{}
	for _, c := range CSVHeader() {
		known[c] = true
	}
	n := &featureNeeds{columns: map[string]bool{}, dists: map[string]bool{}}
	for _, f := range features {
		if !known[f] {
			return nil, fmt.Errorf("unknown feature %q (not a stats column)", f)
		}
		n.columns[f] = true
		if strings.HasPrefix(f, "rtt_ms_") || strings.HasSuffix(f, "_retransmission_count") || strings.HasSuffix(f, "_out_of_order_count") {
			n.seq = true
		}
		i := strings.LastIndex(f, "_")
		family, stat := f[:i], f[i+1:]
		if isDistStat(stat) && isDistFamily(family) {
			n.dists[family] = n.dists[family] || bufferedStats[stat]
		}
	}
	// Always-on diagnostics, whatever the model uses: the live eval reads
	// these to tell active windows from idle ones and to catch router-side
	// packet merging (sizes over 1514B). All O(1) -- counters and a running
	// max -- and they never upgrade a family to a full buffer.
	for _, c := range diagnosticColumns {
		n.columns[c] = true
	}
	for _, family := range []string{"up_packet_size", "down_packet_size"} {
		if _, ok := n.dists[family]; !ok {
			n.dists[family] = false
		}
	}
	return n, nil
}

var diagnosticColumns = []string{"total_packet_count", "frac_udp", "up_packet_size_max", "down_packet_size_max"}

func isDistStat(s string) bool {
	for _, d := range distStats {
		if s == d {
			return true
		}
	}
	return false
}

func isDistFamily(family string) bool {
	if family == "rtt_ms" {
		return true
	}
	for _, d := range directions {
		for _, m := range distMetrics {
			if family == d+"_"+m {
				return true
			}
		}
	}
	return false
}

// distAcc accumulates one distribution family for one window.
type distAcc struct {
	buffered      bool
	vals          []float64
	n             int
	min, max, sum float64
}

func (d *distAcc) add(v float64) {
	if d == nil {
		return
	}
	if d.buffered {
		d.vals = append(d.vals, v)
		return
	}
	if d.n == 0 || v < d.min {
		d.min = v
	}
	if d.n == 0 || v > d.max {
		d.max = v
	}
	d.sum += v
	d.n++
}

func (d *distAcc) result() Distribution {
	if d == nil {
		return NaNDistribution()
	}
	if d.buffered {
		return ComputeDistribution(d.vals)
	}
	if d.n == 0 {
		return NaNDistribution()
	}
	nan := math.NaN()
	return Distribution{Min: d.min, Max: d.max, Mean: d.sum / float64(d.n), Median: nan, P25: nan, P75: nan}
}

// dirAcc accumulates one direction ("up"/"down"/"total") for one window.
type dirAcc struct {
	packets, retrans, ooo int
	bytes                 int64
	flags                 [5]int // TCPFlags order
	payload, size, recv   *distAcc
	gaps                  *distAcc
	gapTs                 []float64 // only when gaps is buffered: timestamps, sorted at close like interarrivalMs does
	lastTs                float64
	haveTs                bool
}

// flowSeq is one TCP flow's incremental retransmission/out-of-order/RTT
// state: exactly what ComputeFlowMetrics builds while replaying the flow,
// kept between packets instead of rebuilt from the full history.
type flowSeq struct {
	tcp         bool
	state       map[string]*directionState
	outstanding map[string]map[uint32]float64
}

type SelectiveAccumulator struct {
	T0            float64
	WindowSeconds float64
	SessionID     string
	Label         string

	needs   *featureNeeds
	tracker *FlowTracker
	seq     map[FlowKey]*flowSeq

	currentWindowIndex int
	packets, udp, newF int
	flows              map[FlowKey]struct{}
	dirs               map[string]*dirAcc
	rtt                *distAcc
}

// NewAccumulatorFor returns a SelectiveAccumulator for these features, or
// the all-features WindowAccumulator when features is nil.
func NewAccumulatorFor(features []string, t0, windowSeconds float64, sessionID, label string) (Accumulator, error) {
	if features == nil {
		return NewWindowAccumulator(t0, windowSeconds, sessionID, label), nil
	}
	return NewSelectiveAccumulator(features, t0, windowSeconds, sessionID, label)
}

// NewSelectiveAccumulator builds an accumulator for exactly these feature
// columns (a model's input list); an unknown name is an error.
func NewSelectiveAccumulator(features []string, t0, windowSeconds float64, sessionID, label string) (*SelectiveAccumulator, error) {
	needs, err := parseFeatureNeeds(features)
	if err != nil {
		return nil, err
	}
	a := &SelectiveAccumulator{
		T0: t0, WindowSeconds: windowSeconds, SessionID: sessionID, Label: label,
		needs: needs, tracker: NewFlowTracker(), seq: map[FlowKey]*flowSeq{},
	}
	a.resetWindow()
	return a, nil
}

func (a *SelectiveAccumulator) newDist(family string) *distAcc {
	buffered, ok := a.needs.dists[family]
	if !ok {
		return nil
	}
	return &distAcc{buffered: buffered}
}

func (a *SelectiveAccumulator) resetWindow() {
	a.packets, a.udp, a.newF = 0, 0, 0
	a.flows = map[FlowKey]struct{}{}
	a.dirs = map[string]*dirAcc{}
	for _, d := range directions {
		a.dirs[d] = &dirAcc{
			payload: a.newDist(d + "_payload_size"),
			size:    a.newDist(d + "_packet_size"),
			recv:    a.newDist(d + "_recv_window"),
			gaps:    a.newDist(d + "_interarrival_time"),
		}
	}
	a.rtt = a.newDist("rtt_ms")
}

func (a *SelectiveAccumulator) HandlePacket(p *Packet) []WindowRow {
	var closed []WindowRow
	pktWindowIndex := int((p.Timestamp - a.T0) / a.WindowSeconds)
	for pktWindowIndex > a.currentWindowIndex {
		closed = append(closed, a.closeWindow())
		a.currentWindowIndex++
	}

	flowKey, direction, isNewFlow := a.tracker.Process(p)
	var retrans, ooo bool
	if a.needs.seq {
		retrans, ooo = a.advanceSeq(flowKey, p, direction)
	}

	a.packets++
	if p.Protocol == "udp" {
		a.udp++
	}
	if isNewFlow {
		a.newF++
	}
	a.flows[flowKey] = struct{}{}
	for _, d := range []string{direction, "total"} {
		a.addToDirection(a.dirs[d], p, retrans, ooo)
	}
	return closed
}

func (a *SelectiveAccumulator) addToDirection(d *dirAcc, p *Packet, retrans, ooo bool) {
	d.packets++
	d.bytes += int64(p.WireLen)
	if retrans {
		d.retrans++
	}
	if ooo {
		d.ooo++
	}
	for i, flag := range TCPFlags {
		if p.Flags&flag.Bit != 0 {
			d.flags[i]++
		}
	}
	d.payload.add(float64(p.PayloadSize))
	d.size.add(float64(p.WireLen))
	if p.Protocol == "tcp" {
		d.recv.add(float64(p.Window))
	}
	if d.gaps != nil && d.gaps.buffered {
		d.gapTs = append(d.gapTs, p.Timestamp)
	} else if d.haveTs {
		d.gaps.add((p.Timestamp - d.lastTs) * 1000)
	}
	d.lastTs, d.haveTs = p.Timestamp, true
}

// advanceSeq is one iteration of ComputeFlowMetrics's loop, run against the
// flow's persistent state; an RTT sample goes to the current window, as the
// sample's timestamp (this ACK's) always falls in it.
func (a *SelectiveAccumulator) advanceSeq(key FlowKey, packet *Packet, direction string) (bool, bool) {
	fs, ok := a.seq[key]
	if !ok {
		// ComputeFlowMetrics decides TCP-or-not from the flow's first packet.
		fs = &flowSeq{tcp: packet.Protocol == "tcp"}
		if fs.tcp {
			fs.state = map[string]*directionState{"up": newDirectionState(), "down": newDirectionState()}
			fs.outstanding = map[string]map[uint32]float64{"up": {}, "down": {}}
		}
		a.seq[key] = fs
	}
	if !fs.tcp {
		return false, false
	}
	state := fs.state[direction]
	seq := packet.Seq
	var isRetransmission, isOutOfOrder bool
	consumed := uint32(packet.PayloadSize)
	if packet.Flag(FlagSYN) {
		consumed++
	}
	if consumed != 0 {
		if _, seen := state.seenSeqs[seq]; seen {
			isRetransmission = true
		} else {
			state.seenSeqs[seq] = struct{}{}
			if state.baseSeq == nil {
				base := seq
				state.baseSeq = &base
			}
			relative := seqDelta(seq, *state.baseSeq)
			if state.maxRelativeSeq != nil && relative < *state.maxRelativeSeq {
				isOutOfOrder = true
			} else {
				state.maxRelativeSeq = &relative
			}
		}
		fs.outstanding[direction][seqDelta(seq+consumed, 0)] = packet.Timestamp
	}
	if packet.Flag(FlagACK) {
		other := "up"
		if direction == "up" {
			other = "down"
		}
		expectedAck := seqDelta(packet.Ack, 0)
		if sentTs, ok := fs.outstanding[other][expectedAck]; ok {
			delete(fs.outstanding[other], expectedAck)
			a.rtt.add((packet.Timestamp - sentTs) * 1000)
		}
	}
	return isRetransmission, isOutOfOrder
}

// AdvanceTo closes every window before windowIndex and returns them, so
// several accumulators (e.g. one per connection) can be driven off one
// clock: when the main accumulator closes window N, each per-connection
// accumulator is advanced to N+1 to get its own row for window N.
func (a *SelectiveAccumulator) AdvanceTo(windowIndex int) []WindowRow {
	var closed []WindowRow
	for a.currentWindowIndex < windowIndex {
		closed = append(closed, a.closeWindow())
		a.currentWindowIndex++
	}
	return closed
}

// StartAt moves a still-empty accumulator's open window to windowIndex
// without emitting the empty windows before it, so a connection first seen
// an hour in doesn't close 360 empty windows on its first packet.
func (a *SelectiveAccumulator) StartAt(windowIndex int) {
	if a.packets == 0 && windowIndex > a.currentWindowIndex {
		a.currentWindowIndex = windowIndex
	}
}

func (a *SelectiveAccumulator) Flush() WindowRow {
	return a.closeWindow()
}

func (a *SelectiveAccumulator) PacketCount() int {
	return a.packets
}

// RetainedState reports flows tracked and per-flow sequence entries held
// (seen sequence numbers plus unacknowledged segments) -- the only state
// kept across windows, and zero entries when no seq feature is needed.
func (a *SelectiveAccumulator) RetainedState() (flows, entries int) {
	for _, fs := range a.seq {
		if fs.tcp {
			for _, d := range []string{"up", "down"} {
				entries += len(fs.state[d].seenSeqs) + len(fs.outstanding[d])
			}
		}
	}
	return len(a.tracker.Flows()), entries
}

func (a *SelectiveAccumulator) closeWindow() WindowRow {
	idx := a.currentWindowIndex
	row := WindowRow{
		Label: a.Label, SessionID: a.SessionID, WindowIndex: idx,
		WindowStartS: float64(idx) * a.WindowSeconds,
		WindowEndS:   float64(idx+1) * a.WindowSeconds,
		only:         a.needs.columns,
	}
	if a.packets == 0 {
		row.FracUDP = math.NaN()
	} else {
		row.NumFlows = len(a.flows)
		row.NumNewFlows = a.newF
		row.NumContinuing = row.NumFlows - row.NumNewFlows
		row.FracUDP = float64(a.udp) / float64(a.packets)
	}
	row.Up = a.dirs["up"].stats(a.WindowSeconds)
	row.Down = a.dirs["down"].stats(a.WindowSeconds)
	row.Total = a.dirs["total"].stats(a.WindowSeconds)
	row.RTT = a.rtt.result()
	row.SegmentSizeBytes = NaNDistribution()
	row.RequestDurationMs = NaNDistribution()
	a.resetWindow()
	return row
}

func (d *dirAcc) stats(windowSeconds float64) DirectionStats {
	s := DirectionStats{
		PacketCount: d.packets, ByteCount: d.bytes,
		ThroughputBps:       float64(d.bytes) * 8 / windowSeconds,
		RetransmissionCount: d.retrans, OutOfOrderCount: d.ooo,
		FlagCounts:         make(map[string]int, len(TCPFlags)),
		PayloadSize:        d.payload.result(),
		PacketSize:         d.size.result(),
		RecvWindow:         d.recv.result(),
		InterarrivalTimeMs: d.gaps.result(),
	}
	if d.gaps != nil && d.gaps.buffered {
		// Exactly WindowAccumulator's path: sort the timestamps, then diff.
		records := make([]PacketRecord, len(d.gapTs))
		for i, ts := range d.gapTs {
			records[i].Timestamp = ts
		}
		s.InterarrivalTimeMs = ComputeDistribution(interarrivalMs(records))
	}
	for i, flag := range TCPFlags {
		s.FlagCounts[flag.Name] = d.flags[i]
	}
	return s
}
