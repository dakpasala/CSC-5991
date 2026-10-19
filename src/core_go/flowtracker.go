// Mirrors src/core/flow_tracker.py exactly: 5-tuple flow identification,
// client/server determination (authoritative via a pure SYN for TCP,
// first-packet-sender assumption for UDP).
package core

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
