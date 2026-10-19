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
- `src/core/pcap_reader.py` — endian-aware plain-`.pcap` reader that keeps the true,
  pre-truncation packet length (`dpkt.pcap.Reader`'s own iterator discards it).
- `src/core/packet_parser.py` — Ethernet/IPv4/TCP-or-UDP parsing; payload size is computed
  from header-declared lengths, not from how many payload bytes actually got captured.
- `src/core/flow_tracker.py` — 5-tuple flow identification across a packet stream, client/
  server determination (SYN-based for TCP, first-packet-sender assumption for UDP).
- `src/core/flow_metrics.py` — per-flow retransmission/out-of-order/RTT detection, using
  each flow's full session history so cross-window retransmissions are caught
  correctly. Only packets that consume sequence space (real payload or a SYN) are
  checked — a bug where pure ACKs' naturally-repeated sequence number was being
  misread as mass retransmission was found and fixed here (see git history).
- `src/core/requests_log.py` — loads `requests_history.json`, filters to the requests that
  actually belong to the service under test (by domain keyword), extracts real
  segment sizes and request/completion timing.
- `src/core/window_features.py` — the orchestrator: buckets everything into fixed-size time
  windows and computes the full ~130-column feature table (see AGENTS.md for the
  complete column list and the direction-scoped up/down/total design).
- `src/build_session_csv.py` — CLI entry point. Takes a session directory, appends its
  windows to a shared, accumulating CSV named by window size
  (`session_statistics_<N>_second_window.csv`), so re-running it on more session
  directories builds up one combined dataset rather than one file per session. Either
  positional argument (`session_directory`, `--window-seconds`) can be the literal
  string `"all"` instead of a specific value: `"all"` for the directory
  auto-discovers every subdirectory of `--source-dir` (default: the **parent** of the
  current working directory) containing both `dump.pcap` and `exp.txt` (skipping
  everything else) — on the real Linux box the raw session directories sit as siblings
  of this repo by default, but `--source-dir` can point anywhere (e.g. a NAS mount),
  so there's no need to `cp` session directories locally first. `--batch-index`/
  `--batch-size` (default 500) slice that listing into fixed-size chunks for working
  through a huge source directory one batch at a time across multiple runs. Output
  CSVs always land in the repo root regardless of `--source-dir`. `"all"` for
  `--window-seconds` runs the full pipeline at 10/8/4/2/1 seconds, each to its own
  file. Both can be `"all"` at once for the full cross-product — meant for a Linux
  batch run once every session directory is present. Each directory runs in its
  own try/except; one failure doesn't abort the batch. `"all"` also parallelizes
  across session **directories** with a `ProcessPoolExecutor` (one worker per CPU
  core by default, override with `--workers`) — only the CSV write stays
  serialized in the main process, so concurrent workers never corrupt the shared
  output file. CPU-bound, not GPU-bound: the cost is per-packet branchy logic
  with a sequential per-flow dependency, not bulk numeric work a GPU helps with.
  Parsing the pcap and running flow tracking happens exactly **once per
  directory**, regardless of how many window sizes are requested — window size
  only affects the cheap final bucketing step — so `--window-seconds all` costs
  barely more than one window size, not 5x more. Verified both for correctness
  (output numerically identical to the pre-optimization code) and speed (a single
  session at all 5 window sizes dropped from 28s to 11s wall-clock). **Controlled/
  lab-condition sessions are skipped entirely**, not written to any CSV — detected
  live per directory (via `src/scripts/find_controlled_sessions.py`'s `is_controlled_experiment()`)
  before the pcap is even opened, and logged as `skipping <dir> (controlled
  experiment)` rather than silently dropped or counted as a failure. This is a
  policy change: earlier in the project every directory was processed uniformly and
  the mix was split apart afterward (see `src/scripts/filter_controlled_sessions.py` and the
  `session_statistics_*_second_window_controlled.csv` files it produced from the
  real 19,672-directory batch — 18,957 organic, 715 controlled).

`src/` is organized by category. The 6 shared library modules above
(`pcap_reader.py`/`packet_parser.py`/`flow_tracker.py`/`flow_metrics.py`/`requests_log.py`/
`window_features.py`) live in `src/core/`, since every other category imports from them.
`find_controlled_sessions.py` lives in `src/scripts/` instead, even though `build_session_csv.py`
and both benchmarking scripts import `is_controlled_experiment()` from it — it's genuinely
dual-purpose (also a standalone CLI tool, see below), so it didn't fit the "pure shared library"
rule `src/core/` is organized around. `src/build_session_csv.py` (the main CLI entry point
`run_all_sessions.sh` directly references) stays at `src/` root, one level above `src/core/`.
Everything else is grouped by what it's for: `src/scripts/` (one-off utilities:
`find_controlled_sessions.py`, `filter_controlled_sessions.py`, `strip_application_layer.py` —
pure stdlib+pandas, no core-module imports), `src/progress/` (`check_progress.py`,
`check_model_failures.py` — same), `src/benchmarking/` (`benchmark_feature_costs.py`,
`benchmark_feature_memory.py`), `src/live_capture/` (`live_capture_pipeline.py`,
`replay_pcap_as_live.py`). Files outside `src/core/`/`src/scripts/` that need either one have
`sys.path.insert(0, ...)` line(s) before those imports — `build_session_csv.py` needs both
`src/core/` (for `window_features`) and `src/scripts/` (for `find_controlled_sessions`); the two
benchmarking scripts need those same two plus `src/` itself (since they also import
`build_session_csv.py` directly, which stays there). Invoked the same way as before, just with
the new path, e.g. `python src/benchmarking/benchmark_feature_costs.py`. `src/train_autogluon.py`
deliberately stayed at `src/` root, unmoved, since it was actively training when this
reorganization happened (it imports none of the above anyway — just pandas/sklearn/autogluon,
reading CSVs directly).

## Go port (`src/core_go/`, `src/live_capture_go/`)

The professor asked for real-time/router deployment code to be in Go, not Python. `src/core_go/`
is a field-for-field Go port of the live-relevant parts of `src/core/` (packet parsing, flow
tracking, retransmission/RTT detection, window-stat computation) — `requests_log.py`/
`find_controlled_sessions.py` are NOT ported, since there's no `requests_history.json`/`exp.txt`
on a live capture for them to act on. `src/live_capture_go/replay/` ports
`replay_pcap_as_live.py`; `src/live_capture_go/live/` ports `live_capture_pipeline.py` but
architecturally better — no `tcpdump` subprocess or file-rotation polling needed, since Go's
goroutines (no GIL) let capture and window-processing run genuinely concurrently in one process.
Both share one `core.WindowAccumulator` state machine.

This is its own Go module (`src/go.mod`) — **run Go commands from inside `src/`**, not the
project root (`go build ./...`, `go run ./live_capture_go/replay -pcap ../sessions/<id>/dump.pcap`,
`sudo go run ./live_capture_go/live -interface eth0`), since `go.mod` lives there.

Verified against the real batch pipeline for the same session: 6,499 of 6,500 compared values
identical; the one outlier (`up_interarrival_time_min`, the sparsest/last window) was traced to a
~240-nanosecond floating-point rounding difference between Python/dpkt's and Go/gopacket's
timestamp-parsing pipelines (confirmed by dumping raw per-packet timestamps from both side by
side), not a logic bug. Also dramatically faster: 0.007–0.026s per window vs. Python's
0.2–0.4s+ at the same packet volumes. The live-capture command was also tested for real, not just
built — it successfully captured genuine live traffic off a Mac's own `en0` interface with no
`sudo` needed, producing correctly-structured real-time windows.

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
- **`exp.txt`'s label comes from substring-matching a known service name**
  (`youtube`/`netflix`/`twitch`/`amazon`), not positional parsing — two shapes exist
  and disagree on where the service sits relative to `_`/`.`: real-world sessions
  (`"<prefix>_<service>.<suffix>"`, e.g. `"lab10_youtube.run30-1549387740"`) and
  controlled lab experiments where the service leads and arbitrary condition text
  follows before `.run` (e.g. `"youtube-0_rate+3000kbps+loss+1%+25%.run2"`, where
  everything between `youtube` and `.run` is experiment config, not part of the
  label, and is ignored). Amazon not yet seen in either shape.
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
