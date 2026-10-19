// Mirrors src/core/window_features.py's per-window row construction
// (_direction_stats / _build_one_window_row), minus the request-log
// application-layer stats -- there's no requests_history.json on a live
// capture, so those 13 columns are always emitted as zero/NaN, matching
// exactly what live_capture_pipeline.py (the Python version) already does.
package core

import (
	"math"
	"sort"
)

// TCPFlags matches window_features.py's TCP_FLAGS tuple exactly (FIN is
// deliberately excluded, same as the Python version).
var TCPFlags = []struct {
	Name string
	Bit  uint8
}{
	{"syn", FlagSYN},
	{"ack", FlagACK},
	{"rst", FlagRST},
	{"psh", FlagPSH},
	{"urg", FlagURG},
}

// PacketRecord mirrors one "tagged" dict row from _parse_and_track_flows.
type PacketRecord struct {
	Direction        string
	Protocol         string
	WireLen          int
	PayloadSize      int
	RecvWindow       float64 // math.NaN() for UDP, matching the Python pipeline
	Timestamp        float64
	Flags            uint8
	IsNewFlow        bool
	FlowKey          FlowKey
	IsRetransmission bool
	IsOutOfOrder     bool
}

// WindowRow is the full column set for one window, built with the same
// column ORDER build_session_csv.py's CSV uses (comparison tooling aligns
// by name, not position, but matching order makes a visual diff easier).
type WindowRow struct {
	Label         string
	SessionID     string
	WindowIndex   int
	WindowStartS  float64
	WindowEndS    float64
	NumFlows      int
	NumNewFlows   int
	NumContinuing int
	FracUDP       float64

	Up    DirectionStats
	Down  DirectionStats
	Total DirectionStats

	RTT Distribution

	// Application-layer: always zero/NaN on a live capture (no
	// requests_history.json exists), kept for CSV-schema parity with the
	// Python live pipeline's output.
	RequestCount           int
	SegmentSizeBytes       Distribution
	RequestDurationMs      Distribution
}

type DirectionStats struct {
	PacketCount          int
	ByteCount             int64
	ThroughputBps         float64
	RetransmissionCount   int
	OutOfOrderCount       int
	FlagCounts            map[string]int // keyed by TCPFlags[i].Name
	PayloadSize           Distribution
	PacketSize            Distribution
	RecvWindow            Distribution
	InterarrivalTimeMs    Distribution
}

// ComputeDirectionStats mirrors _direction_stats(group, prefix, window_seconds).
func ComputeDirectionStats(group []PacketRecord, windowSeconds float64) DirectionStats {
	stats := DirectionStats{
		PacketCount: len(group),
		FlagCounts:  make(map[string]int, len(TCPFlags)),
	}

	var byteSum int64
	for _, p := range group {
		byteSum += int64(p.WireLen)
	}
	stats.ByteCount = byteSum
	stats.ThroughputBps = float64(byteSum) * 8 / windowSeconds

	for _, p := range group {
		if p.IsRetransmission {
			stats.RetransmissionCount++
		}
		if p.IsOutOfOrder {
			stats.OutOfOrderCount++
		}
	}

	for _, flag := range TCPFlags {
		count := 0
		for _, p := range group {
			if p.Flags&flag.Bit != 0 {
				count++
			}
		}
		stats.FlagCounts[flag.Name] = count
	}

	payloadSizes := make([]float64, len(group))
	packetSizes := make([]float64, len(group))
	var recvWindows []float64
	for i, p := range group {
		payloadSizes[i] = float64(p.PayloadSize)
		packetSizes[i] = float64(p.WireLen)
		if !math.IsNaN(p.RecvWindow) {
			recvWindows = append(recvWindows, p.RecvWindow)
		}
	}
	stats.PayloadSize = ComputeDistribution(payloadSizes)
	stats.PacketSize = ComputeDistribution(packetSizes)
	stats.RecvWindow = ComputeDistribution(recvWindows)
	stats.InterarrivalTimeMs = ComputeDistribution(interarrivalMs(group))

	return stats
}

// interarrivalMs mirrors group["timestamp"].sort_values().diff().dropna() * 1000.
func interarrivalMs(group []PacketRecord) []float64 {
	if len(group) < 2 {
		return nil
	}
	timestamps := make([]float64, len(group))
	for i, p := range group {
		timestamps[i] = p.Timestamp
	}
	// sort.Float64s is used rather than relying on input order, matching
	// the Python pipeline's explicit .sort_values() before diff().
	sort.Float64s(timestamps)

	diffs := make([]float64, 0, len(timestamps)-1)
	for i := 1; i < len(timestamps); i++ {
		diffs = append(diffs, (timestamps[i]-timestamps[i-1])*1000)
	}
	return diffs
}

// BuildOneWindowRow mirrors _build_one_window_row exactly (requests always
// empty -- see package docstring).
func BuildOneWindowRow(windowPackets []PacketRecord, flowFirstWindowCount int, rttValues []float64, windowIndex int, windowSeconds float64, label, sessionID string) WindowRow {
	row := WindowRow{
		Label:        label,
		SessionID:    sessionID,
		WindowIndex:  windowIndex,
		WindowStartS: float64(windowIndex) * windowSeconds,
		WindowEndS:   float64(windowIndex+1) * windowSeconds,
	}

	if len(windowPackets) == 0 {
		row.FracUDP = math.NaN()
	} else {
		flowSet := make(map[FlowKey]struct{})
		udpCount := 0
		for _, p := range windowPackets {
			flowSet[p.FlowKey] = struct{}{}
			if p.Protocol == "udp" {
				udpCount++
			}
		}
		row.NumFlows = len(flowSet)
		row.NumNewFlows = flowFirstWindowCount
		row.NumContinuing = row.NumFlows - row.NumNewFlows
		row.FracUDP = float64(udpCount) / float64(len(windowPackets))
	}

	var up, down []PacketRecord
	for _, p := range windowPackets {
		if p.Direction == "up" {
			up = append(up, p)
		} else {
			down = append(down, p)
		}
	}
	row.Up = ComputeDirectionStats(up, windowSeconds)
	row.Down = ComputeDirectionStats(down, windowSeconds)
	row.Total = ComputeDirectionStats(windowPackets, windowSeconds)

	row.RTT = ComputeDistribution(rttValues)

	// Application-layer: always empty on a live capture.
	row.RequestCount = 0
	row.SegmentSizeBytes = NaNDistribution()
	row.RequestDurationMs = NaNDistribution()

	return row
}
