// Go port of benchmark_feature_memory.py -- same methodology, same stat
// categories, same CSV schema (stat,avg_bytes,p95_bytes,max_bytes), but
// measuring Go's actual memory shapes instead of Python's. Written to a
// SEPARATE file (benchmarks/feature_extraction_memory_benchmark_go.csv,
// at the repo root -- NOT src/benchmarks/, even though this binary runs
// from src/; see the outPath comment near the bottom), not overwriting
// the Python one, so the two can be compared side by side -- that
// comparison is the whole point of building this.
//
// Why bother with a second implementation at all: CPython caches small
// integers (-5..256) as process-wide singletons, so a Python list full of
// repeated small values (e.g. up_payload_size_buffer, mostly bare-ACK 0s)
// gets measured as much cheaper than it really costs on a platform without
// that caching -- which is exactly the situation on the real deployment
// target (a Go router binary). This file exists to show what those same
// buffers actually cost in Go's own memory model, where every slice slot
// is charged in full regardless of its value.
//
// core_go's ComputeFlowMetrics doesn't expose its internal per-direction
// bookkeeping state (seenSeqs set, outstanding-ack map) -- it only returns
// the final retransmission/out-of-order flags and RTT samples. flowStateSize
// below duplicates that bookkeeping loop locally, purely to get at the
// state for sizing, same reason the Python benchmark duplicates
// flow_metrics.py's loop in _flow_state_sizes rather than modifying the
// real implementation just to expose internals it doesn't otherwise need.
//
// Go has no general "deep size of this value and everything it points to"
// API the way Python's sys.getsizeof (extended here into deep_sizeof) is.
// The sizeBytes* helpers below are hand-rolled approximations of Go's own
// known memory layout (slice header + backing array, map bucket overhead)
// -- approximate, not byte-exact, the same honest caveat the Python
// benchmark's deep_sizeof already carries.
//
// Usage (run from src/, where go.mod lives):
//
//	go run ./benchmarking -source-dir /mnt/NAS/salmo-trutta/traces -num-sessions 100 -window-seconds 10
package main

import (
	"encoding/csv"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unsafe"

	core "csc5991/core_go"

	"github.com/google/gopacket/pcapgo"
)

// ---- pcap loading (same contract as live_capture_go/replay's loadPackets) ----

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

// ---- exp.txt label / controlled-experiment detection (mirrors
// build_session_csv.parse_exp_label / find_controlled_sessions.is_controlled_experiment) ----

var serviceNames = []string{"youtube", "netflix", "twitch", "amazon"}

// isControlledExperiment mirrors is_controlled_experiment: nil means "no
// known service name found" (Python's None), matching that three-way
// return exactly rather than collapsing it to a plain bool.
func isControlledExperiment(expContent string) *bool {
	content := strings.ToLower(strings.TrimSpace(expContent))
	for _, service := range serviceNames {
		idx := strings.Index(content, service)
		if idx == -1 {
			continue
		}
		end := idx + len(service) + 4
		if end > len(content) {
			end = len(content)
		}
		result := content[idx+len(service):end] != ".run"
		return &result
	}
	return nil
}

func parseExpLabel(expContent string) (string, error) {
	content := strings.ToLower(strings.TrimSpace(expContent))
	for _, service := range serviceNames {
		if strings.Contains(content, service) {
			return service, nil
		}
	}
	return "", fmt.Errorf("no known service name found in exp.txt content: %q", content)
}

// ---- session directory discovery (mirrors build_session_csv.discover_session_directories) ----

func discoverSessionDirectories(root string, batchSize int) ([]string, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		names = append(names, e.Name())
	}
	sort.Strings(names)
	if batchSize > 0 && batchSize < len(names) {
		names = names[:batchSize]
	}

	var found []string
	for _, name := range names {
		path := filepath.Join(root, name)
		info, err := os.Stat(path)
		if err != nil || !info.IsDir() {
			continue
		}
		if fileExists(filepath.Join(path, "dump.pcap")) && fileExists(filepath.Join(path, "exp.txt")) {
			found = append(found, path)
		}
	}
	return found, nil
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

// ---- requests_history.json loading (mirrors core/requests_log.py) ----

var serviceDomainKeywords = map[string][]string{
	"twitch":  {"twitch", "ttvnw"},
	"youtube": {"youtube", "googlevideo", "ytimg"},
	"netflix": {"netflix", "nflxvideo", "nflximg"},
	"amazon":  {"amazon", "primevideo", "media-amazon"},
}

type requestRecord struct {
	StartS        float64
	EndS          *float64
	ContentLength *int
}

func (r requestRecord) DurationMs() *float64 {
	if r.EndS == nil {
		return nil
	}
	d := (*r.EndS - r.StartS) * 1000
	return &d
}

type rawHeader struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type rawBefore struct {
	URL       string  `json:"url"`
	TimeStamp float64 `json:"timeStamp"`
}

type rawCompleted struct {
	TimeStamp       float64     `json:"timeStamp"`
	ResponseHeaders []rawHeader `json:"responseHeaders"`
}

type rawEntry struct {
	OnBeforeRequestOptions *rawBefore    `json:"OnBeforeRequestOptions"`
	OnCompleted            *rawCompleted `json:"onCompleted"`
}

func contentLengthFromHeaders(headers []rawHeader) *int {
	for _, h := range headers {
		if strings.EqualFold(h.Name, "content-length") {
			if v, err := strconv.Atoi(h.Value); err == nil {
				return &v
			}
			return nil
		}
	}
	return nil
}

// loadRelevantRequests mirrors load_relevant_requests, including treating
// a corrupt/empty requests_history.json the same as the file not existing
// (an optional, auxiliary input -- shouldn't fail the whole session).
func loadRelevantRequests(path, label string) []requestRecord {
	keywords := serviceDomainKeywords[strings.Split(label, "_")[0]]

	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var entries map[string]rawEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil
	}

	var records []requestRecord
	for _, entry := range entries {
		if entry.OnBeforeRequestOptions == nil {
			continue
		}
		host := ""
		if u, err := url.Parse(entry.OnBeforeRequestOptions.URL); err == nil {
			host = u.Host
		}
		matched := false
		for _, kw := range keywords {
			if strings.Contains(host, kw) {
				matched = true
				break
			}
		}
		if !matched {
			continue
		}

		rec := requestRecord{StartS: entry.OnBeforeRequestOptions.TimeStamp / 1000}
		if entry.OnCompleted != nil {
			endS := entry.OnCompleted.TimeStamp / 1000
			rec.EndS = &endS
			rec.ContentLength = contentLengthFromHeaders(entry.OnCompleted.ResponseHeaders)
		}
		records = append(records, rec)
	}
	return records
}

// ---- Go-side memory-size approximations (see file header for why these
// are hand-rolled instead of a general "deep sizeof") ----

const (
	sliceHeaderBytes    = 24 // ptr + len + cap, 8 bytes each on a 64-bit build
	mapOverheadPerEntry = 48 // rough, commonly-cited figure for small-key Go maps -- approximate, not exact
	ptrBytes            = 8
	minAllocBytes       = 8 // Go's smallest allocator size class
)

func sliceBytesInt(n int) int     { return sliceHeaderBytes + n*8 }
func sliceBytesFloat64(n int) int { return sliceHeaderBytes + n*8 }

func mapBytesSeenSeqs(n int) int    { return n * (4 + mapOverheadPerEntry) }     // uint32 key, struct{} value (0 bytes)
func mapBytesOutstanding(n int) int { return n * (4 + 8 + mapOverheadPerEntry) } // uint32 key, float64 value

func ptrFieldBytes(nonNil bool) int {
	if nonNil {
		return ptrBytes + minAllocBytes
	}
	return ptrBytes
}

// ---- retransmission/RTT state sizing (mirrors flow_metrics.ComputeFlowMetrics's
// bookkeeping loop, duplicated locally purely to expose the state for sizing --
// see file header) ----

type benchDirectionState struct {
	seenSeqs       map[uint32]struct{}
	baseSeq        *uint32
	maxRelativeSeq *uint32
}

func newBenchDirectionState() *benchDirectionState {
	return &benchDirectionState{seenSeqs: make(map[uint32]struct{})}
}

func seqDeltaLocal(later, earlier uint32) uint32 {
	const seqModulus = uint64(1) << 32
	return uint32((uint64(later) - uint64(earlier) + seqModulus) % seqModulus)
}

func flowStateSize(flowPackets []core.FlowPacket) int {
	if len(flowPackets) == 0 || flowPackets[0].Packet.Protocol != "tcp" {
		return 0
	}

	directionState := map[string]*benchDirectionState{"up": newBenchDirectionState(), "down": newBenchDirectionState()}
	outstanding := map[string]map[uint32]float64{"up": {}, "down": {}}

	for _, fp := range flowPackets {
		packet := fp.Packet
		direction := fp.Direction
		state := directionState[direction]
		seq := packet.Seq

		consumed := uint32(packet.PayloadSize)
		if packet.Flag(core.FlagSYN) {
			consumed++
		}
		if consumed != 0 {
			if _, seen := state.seenSeqs[seq]; !seen {
				state.seenSeqs[seq] = struct{}{}
				if state.baseSeq == nil {
					base := seq
					state.baseSeq = &base
				}
				relative := seqDeltaLocal(seq, *state.baseSeq)
				if state.maxRelativeSeq == nil || relative >= *state.maxRelativeSeq {
					state.maxRelativeSeq = &relative
				}
			}
			expectedAck := seqDeltaLocal(seq+consumed, 0)
			outstanding[direction][expectedAck] = packet.Timestamp
		}

		if packet.Flag(core.FlagACK) {
			other := "down"
			if direction == "down" {
				other = "up"
			}
			delete(outstanding[other], seqDeltaLocal(packet.Ack, 0))
		}
	}

	size := 0
	for _, dir := range []string{"up", "down"} {
		ds := directionState[dir]
		size += mapBytesSeenSeqs(len(ds.seenSeqs))
		size += ptrFieldBytes(ds.baseSeq != nil)
		size += ptrFieldBytes(ds.maxRelativeSeq != nil)
	}
	for _, dir := range []string{"up", "down"} {
		size += mapBytesOutstanding(len(outstanding[dir]))
	}
	return size
}

// ---- per-session benchmark (mirrors benchmark_feature_memory.benchmark_session) ----

type windowSizesMap map[string][]int

type sessionResult struct {
	flowIdentificationSizes []int
	retransmissionRTTSizes  []int
	flowIdentificationTotal int
	retransmissionRTTTotal  int
	windowSizes             windowSizesMap
}

func sumInts(xs []int) int {
	t := 0
	for _, x := range xs {
		t += x
	}
	return t
}

type taggedPacket struct {
	direction   string
	wireLen     int
	payloadSize int
	recvWindow  *int
	timestamp   float64
}

// benchmarkSession returns (result, skippedAsControlled, error).
func benchmarkSession(sessionDir string, windowSeconds float64) (*sessionResult, bool, error) {
	expBytes, err := os.ReadFile(filepath.Join(sessionDir, "exp.txt"))
	if err != nil {
		return nil, false, err
	}
	expContent := string(expBytes)

	if controlled := isControlledExperiment(expContent); controlled != nil && *controlled {
		return nil, true, nil
	}
	label, err := parseExpLabel(expContent)
	if err != nil {
		return nil, false, err
	}

	packets, t0, err := loadPackets(filepath.Join(sessionDir, "dump.pcap"))
	if err != nil {
		return nil, false, err
	}

	tracker := core.NewFlowTracker()
	windowIndexOf := func(ts float64) int { return int((ts - t0) / windowSeconds) }

	packetsByWindow := map[int][]taggedPacket{}
	flowPacketLists := map[core.FlowKey][]core.FlowPacket{}
	maxWindowIdx := 0

	for _, p := range packets {
		key, direction, _ := tracker.Process(p)
		var rw *int
		if p.Protocol == "tcp" {
			w := int(p.Window)
			rw = &w
		}
		idx := windowIndexOf(p.Timestamp)
		if idx > maxWindowIdx {
			maxWindowIdx = idx
		}
		packetsByWindow[idx] = append(packetsByWindow[idx], taggedPacket{direction, p.WireLen, p.PayloadSize, rw, p.Timestamp})
		flowPacketLists[key] = append(flowPacketLists[key], core.FlowPacket{Packet: p, Direction: direction})
	}

	// Per-flow state, one measurement per flow -- same two categories as
	// the Python benchmark: flow identification (fixed-size) and
	// retransmission/RTT tracking (grows with the flow's packet count).
	var flowIdentificationSizes []int
	for _, state := range tracker.Flows() {
		flowIdentificationSizes = append(
			flowIdentificationSizes,
			int(unsafe.Sizeof(*state))+len(state.ClientIP)+len(state.ServerIP),
		)
	}

	var retransmissionRTTSizes []int
	var rttSamples []core.RTTSample
	for _, fps := range flowPacketLists {
		retransmissionRTTSizes = append(retransmissionRTTSizes, flowStateSize(fps))
		_, rtts := core.ComputeFlowMetrics(fps)
		rttSamples = append(rttSamples, rtts...)
	}

	rttByWindow := map[int][]float64{}
	for _, s := range rttSamples {
		idx := windowIndexOf(s.Timestamp)
		rttByWindow[idx] = append(rttByWindow[idx], s.RTTMs)
	}

	var requests []requestRecord
	requestsPath := filepath.Join(sessionDir, "requests_history.json")
	if fileExists(requestsPath) {
		requests = loadRelevantRequests(requestsPath, label)
	}

	windowSizes := windowSizesMap{}
	numWindows := maxWindowIdx + 1

	for windowIndex := 0; windowIndex < numWindows; windowIndex++ {
		windowPackets := packetsByWindow[windowIndex]
		windowTotalBytes := 0

		var up, down []taggedPacket
		for _, tp := range windowPackets {
			if tp.direction == "up" {
				up = append(up, tp)
			} else {
				down = append(down, tp)
			}
		}

		groups := []struct {
			prefix string
			pkts   []taggedPacket
		}{{"up", up}, {"down", down}, {"total", windowPackets}}

		for _, g := range groups {
			sorted := append([]taggedPacket{}, g.pkts...)
			sort.Slice(sorted, func(i, j int) bool { return sorted[i].timestamp < sorted[j].timestamp })

			payloadCount, packetCount, recvWindowCount := 0, 0, 0
			for _, tp := range sorted {
				payloadCount++
				packetCount++
				if tp.recvWindow != nil {
					recvWindowCount++
				}
			}
			interarrivalCount := 0
			if len(sorted) > 1 {
				interarrivalCount = len(sorted) - 1
			}

			metrics := []struct {
				name string
				size int
			}{
				{"payload_size", sliceBytesInt(payloadCount)},
				{"packet_size", sliceBytesInt(packetCount)},
				{"recv_window", sliceBytesInt(recvWindowCount)},
				{"interarrival_time", sliceBytesFloat64(interarrivalCount)},
			}
			for _, m := range metrics {
				key := fmt.Sprintf("%s_%s_buffer", g.prefix, m.name)
				windowSizes[key] = append(windowSizes[key], m.size)
				if g.prefix == "total" {
					windowTotalBytes += m.size
				}
			}
		}

		rttSize := sliceBytesFloat64(len(rttByWindow[windowIndex]))
		windowSizes["rtt_ms_buffer"] = append(windowSizes["rtt_ms_buffer"], rttSize)
		windowTotalBytes += rttSize

		windowStart := t0 + float64(windowIndex)*windowSeconds
		windowEnd := windowStart + windowSeconds
		sizesCount, durationsCount := 0, 0
		for _, r := range requests {
			if r.StartS >= windowStart && r.StartS < windowEnd {
				if r.ContentLength != nil {
					sizesCount++
				}
				if r.DurationMs() != nil {
					durationsCount++
				}
			}
		}
		segSize := sliceBytesInt(sizesCount)
		durSize := sliceBytesFloat64(durationsCount)
		windowSizes["segment_size_bytes_buffer"] = append(windowSizes["segment_size_bytes_buffer"], segSize)
		windowSizes["request_duration_ms_buffer"] = append(windowSizes["request_duration_ms_buffer"], durSize)
		windowTotalBytes += segSize + durSize

		windowSizes["total_window_buffer (simultaneous)"] = append(windowSizes["total_window_buffer (simultaneous)"], windowTotalBytes)
	}

	return &sessionResult{
		flowIdentificationSizes: flowIdentificationSizes,
		retransmissionRTTSizes:  retransmissionRTTSizes,
		flowIdentificationTotal: sumInts(flowIdentificationSizes),
		retransmissionRTTTotal:  sumInts(retransmissionRTTSizes),
		windowSizes:             windowSizes,
	}, false, nil
}

// ---- aggregation / output ----

// percentile95 replicates numpy's default linear-interpolation percentile
// on an unsorted slice (sorts its own copy) -- same algorithm as
// core_go/distribution.go's numpyPercentile, which is unexported and so
// not reachable from this package; duplicated here rather than exported
// from core just for this one benchmarking tool.
func percentile95(values []float64) float64 {
	sorted := append([]float64{}, values...)
	sort.Float64s(sorted)
	n := len(sorted)
	if n == 1 {
		return sorted[0]
	}
	h := 0.95 * float64(n-1)
	lo := int(math.Floor(h))
	hi := int(math.Ceil(h))
	if lo == hi {
		return sorted[lo]
	}
	frac := h - float64(lo)
	return sorted[lo] + frac*(sorted[hi]-sorted[lo])
}

type statsRow struct {
	stat           string
	avg, p95, maxV float64
}

func computeStatsRow(name string, valuesInt []int) statsRow {
	values := make([]float64, len(valuesInt))
	sum := 0.0
	maxV := math.Inf(-1)
	for i, v := range valuesInt {
		fv := float64(v)
		values[i] = fv
		sum += fv
		if fv > maxV {
			maxV = fv
		}
	}
	return statsRow{stat: name, avg: sum / float64(len(values)), p95: percentile95(values), maxV: maxV}
}

func main() {
	sourceDir := flag.String("source-dir", "/mnt/NAS/salmo-trutta/traces", "directory containing session subdirectories")
	numSessions := flag.Int("num-sessions", 100, "number of real (non-controlled) sessions to benchmark")
	windowSeconds := flag.Float64("window-seconds", 10, "window size in seconds")
	flag.Parse()

	candidates, err := discoverSessionDirectories(*sourceDir, *numSessions*2)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error listing source-dir:", err)
		os.Exit(1)
	}

	var allFlowIdentification, allRetransmissionRTT, allFlowIdentificationTotal, allRetransmissionRTTTotal []int
	allWindowSizes := windowSizesMap{}
	benchmarked, skippedControlled, failed := 0, 0, 0

	for _, dir := range candidates {
		if benchmarked >= *numSessions {
			break
		}
		result, skip, err := benchmarkSession(dir, *windowSeconds)
		if err != nil {
			failed++
			fmt.Fprintf(os.Stderr, "FAILED: %s: %v\n", dir, err)
			continue
		}
		if skip {
			skippedControlled++
			continue
		}

		benchmarked++
		allFlowIdentification = append(allFlowIdentification, result.flowIdentificationSizes...)
		allRetransmissionRTT = append(allRetransmissionRTT, result.retransmissionRTTSizes...)
		allFlowIdentificationTotal = append(allFlowIdentificationTotal, result.flowIdentificationTotal)
		allRetransmissionRTTTotal = append(allRetransmissionRTTTotal, result.retransmissionRTTTotal)
		for stat, vals := range result.windowSizes {
			allWindowSizes[stat] = append(allWindowSizes[stat], vals...)
		}
		fmt.Printf("[%d/%d] %s: %d flows\n", benchmarked, *numSessions, dir, len(result.flowIdentificationSizes))
	}

	fmt.Printf("\nbenchmarked %d sessions, skipped %d controlled, %d failed\n", benchmarked, skippedControlled, failed)

	var rows []statsRow
	rows = append(rows, computeStatsRow("flow_identification_state (per flow)", allFlowIdentification))
	rows = append(rows, computeStatsRow("retransmission_rtt_state (per flow)", allRetransmissionRTT))
	rows = append(rows, computeStatsRow("flow_identification_state (total per session)", allFlowIdentificationTotal))
	rows = append(rows, computeStatsRow("retransmission_rtt_state (total per session)", allRetransmissionRTTTotal))
	for stat, vals := range allWindowSizes {
		label := stat
		if !strings.Contains(stat, "(simultaneous)") {
			label = stat + " (per window)"
		}
		rows = append(rows, computeStatsRow(label, vals))
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].avg > rows[j].avg })

	fmt.Println("\nmemory per stat (bytes) -- avg / p95 / max across sessions-or-windows:")
	for _, r := range rows {
		fmt.Printf("%-55s %12.1f %12.1f %12.1f\n", r.stat, r.avg, r.p95, r.maxV)
	}

	// Unlike live_capture_go's tools (which intentionally land wherever
	// the process's CWD happens to be, same as their Python originals),
	// this one's whole point is to sit next to the Python benchmark's
	// output for direct comparison -- so it goes up one level from
	// src/ (where this is run from, per go.mod) to the repo root's
	// benchmarks/, not a separate src/benchmarks/ copy.
	outPath := "../benchmarks/feature_extraction_memory_benchmark_go.csv"
	if err := os.MkdirAll(filepath.Dir(outPath), 0755); err != nil {
		fmt.Fprintln(os.Stderr, "error creating output dir:", err)
		os.Exit(1)
	}
	f, err := os.Create(outPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error creating output file:", err)
		os.Exit(1)
	}
	defer f.Close()

	w := csv.NewWriter(f)
	w.Write([]string{"stat", "avg_bytes", "p95_bytes", "max_bytes"})
	for _, r := range rows {
		w.Write([]string{
			r.stat,
			strconv.FormatFloat(r.avg, 'f', -1, 64),
			strconv.FormatFloat(r.p95, 'f', -1, 64),
			strconv.FormatFloat(r.maxV, 'f', -1, 64),
		})
	}
	w.Flush()
	fmt.Printf("\nsaved to %s\n", outPath)
}
