# CLAUDE.md

This file orients Claude Code sessions in this repo. **[AGENTS.md](AGENTS.md) is the
authoritative project-instructions file** (Codex convention) — read it in full before
doing any nontrivial work here. This file is a shorter map on top of it.

## What this project is

A CSC-5991 thesis: lightweight ML classification of network activity *without* deep
packet inspection. As of October 2026 the active phase builds a labeled, per-10-second-
window traffic-statistics CSV from real per-session capture directories (raw `.pcap` +
browser-level telemetry), for four streaming services (YouTube split into
`youtube_tcp`/`youtube_quic`, Netflix, Twitch, Amazon). As of November 2026 a full
AutoGluon training run on that data has finished (`no_applayer` variant, 19,468 sessions,
0.98 test accuracy). Single-XGBoost feature ablation then cut the feature set: option C has
22 features and 0 buffers, at 0.980 test accuracy.

The classifier now runs **live on the professor's OpenWrt router** as one static Go binary: Go-native
XGBoost, and selective feature extraction that computes only the model's features. It is evaluated with
known ground truth (headless Chromium on the Linux box behind the router).

The headline finding is drift. **Fingerprint features** (TCP receive window, RTT, TCP flags,
retransmission/out-of-order) changed since the 2018–19 data. Models that use them fail on live Twitch
(0.06–0.38). `final_shape_streaming` (28 traffic-shape features, 0.964 offline) is the most accurate model
live (Twitch 0.88, YouTube QUIC 1.00) and among the cheapest: no per-flow state, ~37 MB, <2 % CPU.
Priority is enforced on the router (nftables + `cake`, test runs only) by classifying each
connection on its own; long bulk downloads remain the one false positive. See "Router deployment" below and AGENTS.md. Thesis writing has started in `thesis/` (gitignored, local only).

Two earlier phases are archived, not active. Both directories were removed from the working
tree in commit `e6953ec`; recover them from git history (`git show e6953ec^:<path>`) if needed:
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
`sudo go run ./live_capture_go/live -interface eth0`), since `go.mod` lives there. Because of
this, its default output path (see `live_runs/` below) lands relative to `src/`, not the repo
root, unless run from the repo root instead.

Verified against the real batch pipeline for the same session: 6,499 of 6,500 compared values
identical; the one outlier (`up_interarrival_time_min`, the sparsest/last window) was traced to a
~240-nanosecond floating-point rounding difference between Python/dpkt's and Go/gopacket's
timestamp-parsing pipelines (confirmed by dumping raw per-packet timestamps from both side by
side), not a logic bug. Also dramatically faster: 0.007–0.026s per window vs. Python's
0.2–0.4s+ at the same packet volumes. The live-capture command was also tested for real, not just
built — it successfully captured genuine live traffic off a Mac's own `en0` interface with no
`sudo` needed, producing correctly-structured real-time windows.

## Inference (`src/inference/predict.py`, `src/live_capture_go/inference/`)

**Trained model.** The `no_applayer` 10s run (`AutogluonModels/window_10s_19468sessions_no_applayer`,
70GB, Linux box only) scored 0.98 accuracy / 0.99 macro F1 on 212,184 held-out windows. Its best
model, `WeightedEnsemble_L3`, is only ~0.11 points better on validation (0.985771) than the best
single L1 model (0.984623), and `clone_for_deployment()` only shrank it to 50GB, because every L2
stacker depends on all ~75 L1 models. So a single model was cloned instead, chosen **by
validation score, not test score** (choosing on test would leak): `XGBoost_r22_BAG_L1` (val
0.984536, 3rd best L1, ~15x faster to predict than the top LightGBM) →
`AutogluonModels/window_10s_19468sessions_no_applayer_xgb22`, 333MB, copied to the Mac.
`AutogluonModels/` is gitignored.

**Local environment.** The repo's `.venv` is Python 3.14 with no AutoGluon. Inference uses a
separate `.venv-infer` (python3.13) pinned to the Linux box's versions: `autogluon.tabular==1.6.3`,
`xgboost==3.4.1`, `scikit-learn==1.9.1`, `numpy==2.5.4`, `pandas==2.3.3`. It installs only
`autogluon.tabular`, not the full `autogluon` package (which pulls in PyTorch). Loading prints a
harmless Python micro-version (3.13.5 vs 3.13.2) and Linux-vs-Darwin mismatch warning.

**Sidecar pipeline.** `live_capture_go/live` and `live_capture_go/replay` take `-model-dir`. When
it's set, `live_capture_go/inference/sidecar.go` starts `predict.py` once as a child process, and
every closed window row is piped to its stdin as CSV text (no intermediate stats file). `predict.py`
appends to `live_runs/{live,replay}_predictions_<N>_second_window[_<timestamp>].csv`
(`window_index, window_start_s, window_end_s, predicted_label, confidence, prob_<class>...`) and
prints a JSON line per window, which Go echoes as `-> window N predicted: <label> (<conf>)`.
- With `-model-dir`, the stats CSV is skipped unless `-output` is also given explicitly.
- `Send` never blocks: rows go through a buffered channel to a writer goroutine, so a slow Python
  start can't back up packet capture.
- The child runs in its own process group (`Setpgid`), so Ctrl+C only reaches Go. Go flushes the
  last partial window, closes the pipe, and waits for every queued prediction before exiting.
- Other flags: `-python` (default `../.venv-infer/bin/python`), `-predict-script` (default
  `inference/predict.py`), `-predictions-output`. All defaults assume running from `src/`.

**Verified.** Replay of all 7 local sessions → every one correct by majority vote (`lab08_netflix`
had 5/49 windows called `youtube_tcp`, the same Netflix/youtube_tcp confusion as the full test
report). Note that these 7 may be in the training set. A 15-session held-out sample (3 per class,
reproducing `train_autogluon.py`'s split) scored 0.9963 over 815 windows, 15/15 sessions correct.
Live on the Mac's `en0` with YouTube playing: 11 of 13 windows labeled YouTube (mostly
`youtube_quic`), and both misses were low-confidence `netflix` (0.56, 0.45). Idle traffic with
nothing streaming gets ~0.5 confidence: the model is closed-world and always picks one of the 5
classes. Ctrl+C shutdown loses no predictions and leaves no `predict.py` process behind.

## XGBoost feature ablation (`src/train_xgboost_ablation.py`)

Trains plain single XGBoost models in minutes. The test sessions are the same as AutoGluon's.
**Every decision is made on a session-aware validation split**; test is reported only.

- Fixed sets, RFE by feature or by distribution family, and router-cost columns (`dist_families`,
  `buffered_families`).
- Router cost is per **family**: min/max/mean are O(1), while median/p25/p75 need a full buffer.
- `--final <feature list>` trains one deployable model:
  - fit with early stopping, then refit on fit + validation;
  - writes `XGBoostModels/10s_no_applayer/final_<name>.json` + `.features.json` + `classes.json`.
- Feature lists live in `benchmarks/feature_sets/`, and results in
  `benchmarks/xgb_ablation_10s_no_applayer.csv` and `benchmarks/feature_selection_results.md`.
- The Linux box is shared, so cap threads with `--n-jobs` and pin with `taskset -c <cores>` (~10 cores).
- A low-information feature set can run to 2,000 rounds. Use `--learning-rate 0.3 --max-rounds 600`.

## Router deployment and live evaluation

- **Router.** OpenWrt aarch64, ~1 GB RAM, no swap, no Python/gcc. Reached as `root@192.168.1.1` from the
  Linux box, or `prs-router` from the Mac. Captures on `lan2`. **GRO must be off and reverts on reboot**:
  `ethtool -K eth1 gro off; ethtool -K lan2 gro off`.
- **`./deploy_router.sh`** (Mac, repo root):
  - cross-compiles `live-router` (`CGO_ENABLED=0`, pure-Go AF_PACKET capture, 3.6 MB static);
  - copies it, the model and `router/qos.sh` to `/root/csc5991/`;
  - `MODEL=<json>` picks the model.
- **`live-router`.** A `.json` `-model-dir` is scored by the Go evaluator (`live_capture_go/xgbmodel`,
  identical to xgboost). Only the model's features are computed (`core_go/selective.go`;
  `-all-features` overrides). Other flags:
  - `-port-direction`: flow direction by service port; default on live, off in replay, for training
    parity;
  - `-perf-output`: per-window CPU/memory/timing;
  - `-max-duration`: safety stop;
  - `-priority` (dry run) / `-priority-enforce`: tag connections by their **own** label (each
    connection scored on its own packets, `live/flows.go`), same service at ≥0.93 for 5 windows in a
    row, 443/80 with the label's transport, at most 25 Mbit/s. Full flag list in AGENTS.md.
- **QoS (`router/qos.sh up|down|status`, test runs only, professor-approved).** `cake` on `lan2` egress
  (`RATE`, default 100 Mbit, `diffserv4 flowblind`) plus nftables set `inet csc5991 video_flows`, whose
  connections' download packets get DSCP AF41. **Run `qos.sh down` after every test session.**
- **`src/live_capture/stream_and_check.py`** (Linux box) runs one ground-truth test:
  1. start `live-router` over ssh;
  2. play a video in headless Chromium (quiet flags, bot detection masked, stall
     screenshots/reloads);
  3. stop and score it.

  The number that measures the classifier is **playing-window accuracy**. Results go to
  `live_runs/eval_*`; summaries are kept in `live_runs/tests/*.md`, and raw runs are deleted after
  summarizing.
- **Results.**
  - Option C: 1.00 on YouTube QUIC (3 runs), but 0.19 on Twitch.
  - The all-features model was the worst live: the QUIC ACK ratio dropped from 0.505 to 0.176
    upload/download packets since 2018–19.
  - Without fingerprint features: `final_shape_streaming` Twitch 0.88, YouTube QUIC 1.00;
    `final_option_c_no_fingerprint` YouTube TCP 0.88.
  - Single runs except option C/YouTube.
  - QoS (`live_runs/tests/qos_priority_results.md`): with background traffic the whole-window label
    collapses (0.22) but the video connection's own label holds (1.00). Light traffic is never tagged;
    long bulk HTTPS downloads still are (closed-world model). No measured video-quality benefit yet.
  - Twitch: correct unshaped and at 500 Mbit shaping, but heavy shaping (12 Mbit) flips its label to
    `youtube_tcp` by changing the client's ACK pattern. Force congestion with load, not a low `RATE`.
  - Under heavy load, downloads took 97 % of the Video tier's bytes; a 25 Mbit/s cap
    (`-priority-max-mbps`, default) blocks them on fast lines only. No measured video-quality benefit
    yet (the video was never short of bandwidth). CPU exceeds one core at full line rate. Next QoS steps
    wait on the professor.

## Output folder conventions: `benchmarks/` and `live_runs/`

Every benchmarking/feature-importance output (`benchmark_feature_costs.py`,
`benchmark_feature_memory.py`, `train_autogluon.py`'s feature-importance CSV) is written under
`benchmarks/`, created with `os.makedirs("benchmarks", exist_ok=True)` right before the write.
Every live-traffic-statistics output (`live_capture_pipeline.py`/`replay_pcap_as_live.py` and
their Go ports `live_capture_go/live`, `live_capture_go/replay`) is written under `live_runs/`
the same way (`os.MkdirAll("live_runs", 0755)` on the Go side). Both default-output filename
patterns inside those folders are already covered by existing `.gitignore` glob entries
(no leading slash, so they match at any depth) — no `.gitignore` changes needed when adding
either folder. `--output`/`-output` still overrides the default path explicitly if given.

One deliberate exception: `benchmark_feature_memory.go` (see below) writes to
`../benchmarks/...` instead of plain `benchmarks/...`, since its whole point is to land next to
the Python benchmark's output for direct comparison, not a separate `src/benchmarks/` copy —
unlike the live-capture Go tools, which intentionally land wherever the process's CWD happens
to be, matching their Python originals' behavior.

## Go memory benchmark (`src/benchmarking/benchmark_feature_memory.go`)

A Go port of `benchmark_feature_memory.py`, same stat categories and CSV schema, written to
`benchmarks/feature_extraction_memory_benchmark_go.csv` (a separate file from the Python one,
so both can be compared). Built specifically because CPython caches small integers (-5 to 256)
as process-wide singletons — a Python list full of repeated `0`s (e.g. `up_payload_size_buffer`,
mostly bare-ACK packets) gets measured as far cheaper than it really costs on a platform without
that caching, which matters because the real deployment target is Go, not Python. Confirmed at
real 100-session scale: Python's benchmark showed an inconsistent down:up ratio across metrics
(~8x for `payload_size`/`packet_size`, ~2x for `recv_window`/`interarrival_time`); the Go version
showed a consistent ~2x ratio across all four, confirming the 8x figure was a measurement
artifact of Python's int caching, not a real traffic asymmetry. Also surfaced independently: Go's
flat `FlowState` struct costs ~74 bytes per flow vs. Python's ~520 — a real per-flow memory
advantage of the actual router implementation, not a benchmarking artifact. Since `core_go`'s
`ComputeFlowMetrics` doesn't expose its internal per-direction bookkeeping state, this file
duplicates that bookkeeping loop locally to get at it for sizing — same reasoning as the Python
benchmark's own `_flow_state_sizes` duplicating `flow_metrics.py`'s loop for the same reason. Run
from `src/`: `go run ./benchmarking -source-dir /mnt/NAS/salmo-trutta/traces -num-sessions 100`.

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
  label, and is ignored). Amazon has since appeared in the real-world shape
  (`"lab2_amazon.run250-1523976505"`, local session `001f998ac218390146d1c568eaa21cd8`).
- **`youtube_quic` is now confirmed on real data.** The full dataset has 30,086
  `youtube_quic` test windows (F1 0.98), and local session `a03921b7cdc37b4f42965503297b4be9`
  (`lab09_youtube`) resolves to it. The QUIC/TCP split is the `frac_udp` majority vote in
  `_decide_label`, which zeroes out ~40 TCP-specific columns for a QUIC-dominant session.
  QUIC did *not* turn out to be the trivially easy class AGENTS.md predicted: Amazon and
  Twitch score 1.00, while `youtube_quic` sits at 0.98 with the TCP-based classes.
- Session-aware splitting (train/test by `session_id`, not by row) still applies once
  this feeds a model, same reasoning as the earlier phase. AutoGluon's *internal* bagging
  splits by row, so its `score_val` is optimistic; pass session IDs as `groups` next time.
- **Choose on validation, report test.** Picking a model, feature set or step by test
  accuracy and then reporting that test accuracy is data snooping.
- **Offline accuracy does not predict live accuracy here.** Prefer traffic-shape features
  over fingerprint features: TCP receive window, RTT, TCP flag counts, retransmission/
  out-of-order. They encode the servers, client OS and capture point, all of which changed
  since 2018–19. RTT/retransmission also need per-flow sequence state, the main memory cost
  on the router.
- **Live capture must measure what training measured.** GRO off at the router (≤1,514 B
  packets), and flow direction checked: upload bytes ≪ download bytes for video.
- **Router cost is counted in distribution buffers and per-flow state, not feature count.**

## Maintenance expectations

No test suite or formatter convention has been established for this phase yet. Before
calling anything done: verify numeric output against hand-checkable facts in the real
data (e.g. `up_count + down_count == total_count`, no unexpectedly-all-null columns),
the same habit that caught the retransmission-counting bug above.
