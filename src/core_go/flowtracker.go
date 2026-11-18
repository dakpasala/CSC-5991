// Mirrors src/core/flow_tracker.py exactly by default: 5-tuple flow
// identification, client/server determination (authoritative via a pure
// SYN for TCP, first-packet-sender assumption otherwise). With
// DirectionByServicePort set, one rule is added; see there.
package core

// DirectionByServicePort, when true, assigns a flow whose first seen packet
// goes FROM a service port (<1024: 443, 80, ...) TO an ephemeral port the
// other way round -- that first packet is the server's, so the client is
// the other end. A pure SYN still overrides it.
//
// Off by default, for exact parity with flow_tracker.py and therefore with
// the training data: the dataset's client-side captures sometimes start
// mid-connection, so the first-packet rule mis-assigned some flows there
// too, and the models learned those features as computed (turning the rule
// on drops option C from 0.878 to 0.816 on one local session, from 1.0 to
// 0.984 on another). The live tool turns it on: a live capture starts before
// the traffic it measures, so the rule only fires on genuinely reversed
// flows -- and one early router run had its QUIC video flow reversed (46MB
// "uploaded", every up/down feature mirrored, accuracy 0.42). Fixing it in
// flow_tracker.py too, then regenerating the dataset and retraining, would
// make training and deployment agree on the corrected rule.
var DirectionByServicePort = false

// FlowKey mirrors the Python tuple (ip_a, port_a, ip_b, port_b, protocol).
type FlowKey struct {
	IPA      string
	PortA    int
	IPB      string
	PortB    int
	Protocol string
}

type FlowState struct {
	ClientIP   string
	ClientPort int
	ServerIP   string
	ServerPort int
}

type FlowTracker struct {
	flows map[FlowKey]*FlowState
}

func NewFlowTracker() *FlowTracker {
	return &FlowTracker{flows: make(map[FlowKey]*FlowState)}
}

// Flows exposes the tracker's internal map, read-only use expected --
// mirrors Python code elsewhere reaching into tracker._flows (e.g. the
// memory benchmark measuring FlowState's size directly).
func (t *FlowTracker) Flows() map[FlowKey]*FlowState {
	return t.flows
}

// endpointLess replicates Python's tuple comparison (ip_str, port_int) <=
// (ip_str, port_int): compare IP strings lexicographically first, then
// ports numerically if the IPs are equal.
func endpointLess(ipA string, portA int, ipB string, portB int) bool {
	if ipA != ipB {
		return ipA < ipB
	}
	return portA <= portB
}

// FlowKeyOf is the direction-independent flow key for a packet (both
// directions of a connection map to the same key).
func FlowKeyOf(p *Packet) FlowKey { return canonicalKey(p) }

func canonicalKey(p *Packet) FlowKey {
	var loIP, hiIP string
	var loPort, hiPort int
	if endpointLess(p.SrcIP, p.SrcPort, p.DstIP, p.DstPort) {
		loIP, loPort = p.SrcIP, p.SrcPort
		hiIP, hiPort = p.DstIP, p.DstPort
	} else {
		loIP, loPort = p.DstIP, p.DstPort
		hiIP, hiPort = p.SrcIP, p.SrcPort
	}
	return FlowKey{IPA: loIP, PortA: loPort, IPB: hiIP, PortB: hiPort, Protocol: p.Protocol}
}

// Process mirrors FlowTracker.process: call once per packet, in
// chronological order. Returns (key, direction, isNewFlow); direction is
// "up" if the packet travels client -> server, else "down".
func (t *FlowTracker) Process(p *Packet) (FlowKey, string, bool) {
	key := canonicalKey(p)
	isPureSYN := p.Protocol == "tcp" && p.Flag(FlagSYN) && !p.Flag(FlagACK)

	state, exists := t.flows[key]
	isNewFlow := false
	if !exists {
		state = &FlowState{
			ClientIP:   p.SrcIP,
			ClientPort: p.SrcPort,
			ServerIP:   p.DstIP,
			ServerPort: p.DstPort,
		}
		if DirectionByServicePort && !isPureSYN && isServicePort(p.SrcPort) && !isServicePort(p.DstPort) {
			state.ClientIP, state.ClientPort, state.ServerIP, state.ServerPort = p.DstIP, p.DstPort, p.SrcIP, p.SrcPort
		}
		t.flows[key] = state
		isNewFlow = true
	} else if isPureSYN {
		// Authoritative: a real SYN always wins over an earlier guess.
		state.ClientIP, state.ClientPort = p.SrcIP, p.SrcPort
		state.ServerIP, state.ServerPort = p.DstIP, p.DstPort
	}

	direction := "down"
	if p.SrcIP == state.ClientIP && p.SrcPort == state.ClientPort {
		direction = "up"
	}
	return key, direction, isNewFlow
}

// isServicePort reports a well-known/system port (below 1024: 443 for
// HTTPS/QUIC, 80, 53, ...), which servers listen on and client operating
// systems never pick as an ephemeral source port.
func isServicePort(port int) bool {
	return port > 0 && port < 1024
}
