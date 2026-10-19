// Mirrors src/core/flow_metrics.py exactly: per-flow retransmission,
// out-of-order, and RTT detection over a flow's FULL chronological packet
// history (not just one window's worth -- a packet in a later window can
// be a retransmission of something sent earlier).
//
// UDP carries no TCP sequence/ack numbers, so this produces no signal for
// it -- same as a real no-DPI observer would see for QUIC's internals.
package core

const seqModulus = uint64(1) << 32

func seqDelta(later, earlier uint32) uint32 {
	return uint32((uint64(later) - uint64(earlier) + seqModulus) % seqModulus)
}

type directionState struct {
	seenSeqs       map[uint32]struct{}
	baseSeq        *uint32
	maxRelativeSeq *uint32
}

func newDirectionState() *directionState {
	return &directionState{seenSeqs: make(map[uint32]struct{})}
}

// FlowPacket pairs a parsed packet with its direction ("up"/"down"),
// mirroring the Python (packet, direction) tuples compute_flow_metrics takes.
type FlowPacket struct {
	Packet    *Packet
	Direction string
}

// EventFlags mirrors one (is_retransmission, is_out_of_order) tuple.
type EventFlags struct {
	IsRetransmission bool
	IsOutOfOrder     bool
}

// RTTSample mirrors one (ack_timestamp, rtt_ms) tuple.
type RTTSample struct {
	Timestamp float64
	RTTMs     float64
}

// ComputeFlowMetrics mirrors compute_flow_metrics exactly: flowPackets must
// be one flow's complete, chronologically-sorted history. Returns
// per-packet event flags (same order/length as flowPackets) and RTT samples.
func ComputeFlowMetrics(flowPackets []FlowPacket) ([]EventFlags, []RTTSample) {
	eventFlags := make([]EventFlags, len(flowPackets))
	if len(flowPackets) == 0 || flowPackets[0].Packet.Protocol != "tcp" {
		return eventFlags, nil
	}

	directionState := map[string]*directionState{"up": newDirectionState(), "down": newDirectionState()}
	outstanding := map[string]map[uint32]float64{"up": {}, "down": {}}
	var rttSamples []RTTSample

	for i, fp := range flowPackets {
		packet := fp.Packet
		direction := fp.Direction
		state := directionState[direction]
		seq := packet.Seq

		var isRetransmission, isOutOfOrder bool

		// A pure ACK (no payload, not a SYN) doesn't consume sequence
		// space, so its sender legitimately resends the exact same seq on
		// every such packet -- that's normal, not a retransmission. Only
		// packets that actually consume sequence space are meaningful here.
		consumed := uint32(packet.PayloadSize)
		if packet.Flag(FlagSYN) {
			consumed++
		}

		if consumed != 0 {
			if _, seen := state.seenSeqs[seq]; seen {
				isRetransmission = true
			} else {
				state.seenSeqs[seq] = struct{}{}
				if state.baseSeq == nil {
					base := seq
					state.baseSeq = &base
				}
				relative := seqDelta(seq, *state.baseSeq)
				if state.maxRelativeSeq != nil && relative < *state.maxRelativeSeq {
					isOutOfOrder = true
				} else {
					state.maxRelativeSeq = &relative
				}
			}

			expectedAck := seqDelta(seq+consumed, 0)
			outstanding[direction][expectedAck] = packet.Timestamp
		}

		eventFlags[i] = EventFlags{IsRetransmission: isRetransmission, IsOutOfOrder: isOutOfOrder}

		if packet.Flag(FlagACK) {
			other := "up"
			if direction == "up" {
				other = "down"
			}
			expectedAck := seqDelta(packet.Ack, 0)
			if sentTs, ok := outstanding[other][expectedAck]; ok {
				delete(outstanding[other], expectedAck)
				rttSamples = append(rttSamples, RTTSample{
					Timestamp: packet.Timestamp,
					RTTMs:     (packet.Timestamp - sentTs) * 1000,
				})
			}
		}
	}

	return eventFlags, rttSamples
}
