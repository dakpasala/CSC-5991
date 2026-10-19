// Package core is the Go port of src/core/ -- just the pieces relevant to
// a live deployment (packet parsing, flow tracking, retransmission/RTT
// detection, window-stat computation). requests_log.py and
// find_controlled_sessions.py are NOT ported: there's no requests_history.json
// or exp.txt on a live capture, so there's nothing for them to do here.
//
// Mirrors src/core/packet_parser.py field-for-field. Payload size is
// computed from header-declared lengths (IP total length, IP header length,
// TCP/UDP header length), not from how many bytes actually survived capture
// truncation -- same reasoning as the Python version.
package core

import (
	"github.com/google/gopacket"
	"github.com/google/gopacket/layers"
)

const (
	ethernetHeaderLen = 14
	udpHeaderLen       = 8
)

// TCP flag bits, matching dpkt's TH_* constants used in packet_parser.py.
const (
	FlagFIN = 0x01
	FlagSYN = 0x02
	FlagRST = 0x04
	FlagPSH = 0x08
	FlagACK = 0x10
	FlagURG = 0x20
)

// Packet mirrors packet_parser.py's Packet dataclass exactly.
type Packet struct {
	Timestamp   float64
	Protocol    string // "tcp" or "udp"
	SrcIP       string
	DstIP       string
	SrcPort     int
	DstPort     int
	WireLen     int
	PayloadSize int
	Seq         uint32
	Ack         uint32
	Flags       uint8
	Window      uint16
}

func (p *Packet) Flag(bit uint8) bool {
	return p.Flags&bit != 0
}

// ParsePacket returns a *Packet, or nil if the frame isn't Ethernet/IPv4/TCP-or-UDP.
// wireLen is the TRUE original on-wire length (gopacket's ci.Length), not the
// possibly snaplen-truncated captured length (ci.CaptureLength) -- same
// true-length-preservation requirement as pcap_reader.py's docstring explains.
func ParsePacket(data []byte, timestamp float64, wireLen int) *Packet {
	pkt := gopacket.NewPacket(data, layers.LayerTypeEthernet, gopacket.NoCopy)

	ipLayer := pkt.Layer(layers.LayerTypeIPv4)
	if ipLayer == nil {
		return nil
	}
	ip, ok := ipLayer.(*layers.IPv4)
	if !ok {
		return nil
	}
	ipHeaderLen := int(ip.IHL) * 4

	if tcpLayer := pkt.Layer(layers.LayerTypeTCP); tcpLayer != nil {
		tcp, ok := tcpLayer.(*layers.TCP)
		if !ok {
			return nil
		}
		tcpHeaderLen := int(tcp.DataOffset) * 4
		payloadSize := wireLen - ethernetHeaderLen - ipHeaderLen - tcpHeaderLen
		if payloadSize < 0 {
			payloadSize = 0
		}
		return &Packet{
			Timestamp:   timestamp,
			Protocol:    "tcp",
			SrcIP:       ip.SrcIP.String(),
			DstIP:       ip.DstIP.String(),
			SrcPort:     int(tcp.SrcPort),
			DstPort:     int(tcp.DstPort),
			WireLen:     wireLen,
			PayloadSize: payloadSize,
			Seq:         tcp.Seq,
			Ack:         tcp.Ack,
			Flags:       tcpFlagByte(tcp),
			Window:      tcp.Window,
		}
	}

	if udpLayer := pkt.Layer(layers.LayerTypeUDP); udpLayer != nil {
		udp, ok := udpLayer.(*layers.UDP)
		if !ok {
			return nil
		}
		payloadSize := wireLen - ethernetHeaderLen - ipHeaderLen - udpHeaderLen
		if payloadSize < 0 {
			payloadSize = 0
		}
		return &Packet{
			Timestamp:   timestamp,
			Protocol:    "udp",
			SrcIP:       ip.SrcIP.String(),
			DstIP:       ip.DstIP.String(),
			SrcPort:     int(udp.SrcPort),
			DstPort:     int(udp.DstPort),
			WireLen:     wireLen,
			PayloadSize: payloadSize,
		}
	}

	return nil
}

func tcpFlagByte(tcp *layers.TCP) uint8 {
	var b uint8
	if tcp.FIN {
		b |= FlagFIN
	}
	if tcp.SYN {
		b |= FlagSYN
	}
	if tcp.RST {
		b |= FlagRST
	}
	if tcp.PSH {
		b |= FlagPSH
	}
	if tcp.ACK {
		b |= FlagACK
	}
	if tcp.URG {
		b |= FlagURG
	}
	return b
}

