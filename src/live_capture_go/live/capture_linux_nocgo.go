//go:build linux && !cgo

package main

import (
	"github.com/google/gopacket"
	"github.com/google/gopacket/pcapgo"
)

// captureHandle is the part of a packet source captureLoop needs.
type captureHandle interface {
	ReadPacketData() ([]byte, gopacket.CaptureInfo, error)
	Close()
}

// openCapture opens the interface with a Linux AF_PACKET raw socket
// (pcapgo.EthernetHandle), so a CGO_ENABLED=0 build needs no libpcap and no
// C toolchain. Promiscuous mode and the snaplen match the libpcap path: the
// capture buffer is snaplen bytes, so payload beyond the first snaplen bytes
// is never read, and CaptureInfo.Length still carries the true original
// length (from PACKET_AUXDATA), which is what ParsePacket uses.
func openCapture(iface string, snaplen int) (captureHandle, error) {
	h, err := pcapgo.NewEthernetHandle(iface)
	if err != nil {
		return nil, err
	}
	if err := h.SetPromiscuous(true); err != nil {
		h.Close()
		return nil, err
	}
	if err := h.SetCaptureLength(snaplen); err != nil {
		h.Close()
		return nil, err
	}
	return h, nil
}
