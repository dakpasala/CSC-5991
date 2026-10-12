# CLAUDE.md

This file orients Claude Code sessions in this repo. **[AGENTS.md](AGENTS.md) is the
authoritative project-instructions file** (Codex convention) — read it in full before
doing any nontrivial work here. This file is a shorter map on top of it.

## What this project is

A CSC-5991 thesis: lightweight ML classification of network activity *without* deep
packet inspection. As of October 2026 the active phase builds a labeled, per-10-second-
window traffic-statistics CSV from real per-session capture directories (raw `.pcap` +
browser-level telemetry), for four streaming services (YouTube split into
`youtube_tcp`/`youtube_quic`, Netflix, Twitch, Amazon). No model training exists yet.

Two earlier phases are archived, not active:
- `dataset-collection/` — live Selenium-driven browsing capture (first phase).
- `10-5-oldcode/` — the `all_traffic_time_10.pkl`-based phase (second phase, a
  pre-aggregated dataset with no real flow-level data recoverable from it). Superseded
  because it couldn't support true flow/5-tuple-level statistics.

## Layout

- `<hash>/` directories (e.g. `9fa4dd9141aaa60a5f590a2c00bca0ad/`) — one real session
  each: `dump.pcap` (raw capture, snaplen-truncated to 200 captured bytes but with true
  original packet length preserved per-record), `exp.txt` (tiny label/run-config file),
  `requests_history.json` (real browser-level HTTP request log with genuine
  `content-length`/timing — not inferred), and a large per-session JSON of client-side
  video-element telemetry (not currently used by the pipeline below).
- `src/pcap_reader.py` — endian-aware plain-`.pcap` reader that keeps the true,
  pre-truncation packet length (`dpkt.pcap.Reader`'s own iterator discards it).
- `src/packet_parser.py` — Ethernet/IPv4/TCP-or-UDP parsing; payload size is computed
  from header-declared lengths, not from how many payload bytes actually got captured.
- `src/flow_tracker.py` — 5-tuple flow identification across a packet stream, client/
  server determination (SYN-based for TCP, first-packet-sender assumption for UDP).
- `src/flow_metrics.py` — per-flow retransmission/out-of-order/RTT detection, using
  each flow's full session history so cross-window retransmissions are caught
  correctly. Only packets that consume sequence space (real payload or a SYN) are
  checked — a bug where pure ACKs' naturally-repeated sequence number was being
  misread as mass retransmission was found and fixed here (see git history).
- `src/requests_log.py` — loads `requests_history.json`, filters to the requests that
  actually belong to the service under test (by domain keyword), extracts real
  segment sizes and request/completion timing.
- `src/window_features.py` — the orchestrator: buckets everything into fixed-size time
  windows and computes the full ~130-column feature table (see AGENTS.md for the
  complete column list and the direction-scoped up/down/total design).
- `src/build_session_csv.py` — CLI entry point. Takes a session directory, appends its
  windows to a shared, accumulating CSV named by window size
  (`session_statistics_<N>_second_window.csv`), so re-running it on more session
  directories builds up one combined dataset rather than one file per session. Either
  positional argument (`session_directory`, `--window-seconds`) can be the literal
  string `"all"` instead of a specific value: `"all"` for the directory
  auto-discovers every subdirectory containing both `dump.pcap` and `exp.txt` (skipping
  everything else, e.g. `src/`, archived phases, non-session dirs); `"all"` for
  `--window-seconds` runs the full pipeline at 10/8/4/2/1 seconds, each to its own
  file. Both can be `"all"` at once for the full cross-product — meant for a Linux
  batch run once every session directory is present. Each (directory, window-size)
  combination runs in its own try/except; one failure doesn't abort the batch.

## Rules worth not forgetting

- **No IP addresses or ports in the output CSV.** 5-tuples are used internally to
  identify flows; none of that identifying detail should reach the final columns.
- **Window size is configurable** (`--window-seconds`, default 10) and changes the
  output filename accordingly — different window sizes must never land in the same
  CSV.
- **Every distribution-style column gets all six stats**: min, max, mean, median,
  p25, p75. Pure counts (packet count, byte count, flow count) are single numbers per
  window — no percentile breakdown applies to those.
- **Retransmission/out-of-order/RTT only apply to TCP.** UDP (QUIC) flows produce no
  signal for these, same as what a real no-DPI observer would see.
- **The application-layer stats (`request_count`, `segment_size_bytes_*`,
  `request_duration_ms_*`) are real, not inferred** — read directly from
  `requests_history.json`'s actual HTTP request/response records, a genuine upgrade
  over the burst-timing inference the first pcapng-based phase had to rely on.
- **`exp.txt`'s label format is verified against twitch/netflix/youtube examples**
  (all fit `"<prefix>_<service>.<suffix>"`) — amazon not yet seen; re-verify the moment
  one is available rather than assuming the format generalizes.
- **No confirmed `youtube_quic` session yet.** The one YouTube directory processed so
  far (`9b020fe43e6d5751a2e6face89a261c1`) is 99.91% TCP packets, so it resolved to
  `youtube_tcp`. The QUIC/TCP split logic (`frac_udp` majority vote in
  `_decide_label`) is implemented and will zero out ~40 TCP-specific columns for a
  genuinely QUIC-dominant session, but that behavior is reasoned, not yet observed
  against real QUIC traffic.
- Session-aware splitting (train/test by `session_id`, not by row) still applies once
  this feeds a model, same reasoning as the earlier phase.

## Maintenance expectations

No test suite or formatter convention has been established for this phase yet. Before
calling anything done: verify numeric output against hand-checkable facts in the real
data (e.g. `up_count + down_count == total_count`, no unexpectedly-all-null columns),
the same habit that caught the retransmission-counting bug above.
