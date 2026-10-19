package main

import (
	"encoding/csv"
	"fmt"
	"os"
	"time"

	core "csc5991/core_go"
)

const labelPlaceholder = "live_unlabeled"

// replayWriter wraps a core.WindowAccumulator with the replay tool's own
// concerns: writing rows to CSV as they close, and reporting whether each
// window's computation stayed within its real-time budget.
type replayWriter struct {
	acc              *core.WindowAccumulator
	windowSeconds    float64
	speed            float64
	csvWriter        *csv.Writer
	csvFile          *os.File
	totalRowsWritten int
}

func newReplayWriter(t0, windowSeconds float64, sessionID, outputPath string, speed float64) *replayWriter {
	fileExists := false
	if _, err := os.Stat(outputPath); err == nil {
		fileExists = true
	}
	f, err := os.OpenFile(outputPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error opening output file:", err)
		os.Exit(1)
	}
	w := csv.NewWriter(f)
	if !fileExists {
		if err := w.Write(core.CSVHeader()); err != nil {
			fmt.Fprintln(os.Stderr, "error writing CSV header:", err)
			os.Exit(1)
		}
	}

	return &replayWriter{
		acc:           core.NewWindowAccumulator(t0, windowSeconds, sessionID, labelPlaceholder),
		windowSeconds: windowSeconds,
		speed:         speed,
		csvWriter:     w,
		csvFile:       f,
	}
}

func (rw *replayWriter) close() {
	rw.csvWriter.Flush()
	rw.csvFile.Close()
}

func (rw *replayWriter) handlePacket(p *core.Packet) {
	packetCountBeforeClose := rw.acc.PacketCount()
	start := time.Now()
	closedRows := rw.acc.HandlePacket(p)
	elapsed := time.Since(start)
	for _, row := range closedRows {
		rw.writeRow(row, packetCountBeforeClose, elapsed)
	}
}

func (rw *replayWriter) flush() {
	packetCount := rw.acc.PacketCount()
	start := time.Now()
	row := rw.acc.Flush()
	elapsed := time.Since(start)
	rw.writeRow(row, packetCount, elapsed)
}

func (rw *replayWriter) writeRow(row core.WindowRow, packetCount int, computeTime time.Duration) {
	if err := rw.csvWriter.Write(row.Values()); err != nil {
		fmt.Fprintln(os.Stderr, "error writing CSV row:", err)
		os.Exit(1)
	}
	rw.csvWriter.Flush()
	rw.totalRowsWritten++

	budget := rw.windowSeconds / rw.speed
	status := "OK"
	if computeTime.Seconds() >= budget {
		status = "FELL BEHIND"
	}
	fmt.Printf("window %d: %d packets, computed in %.3fs (budget %.1fs) [%s]\n",
		row.WindowIndex, packetCount, computeTime.Seconds(), budget, status)
}
