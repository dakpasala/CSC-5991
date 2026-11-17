package main

import (
	"bufio"
	"encoding/csv"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	core "csc5991/core_go"
)

// perfRecorder writes one row per closed window (-perf-output) with what the
// live tool cost to run during it: CPU, time spent on packets / closing the
// window / scoring it, and memory. It's how the router deployment's
// resource use is measured, alongside the accuracy the eval script scores.
//
// Columns:
//
//	packets_in_window      packets that landed in the window
//	packet_handling_us     total time in WindowAccumulator.HandlePacket for
//	                       them (flow tracking + buffering), window close excluded
//	window_close_ms        time to close the window: retransmission/RTT over
//	                       each touched flow's history + all feature stats
//	inference_us           time the model spent scoring the window (Go-native
//	                       XGBoost only; blank for the Python sidecar)
//	cpu_percent            process CPU time / wall time since the last window
//	                       (100 = one core fully busy); blank for windows
//	                       closed together after a traffic gap
//	heap_alloc_mb          Go heap in use by live objects
//	heap_sys_mb            heap memory obtained from the OS
//	rss_mb                 resident set size right now (Linux /proc; blank elsewhere)
//	max_rss_mb             peak resident set size so far (getrusage)
//	flows_retained         flows the accumulator is tracking
//	packets_retained       per-flow entries kept across windows, never pruned:
//	                       stored packets (all-features accumulator) or seen
//	                       sequence numbers + unacked segments (selective;
//	                       0 when the model uses no RTT/retransmission feature)
type perfRecorder struct {
	f        *os.File
	w        *csv.Writer
	handling time.Duration
	lastWall time.Time
	lastCPU  time.Duration
}

var perfHeader = []string{
	"window_index", "packets_in_window", "packet_handling_us", "window_close_ms", "inference_us",
	"cpu_percent", "heap_alloc_mb", "heap_sys_mb", "rss_mb", "max_rss_mb", "flows_retained", "packets_retained",
}

func newPerfRecorder(path string) (*perfRecorder, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	p := &perfRecorder{f: f, w: csv.NewWriter(f), lastWall: time.Now(), lastCPU: processCPU()}
	p.w.Write(perfHeader)
	p.w.Flush()
	return p, p.w.Error()
}

// packetHandled adds the time one non-window-closing HandlePacket call took.
func (p *perfRecorder) packetHandled(d time.Duration) {
	p.handling += d
}

func (p *perfRecorder) windowClosed(row core.WindowRow, closeDur, inferDur time.Duration, haveInfer bool, acc core.Accumulator) {
	// CPU% needs a real interval: when a traffic gap closes several windows
	// in one call they close microseconds apart, and dividing by that would
	// report a meaningless spike (a 109% "peak" in an early router run). Leave
	// those blank and let the next window's interval cover the whole span.
	now, cpu := time.Now(), processCPU()
	cpuPct := ""
	if wall := now.Sub(p.lastWall); wall >= time.Second {
		cpuPct = strconv.FormatFloat(100*float64(cpu-p.lastCPU)/float64(wall), 'f', 2, 64)
		p.lastWall, p.lastCPU = now, cpu
	}

	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	flows, stored := 0, 0
	if acc != nil {
		flows, stored = acc.RetainedState()
	}
	infer := ""
	if haveInfer {
		infer = strconv.FormatFloat(float64(inferDur.Nanoseconds())/1e3, 'f', 1, 64)
	}
	rss := ""
	if v, ok := currentRSSBytes(); ok {
		rss = mb(v)
	}
	p.w.Write([]string{
		strconv.Itoa(row.WindowIndex),
		strconv.Itoa(row.Total.PacketCount),
		strconv.FormatFloat(float64(p.handling.Nanoseconds())/1e3, 'f', 1, 64),
		strconv.FormatFloat(float64(closeDur.Nanoseconds())/1e6, 'f', 3, 64),
		infer,
		cpuPct,
		mb(ms.HeapAlloc), mb(ms.HeapSys), rss, mb(peakRSSBytes()),
		strconv.Itoa(flows), strconv.Itoa(stored),
	})
	p.w.Flush()
	p.handling = 0
}

func (p *perfRecorder) Close() error {
	p.w.Flush()
	return p.f.Close()
}

func mb(bytes uint64) string {
	return strconv.FormatFloat(float64(bytes)/(1<<20), 'f', 2, 64)
}

// processCPU is user + system CPU time used by this process so far.
func processCPU() time.Duration {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0
	}
	return time.Duration(ru.Utime.Nano() + ru.Stime.Nano())
}

// peakRSSBytes is getrusage's ru_maxrss, which Linux reports in KB and macOS
// in bytes.
func peakRSSBytes() uint64 {
	var ru syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &ru); err != nil {
		return 0
	}
	if runtime.GOOS == "darwin" {
		return uint64(ru.Maxrss)
	}
	return uint64(ru.Maxrss) * 1024
}

// currentRSSBytes reads VmRSS from /proc/self/status (Linux only).
func currentRSSBytes() (uint64, bool) {
	f, err := os.Open("/proc/self/status")
	if err != nil {
		return 0, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, "VmRSS:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				kb, err := strconv.ParseUint(fields[1], 10, 64)
				return kb * 1024, err == nil
			}
		}
	}
	return 0, false
}

// memorySummary is a one-line snapshot for the terminal, e.g. right after the
// model loads, before any traffic has been seen.
func memorySummary() string {
	var ms runtime.MemStats
	runtime.ReadMemStats(&ms)
	s := fmt.Sprintf("heap %s MB, heap from OS %s MB", mb(ms.HeapAlloc), mb(ms.HeapSys))
	if v, ok := currentRSSBytes(); ok {
		s += fmt.Sprintf(", RSS %s MB", mb(v))
	}
	return s + fmt.Sprintf(", peak RSS %s MB", mb(peakRSSBytes()))
}
