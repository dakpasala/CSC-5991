package main

import (
	"encoding/csv"
	"fmt"
	"hash/fnv"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"

	core "csc5991/core_go"
)

// priorityManager decides which connections a router would prioritize,
// from the classifier's per-window labels. By default it is a dry run: it
// prints and logs its decisions but changes nothing on the router. With
// enforce (-priority-enforce), each decision is also applied: a tagged
// connection is added to the nftables set router/qos.sh creates (table inet
// csc5991, set video_flows), whose rule marks the connection's download
// packets DSCP AF41 -- cake's Video tin on lan2. Entries carry a 3-minute
// timeout and are refreshed every window while tagged, so marks lapse on
// their own if live-router stops; the set is flushed on a clean exit.
//
// The rule:
//   - Tag: in a window labeled with confidence >= minConf that carried >=
//     minPackets packets (real video data, not a quiet gap between YouTube's
//     download bursts), tag the connections carrying most of that window's
//     bytes -- the heaviest flows until they cover 80% of the bytes, at most 3,
//     each with at least 10% -- among only the connections that could be the
//     labeled service: server port 443 or 80 (every one of these services
//     streams over HTTPS/HTTP), and UDP for youtube_quic, TCP for the rest.
//     Without that filter, a 22 MB SSH transfer (git/rsync on the Linux box)
//     sharing a window with the video was tagged as "youtube_quic", having
//     outweighed the video in bytes.
//   - Keep the tag while the connection exists, through quiet windows too: a
//     quiet tagged connection uses no bandwidth, and YouTube usually sends its
//     next burst on the same connection, so the tag is already in place for it
//     instead of arriving one 10s window late.
//   - Drop the tag when the connection closes (TCP FIN/RST) or after `idle`
//     with no packets on it. 2 minutes by default: 32-49% of YouTube sessions
//     in the training data have a quiet stretch over 30s between bursts, so a
//     shorter timeout would drop priority right before the next burst.
type priorityManager struct {
	minPackets int
	minConf    float64
	idle       float64 // seconds of capture time
	t0         float64

	tagged map[core.FlowKey]*priorityTag
	log    *csv.Writer
	logF   *os.File
	ons    int
	offs   int

	enforce bool
	nft     func(script string) error // runs an nft script (nft -f -); swappable in tests

	// -priority-by flow: windows in a row a connection's own label must be
	// the same service at >= minConf before it is tagged (-priority-streak).
	streakNeed int
	streaks    map[core.FlowKey]*streak

	// -priority-max-mbps: a window in which the connection carried more than
	// this (bytes per window, i.e. Mbit/s x window seconds / 8) disqualifies
	// it, resetting its streak. Streaming players fetch at bounded rates
	// (Twitch 1080p ~9 Mbit/s); bulk downloads on a fast line run far above
	// that, and were labeled youtube_tcp at >= 0.93 -- on 2026-11-21, 97 % of
	// the bytes marked for the Video tier were downloads. 0 = no cap.
	maxWindowBytes int64
}

// streak counts a connection's confident windows toward streakNeed.
type streak struct {
	label      string
	n          int
	lastWindow int
}

type priorityTag struct {
	label    string
	since    float64
	lastSeen float64
}

func newPriorityManager(minPackets int, minConf float64, idle time.Duration, logPath string) (*priorityManager, error) {
	m := &priorityManager{
		minPackets: minPackets, minConf: minConf, idle: idle.Seconds(),
		tagged: map[core.FlowKey]*priorityTag{}, streakNeed: 1, streaks: map[core.FlowKey]*streak{},
	}
	if logPath != "" {
		f, err := os.Create(logPath)
		if err != nil {
			return nil, err
		}
		m.logF, m.log = f, csv.NewWriter(f)
		// No IP addresses (project rule for written outputs): flows are logged
		// by a short hash, protocol and server port.
		m.log.Write([]string{"time_s", "window_index", "event", "label", "confidence", "flow_id", "protocol", "server_port", "window_bytes", "reason"})
		m.log.Flush()
	}
	return m, nil
}

// observe runs for every packet, after the window it belongs to is known.
func (m *priorityManager) observe(p *core.Packet, key core.FlowKey) {
	if m.t0 == 0 {
		m.t0 = p.Timestamp
	}
	tag, ok := m.tagged[key]
	if !ok {
		return
	}
	tag.lastSeen = p.Timestamp
	if p.Protocol == "tcp" && (p.Flag(core.FlagFIN) || p.Flag(core.FlagRST)) {
		m.off(key, tag, p.Timestamp, -1, "connection closed")
	}
}

// windowClosed runs once per closed window with its prediction and the
// bytes each flow carried in it. now is the capture time of the packet
// that closed the window.
func (m *priorityManager) windowClosed(window, packets int, label string, conf float64, flowBytes map[core.FlowKey]int64, now float64) {
	for key, tag := range m.tagged {
		if now-tag.lastSeen > m.idle {
			m.off(key, tag, now, window, fmt.Sprintf("idle %.0fs", now-tag.lastSeen))
		}
	}
	if label == "" || conf < m.minConf || packets < m.minPackets {
		return
	}
	var total int64
	type fb struct {
		key   core.FlowKey
		bytes int64
	}
	var flows []fb
	for k, b := range flowBytes {
		if !couldBeService(k, label) {
			continue
		}
		flows = append(flows, fb{k, b})
		total += b
	}
	sort.Slice(flows, func(i, j int) bool { return flows[i].bytes > flows[j].bytes })
	var covered int64
	for i, f := range flows {
		if i >= 3 || covered >= total*8/10 || f.bytes*10 < total {
			break
		}
		covered += f.bytes
		if tag, ok := m.tagged[f.key]; ok {
			tag.label = label // still streaming; keep the tag fresh
			continue
		}
		tag := &priorityTag{label: label, since: now, lastSeen: now}
		m.tagged[f.key] = tag
		m.ons++
		m.apply(fmt.Sprintf("add element inet csc5991 video_flows { %s timeout 3m }", nftElement(f.key)))
		fmt.Printf("  ▶ PRIORITY ON  [%s] %s (%.1f MB in window %d, confidence %.2f) — %d connection(s) prioritized\n",
			label, describeFlow(f.key), float64(f.bytes)/1e6, window, conf, len(m.tagged))
		m.event(now, window, "on", label, conf, f.key, f.bytes, "active confident window")
	}
}

// flowsClosed is the per-connection rule (-priority-by flow, the default):
// each connection that carried >= minPackets packets in the window has
// already been labeled on its own (flowScorer); tag the ones whose OWN label
// is a streaming service at >= minConf, on a port/transport that service
// could use. Unlike windowClosed, a connection can't be tagged because the
// window as a whole looked like a service -- in the first QoS test, the
// moment 16 downloads started, the window was labeled netflix at 0.999 and
// two of the downloads were prioritized.
func (m *priorityManager) flowsClosed(window int, flows []flowResult, now float64) {
	for key, tag := range m.tagged {
		if now-tag.lastSeen > m.idle {
			m.off(key, tag, now, window, fmt.Sprintf("idle %.0fs", now-tag.lastSeen))
		}
	}
	for _, f := range flows {
		if f.packets < m.minPackets || !couldBeService(f.key, f.label) {
			continue
		}
		if _, tagged := m.tagged[f.key]; !tagged && m.maxWindowBytes > 0 && f.bytes > m.maxWindowBytes {
			delete(m.streaks, f.key) // too fast to be a video player: start over
			continue
		}
		if tag, ok := m.tagged[f.key]; ok {
			if f.conf >= m.minConf {
				tag.label = f.label
			}
			continue
		}
		// Streak: a scored window that is unconfident or names a different
		// service resets it; windows where the connection is too quiet to be
		// scored neither count nor reset (a video fetches in bursts).
		st := m.streaks[f.key]
		if f.conf < m.minConf {
			delete(m.streaks, f.key)
			continue
		}
		if st == nil || st.label != f.label {
			st = &streak{label: f.label}
			m.streaks[f.key] = st
		}
		st.n++
		st.lastWindow = window
		if st.n < m.streakNeed {
			fmt.Printf("    · candidate [%s] %s (its own label, %.2f): %d/%d confident windows\n",
				f.label, describeFlow(f.key), f.conf, st.n, m.streakNeed)
			continue
		}
		delete(m.streaks, f.key)
		m.tagged[f.key] = &priorityTag{label: f.label, since: now, lastSeen: now}
		m.ons++
		m.apply(fmt.Sprintf("add element inet csc5991 video_flows { %s timeout 3m }", nftElement(f.key)))
		fmt.Printf("  ▶ PRIORITY ON  [%s] %s (its own label, %.2f; %d pkts, %.1f MB in window %d) — %d connection(s) prioritized\n",
			f.label, describeFlow(f.key), f.conf, f.packets, float64(f.bytes)/1e6, window, len(m.tagged))
		reason := "connection's own label"
		if m.streakNeed > 1 {
			reason = fmt.Sprintf("connection's own label, %d windows in a row", m.streakNeed)
		}
		m.event(now, window, "on", f.label, f.conf, f.key, f.bytes, reason)
	}
	// Forget streaks of connections that went quiet for the idle timeout.
	for key, st := range m.streaks {
		if float64(window-st.lastWindow)*10 > m.idle {
			delete(m.streaks, key)
		}
	}
}

// refresh restarts the 3-minute nftables timeout of every tagged connection,
// once per window, so an enforced mark lasts as long as its tag does.
func (m *priorityManager) refresh() {
	if !m.enforce || len(m.tagged) == 0 {
		return
	}
	var b strings.Builder
	for key := range m.tagged {
		el := nftElement(key)
		fmt.Fprintf(&b, "destroy element inet csc5991 video_flows { %s }\nadd element inet csc5991 video_flows { %s timeout 3m }\n", el, el)
	}
	m.apply(b.String())
}

func (m *priorityManager) apply(script string) {
	if !m.enforce {
		return
	}
	if err := m.nft(script); err != nil {
		fmt.Fprintf(os.Stderr, "  priority enforce: nft failed: %v\n", err)
	}
}

// nftElement is a connection as video_flows stores it: client ip . protocol
// . client port . server ip . server port.
func nftElement(k core.FlowKey) string {
	sIP, sPort := serverSide(k)
	cIP, cPort := k.IPB, k.PortB
	if sIP == k.IPB && sPort == k.PortB {
		cIP, cPort = k.IPA, k.PortA
	}
	return fmt.Sprintf("%s . %s . %d . %s . %d", cIP, k.Protocol, cPort, sIP, sPort)
}

// runNft feeds a script to nft on stdin.
func runNft(script string) error {
	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(script)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (m *priorityManager) off(key core.FlowKey, tag *priorityTag, now float64, window int, reason string) {
	delete(m.tagged, key)
	m.apply(fmt.Sprintf("destroy element inet csc5991 video_flows { %s }", nftElement(key)))
	m.offs++
	fmt.Printf("  ■ PRIORITY OFF [%s] %s — %s, after %.0fs — %d connection(s) still prioritized\n",
		tag.label, describeFlow(key), reason, now-tag.since, len(m.tagged))
	m.event(now, window, "off", tag.label, 0, key, 0, reason)
}

func (m *priorityManager) event(now float64, window int, ev, label string, conf float64, key core.FlowKey, bytes int64, reason string) {
	if m.log == nil {
		return
	}
	_, serverPort := serverSide(key)
	w := ""
	if window >= 0 {
		w = strconv.Itoa(window)
	}
	c := ""
	if conf > 0 {
		c = strconv.FormatFloat(conf, 'f', 4, 64)
	}
	m.log.Write([]string{
		strconv.FormatFloat(now-m.t0, 'f', 1, 64), w, ev, label, c, flowID(key), key.Protocol,
		strconv.Itoa(serverPort), strconv.FormatInt(bytes, 10), reason,
	})
	m.log.Flush()
}

func (m *priorityManager) close() {
	mode := "dry run"
	if m.enforce {
		mode = "enforced"
		m.apply("flush set inet csc5991 video_flows")
	}
	fmt.Printf("priority (%s): %d tagged, %d released, %d still tagged at exit\n", mode, m.ons, m.offs, len(m.tagged))
	if m.logF != nil {
		m.log.Flush()
		m.logF.Close()
	}
}

// couldBeService reports whether a flow can carry the labeled service:
// HTTPS/HTTP server port, and the label's transport (QUIC is UDP).
func couldBeService(k core.FlowKey, label string) bool {
	_, port := serverSide(k)
	if port != 443 && port != 80 {
		return false
	}
	if label == "youtube_quic" {
		return k.Protocol == "udp"
	}
	return k.Protocol == "tcp"
}

// serverSide guesses which end is the server: the one on a service port
// (<1024), else the lower port.
func serverSide(k core.FlowKey) (string, int) {
	if k.PortA < 1024 && k.PortB >= 1024 || k.PortA < k.PortB {
		return k.IPA, k.PortA
	}
	return k.IPB, k.PortB
}

// describeFlow is for the terminal only: client ↔ server with addresses.
func describeFlow(k core.FlowKey) string {
	sIP, sPort := serverSide(k)
	cIP, cPort := k.IPB, k.PortB
	if sIP == k.IPB && sPort == k.PortB {
		cIP, cPort = k.IPA, k.PortA
	}
	return fmt.Sprintf("%s %s:%d ↔ %s:%d", k.Protocol, cIP, cPort, sIP, sPort)
}

func flowID(k core.FlowKey) string {
	h := fnv.New32a()
	fmt.Fprintf(h, "%s|%d|%s|%d|%s", k.IPA, k.PortA, k.IPB, k.PortB, k.Protocol)
	return fmt.Sprintf("%08x", h.Sum32())
}
