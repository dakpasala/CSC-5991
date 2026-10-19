// CSV serialization for WindowRow, matching build_session_csv.py's column
// names/order exactly so Python-side comparison tooling can align by name.
package core

import (
	"math"
	"strconv"
)

func formatFloat(v float64) string {
	if math.IsNaN(v) {
		return "" // pandas writes NaN as an empty cell
	}
	return strconv.FormatFloat(v, 'g', -1, 64)
}

func formatInt(v int) string {
	return strconv.Itoa(v)
}

func distributionValues(d Distribution) []string {
	return []string{
		formatFloat(d.Min), formatFloat(d.Max), formatFloat(d.Mean),
		formatFloat(d.Median), formatFloat(d.P25), formatFloat(d.P75),
	}
}

func distributionHeader(prefix string) []string {
	return []string{
		prefix + "_min", prefix + "_max", prefix + "_mean",
		prefix + "_median", prefix + "_p25", prefix + "_p75",
	}
}

func directionStatsHeader(prefix string) []string {
	h := []string{
		prefix + "_packet_count", prefix + "_byte_count", prefix + "_throughput_bps",
		prefix + "_retransmission_count", prefix + "_out_of_order_count",
	}
	for _, flag := range TCPFlags {
		h = append(h, prefix+"_"+flag.Name+"_count")
	}
	h = append(h, distributionHeader(prefix+"_payload_size")...)
	h = append(h, distributionHeader(prefix+"_packet_size")...)
	h = append(h, distributionHeader(prefix+"_recv_window")...)
	h = append(h, distributionHeader(prefix+"_interarrival_time")...)
	return h
}

func directionStatsValues(s DirectionStats, prefix string) []string {
	v := []string{
		formatInt(s.PacketCount), strconv.FormatInt(s.ByteCount, 10), formatFloat(s.ThroughputBps),
		formatInt(s.RetransmissionCount), formatInt(s.OutOfOrderCount),
	}
	for _, flag := range TCPFlags {
		v = append(v, formatInt(s.FlagCounts[flag.Name]))
	}
	v = append(v, distributionValues(s.PayloadSize)...)
	v = append(v, distributionValues(s.PacketSize)...)
	v = append(v, distributionValues(s.RecvWindow)...)
	v = append(v, distributionValues(s.InterarrivalTimeMs)...)
	return v
}

// CSVHeader returns the full column list, matching build_session_csv.py's
// output order (app-layer columns included for schema parity, always
// empty/zero since there's no requests_history.json on a live capture).
func CSVHeader() []string {
	h := []string{
		"label", "session_id", "window_index", "window_start_s", "window_end_s",
		"num_flows", "num_new_flows", "num_continuing_flows", "frac_udp",
	}
	h = append(h, directionStatsHeader("up")...)
	h = append(h, directionStatsHeader("down")...)
	h = append(h, directionStatsHeader("total")...)
	h = append(h, distributionHeader("rtt_ms")...)
	h = append(h, "request_count")
	h = append(h, distributionHeader("segment_size_bytes")...)
	h = append(h, distributionHeader("request_duration_ms")...)
	return h
}

// Values returns this row's values in the same order as CSVHeader().
func (row WindowRow) Values() []string {
	v := []string{
		row.Label, row.SessionID, formatInt(row.WindowIndex),
		formatFloat(row.WindowStartS), formatFloat(row.WindowEndS),
		formatInt(row.NumFlows), formatInt(row.NumNewFlows), formatInt(row.NumContinuing),
		formatFloat(row.FracUDP),
	}
	v = append(v, directionStatsValues(row.Up, "up")...)
	v = append(v, directionStatsValues(row.Down, "down")...)
	v = append(v, directionStatsValues(row.Total, "total")...)
	v = append(v, distributionValues(row.RTT)...)
	v = append(v, formatInt(row.RequestCount))
	v = append(v, distributionValues(row.SegmentSizeBytes)...)
	v = append(v, distributionValues(row.RequestDurationMs)...)
	return v
}
