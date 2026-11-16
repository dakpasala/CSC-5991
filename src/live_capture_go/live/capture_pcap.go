//go:build cgo || !linux

package main

import (
	"github.com/google/gopacket"
	"github.com/google/gopacket/pcap"
)

// captureHandle is the part of a packet source captureLoop needs.
type captureHandle interface {
	ReadPacketData() ([]byte, gopacket.CaptureInfo, error)
	Close()
}

// openCapture opens the interface through libpcap (cgo), in promiscuous
// mode, truncating each packet to snaplen bytes.
func openCapture(iface string, snaplen int) (captureHandle, error) {
	return pcap.OpenLive(iface, int32(snaplen), true, pcap.BlockForever)
}
