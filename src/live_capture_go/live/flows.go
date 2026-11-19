package main

import (
	"encoding/csv"
	"os"
	"sort"
	"strconv"

	core "csc5991/core_go"
	"csc5991/live_capture_go/inference"
)

// flowScorer classifies each connection on its own: it keeps a separate
// SelectiveAccumulator per connection (the same 28-or-so model features,
// computed from that connection's packets only, on the same 10 s clock as
// the main window) and, when a window closes, scores every connection that
// carried >= minPackets packets in it.
//
// Why: the window label describes all of the traffic on the port at once,
// so a burst of unrelated traffic (16 parallel downloads in the QoS test)
// can make the whole window look like "netflix" and get those downloads
// prioritized. Scored on their own, connections can't borrow each other's
// statistics. Tested offline first on the 7 recorded sessions: the main
// connection alone was labeled 1.00 for YouTube (TCP and QUIC) and Netflix,
// 0.94 for Twitch and 0.84 for Amazon -- the dataset's sessions were one
// service at a time, so a single connection already looks like what the
// model learned.
//
// Cost: no buffers and no RTT state (the model's features are streaming
// stats), so a few hundred bytes per active connection; connections idle
// longer than evictAfter are dropped.
type flowScorer struct {
	native     *inference.Native
	features   []string
	t0, ws     float64
	minPackets int
	evictAfter float64

	accs     map[core.FlowKey]*core.SelectiveAccumulator
	lastSeen map[core.FlowKey]float64
	log      *csv.Writer
	logF     *os.File
}

type flowResult struct {
	key     core.FlowKey
	label   string
	conf    float64
	packets int
	bytes   int64
}

func newFlowScorer(native *inference.Native, windowSeconds float64, minPackets int, evictAfter float64, logPath string) (*flowScorer, error) {
	f := &flowScorer{
		native: native, features: native.Features(), ws: windowSeconds, minPackets: minPackets, evictAfter: evictAfter,
		accs: map[core.FlowKey]*core.SelectiveAccumulator{}, lastSeen: map[core.FlowKey]float64{},
	}
	if logPath != "" {
		fh, err := os.Create(logPath)
		if err != nil {
			return nil, err
		}
		f.logF, f.log = fh, csv.NewWriter(fh)
		// No IP addresses (project rule for written outputs).
		f.log.Write([]string{"window_index", "flow_id", "protocol", "server_port", "packets", "bytes", "predicted_label", "confidence"})
		f.log.Flush()
	}
	return f, nil
}

// observe adds a packet to its connection's accumulator (call after the
// main accumulator has handled it, so window N is already closed there).
func (f *flowScorer) observe(p *core.Packet, key core.FlowKey, t0 float64) {
	acc, ok := f.accs[key]
	if !ok {
		acc, _ = core.NewSelectiveAccumulator(f.features, t0, f.ws, "flow", "")
		acc.StartAt(int((p.Timestamp - t0) / f.ws))
		f.accs[key] = acc
	}
	acc.HandlePacket(p) // its own closed rows are collected in windowClosed via AdvanceTo
	f.lastSeen[key] = p.Timestamp
}

// windowClosed returns every connection's own prediction for window N, for
// the connections that carried >= minPackets packets in it, heaviest first.
func (f *flowScorer) windowClosed(window int, now float64) []flowResult {
	var out []flowResult
	for key, acc := range f.accs {
		for _, row := range acc.AdvanceTo(window + 1) {
			if row.WindowIndex != window || row.Total.PacketCount < f.minPackets {
				continue
			}
			p := f.native.Score(row.Values())
			out = append(out, flowResult{key, p.Label, p.Confidence, row.Total.PacketCount, row.Total.ByteCount})
		}
		if now-f.lastSeen[key] > f.evictAfter {
			delete(f.accs, key)
			delete(f.lastSeen, key)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].bytes > out[j].bytes })
	if f.log != nil {
		for _, r := range out {
			_, port := serverSide(r.key)
			f.log.Write([]string{strconv.Itoa(window), flowID(r.key), r.key.Protocol, strconv.Itoa(port),
				strconv.Itoa(r.packets), strconv.FormatInt(r.bytes, 10), r.label, strconv.FormatFloat(r.conf, 'f', 4, 64)})
		}
		f.log.Flush()
	}
	return out
}

func (f *flowScorer) close() {
	if f.logF != nil {
		f.log.Flush()
		f.logF.Close()
	}
}
