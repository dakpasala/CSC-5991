# CSC-5991 project instructions

## Purpose and long-term goal

This thesis investigates lightweight machine-learning classification of network activity without deep packet inspection. The project has gone through three phases: live Selenium-driven traffic collection (archived in `dataset-collection/`), feature engineering on a pre-aggregated professor-provided dataset (archived in `10-5-oldcode/`, superseded because no real flow-level data was recoverable from it), and the current active phase (October 2026 onward): building a labeled, per-10-second-window traffic-statistics CSV directly from real per-session capture directories, which will feed an ML classifier distinguishing streaming services by traffic shape alone — never by payload content.

## Current phase: per-session window statistics (October 2026)

Each real session is one directory (e.g. `9fa4dd9141aaa60a5f590a2c00bca0ad/`), containing:
- `dump.pcap` — raw packet capture. Snaplen-truncated to 200 captured bytes per packet, but verified: the pcap record header's `len` field (true original length, separate from `caplen`) is preserved per-packet regardless of truncation — confirmed directly (10,471 of 20,000 sampled packets in the one real session seen so far have a true length exceeding 200 bytes, up to several hundred). Always compute packet/payload sizes from header-declared lengths (`hdr.len`, IP total length, IP/TCP header lengths), never from how many payload bytes actually got captured — the latter silently undercounts anything over ~200 bytes.
- `exp.txt` — tiny label/run-config file, e.g. `test1-05_twitch.run276-1525964730`. `src/build_session_csv.py`'s `parse_exp_label()` extracts the service by substring-matching a known name (`youtube`/`netflix`/`twitch`/`amazon`), not by position — two real shapes disagree on where the service sits relative to `_`/`.`: real-world sessions (`"<prefix>_<service>.<suffix>"`) and controlled lab-experiment runs where the service leads and arbitrary condition text follows before `.run`, e.g. `youtube-0_rate+3000kbps+loss+1%+25%.run2` (everything between `youtube` and `.run` here is experiment config, correctly ignored by the substring match). Amazon not yet seen in either shape.
- `requests_history.json` — real browser-level HTTP request log (looks like Chrome's `webRequest` API), keyed by request ID, each with `OnBeforeRequestOptions` (always present) and `onCompleted` (present once finished) sub-records — real timestamps, real `content-length` response headers. This session's log included 1,375 requests, most of it unrelated background traffic (Google scripts, ad/analytics domains like `doubleclick`/`scorecardresearch`/`krxd`) alongside the real Twitch CDN requests (`*.ttvnw.net`, `api.twitch.tv`, etc.) — `src/core/requests_log.py` filters by a per-service domain-keyword list, currently just `twitch`/`ttvnw` for Twitch, `youtube`/`googlevideo`/`ytimg`, `netflix`/`nflxvideo`/`nflximg`, `amazon`/`primevideo`/`media-amazon` — unverified for the other three services, since only a Twitch session has actually been seen.
- A large per-session JSON of high-resolution (~500ms interval) client-side `<video>`-element telemetry (buffered ranges, decoded frame/byte counts, resolution, playback state). Not currently used by the pipeline — real, rich QoE ground truth if it's ever needed, but out of scope for what the professor asked for this round.

### Output: `session_statistics_<N>_second_window.csv`

One row per fixed-size time window within a session (default 10s, via `--window-seconds`; the output filename encodes whatever value was used, so different window sizes never mix in the same file). Running `src/build_session_csv.py` against more session directories **appends** to the same file rather than overwriting — it's meant to accumulate across every session directory handed over. No IP addresses or ports appear in the output; 5-tuples are used only internally to identify flows.

Full column set (~130 columns), confirmed against a real 50-window run:

- **Metadata** (5): `label`, `session_id`, `window_index`, `window_start_s`, `window_end_s`.
- **Flow tracking, window-level** (4): `num_flows`, `num_new_flows` (5-tuple never seen in an earlier window this session), `num_continuing_flows`, `frac_udp`.
- **Direction-scoped ×3 (`up`/`down`/`total`), plain counts** (9): `packet_count`, `byte_count`, `throughput_bps`.
- **Direction-scoped ×3, TCP flag counts** (15): `syn_count`, `ack_count`, `rst_count`, `psh_count`, `urg_count`.
- **Direction-scoped ×3, other counts** (6): `retransmission_count`, `out_of_order_count`.
- **Direction-scoped ×3, each with all 6 stats** (min/max/mean/median/p25/p75 — 72 columns): `payload_size` (true TCP data length), `packet_size` (full wire length), `recv_window`, `interarrival_time`.
- **Window-level, not direction-split, 6 stats** (6): `rtt_ms`.
- **From `requests_history.json`, filtered to video-relevant requests** (13): `request_count`, `segment_size_bytes_*` (6, from real `content-length`), `request_duration_ms_*` (6, from real request→response timing).

YouTube's final label (`youtube_tcp` vs `youtube_quic`) is decided per-session from the actual observed protocol mix (`frac_udp` over the whole session), not assumed from the label alone.

### Bug found and fixed: retransmission over-counting

First run showed `up_retransmission_count` at 50-90% of `up_packet_count` — implausible. Root cause: a pure ACK (no payload, not a SYN) doesn't consume TCP sequence space, so a client sending many ACK-only packets (acking downstream video data) legitimately repeats the exact same sequence number on every one of them — that's normal TCP behavior, not a retransmission. `flow_metrics.py`'s `seen_seqs`/retransmission/out-of-order tracking now only applies to packets that actually consume sequence space (real payload, or a SYN). After the fix, `up_retransmission_count` dropped to a believable 0-6% range. Keep this in mind if a future metric looks suspiciously high relative to its packet count — check whether it's conflating "same value repeated" with "same value resent."

One other real observation, not yet fully explained: `rtt_ms_median` and `rtt_ms_mean` diverge heavily (median ~0.07ms, mean 5-30ms) — a right-skewed distribution where most RTT samples are near-instant (plausible for a controlled lab test environment, consistent with this session's `exp.txt` naming pattern) but a minority are much larger. Worth revisiting once more sessions are available to see if this pattern holds.

### Three real sessions processed; TCP vs QUIC differentiation

As of this writing, three session directories have been processed: a Twitch session (`9fa4dd9141aaa60a5f590a2c00bca0ad`, 490s, 50 ten-second windows), a Netflix session (`9f99ed55d91e4463b7ef8d3f29f7a048`, 605s, 61 windows), and a YouTube session (`9b020fe43e6d5751a2e6face89a261c1`, confirmed TCP via direct protocol-mix check — 99.91% TCP packets — so labeled `youtube_tcp`, 74 windows). No confirmed `youtube_quic` example had been seen at that point -- since confirmed: the full 19,468-session dataset has 30,086 `youtube_quic` test windows, and local session `a03921b7cdc37b4f42965503297b4be9` (`lab09_youtube`) resolves to it. `parse_exp_label` is now substring-based (searches for a known service name rather than assuming a fixed position), verified against all three of twitch/netflix/youtube in both the real-world and controlled-lab-experiment `exp.txt` shapes.

Session directories can contain other files beyond `dump.pcap`/`exp.txt`/`requests_history.json` — e.g. `9b020...` also has `cleaned.pcap`, several `loss_rate_*.pcap` variants, and a `flows/` subdirectory of tiny per-flow pcaps. The pipeline only reads the three known files by name; the rest is ignored by design, not accidentally missed.

**Some real sessions ship an empty or corrupt `requests_history.json`** (found via a real 500-directory batch run: `508185219978e05415905ebcd8555fe1`, a `youtube-0_rate+50kbps+delay+20ms.run1` controlled session, failed with `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` -- the classic signature of parsing an empty file). `load_relevant_requests()` in `src/core/requests_log.py` now catches `JSONDecodeError` and returns `[]`, treating it the same as the file not existing at all, since it's an optional, auxiliary input -- a session's real pcap-derived data shouldn't be thrown away over an unrelated malformed file. Before this fix, one bad `requests_history.json` failed the *entire* session, including all the perfectly good packet-level stats.

**How `youtube_tcp`/`youtube_quic` differentiation actually works**: `frac_udp` (majority vote across the session) is the primary signal, since QUIC runs entirely over UDP. But the practical consequence is bigger than one column — if a session is genuinely QUIC-dominant, roughly 40+ of the ~130 columns go structurally empty for it, not just low: every TCP flag count is exactly 0 (UDP has no TCP header), `recv_window_*` is entirely `NaN` (`_tag_with_flows` only populates it for TCP packets), and `retransmission_count`/`out_of_order_count`/`rtt_ms_*` are all 0/`NaN` (`flow_metrics.compute_flow_metrics` returns immediately for any non-TCP flow). Only packet/byte counts, throughput, and the payload/packet-size distributions (computed from IP/UDP header lengths, not TCP-specific fields) carry real signal for QUIC traffic. Practical implication for evaluation: a model will likely separate `youtube_quic` from the other four classes almost trivially (it's closer to a protocol check than traffic-shape learning), which can inflate a single blended accuracy number — report per-class metrics, not just one overall figure, so the genuinely hard part of the task (telling the four TCP-based classes apart from each other) isn't masked by one easy class. **Outcome on the real `no_applayer` run: this prediction did not hold.** `youtube_quic` scored precision 0.99 / recall 0.98 / F1 0.98 -- in line with the TCP classes, not above them -- while Amazon and Twitch were the near-perfect ones (1.00/1.00). The weakest pair is Netflix (recall 0.97) and `youtube_tcp` (precision 0.96), consistent with Netflix windows being called `youtube_tcp`. Per-class reporting is still the right call, just for a different reason than predicted.

### Batch processing: `all` for directory and/or window size

`src/build_session_csv.py <dir_or_all> [--window-seconds <N_or_all>]` supports both arguments being literal `"all"`:
- `session_directory = "all"` → `discover_session_directories()` scans `--source-dir` (default: the **parent** of the current working directory, `root=".."`) for every subdirectory containing both `dump.pcap` and `exp.txt`, processes each, silently skips anything else. Default changed from the current directory because on the real Linux box the raw session directories sit as siblings of this repo, not inside it — run `build_session_csv.py` from inside the repo as usual; output CSVs always land in the current directory (the repo root), regardless of `--source-dir`. Locally, the repo's own sibling directories are unrelated course folders, so this correctly finds 0 on the dev machine; the local test sessions live in `sessions/` instead and must be run individually by path, not via `all`.
- `--source-dir` can point anywhere, including a NAS mount (`--source-dir /mnt/NAS/salmo-trutta/traces`) — avoids a manual `cp` into the home directory before every run, which is how the first 500-directory batch was originally done on the Linux box. `--batch-index`/`--batch-size` (default 500) slice the sorted directory listing into fixed-size chunks (batch 1 = entries `[0:batch_size)`, batch 2 = `[batch_size:2*batch_size)`, etc.), verified to exactly reproduce the unbatched full listing when concatenated — so a source directory far larger than can be processed in one sitting gets worked through a batch at a time across multiple runs, continuing from wherever the last batch left off.
- `"all"` also parallelizes across session **directories** (not `(directory, window-size)` combinations -- see the single-parse optimization below) using a `ProcessPoolExecutor` (default: one worker per CPU core, `os.cpu_count()`; override with `--workers`). Each worker only computes a session's DataFrames (`compute_session_dataframes()`) -- the actual `to_csv(mode="a")` write always happens back in the main process as results arrive via `as_completed()`, so concurrent workers never write to the same output file at once (verified: running 7 test sessions x 5 window sizes with 4 workers produced zero duplicate `(session_id, window_index)` pairs and zero `up+down≠total` identity violations across all 5 output files, with print order visibly interleaved -- confirming tasks really ran out of directory-sorted order without corrupting anything). This is a CPU-bound parallelization (packet parsing and per-flow state tracking are branchy sequential Python work), not something a GPU helps with -- see the "why not GPU" note below.
- **Single-parse optimization**: parsing the raw pcap and running flow tracking (`window_features._parse_and_track_flows`) is entirely independent of window size -- flow identity, retransmission/out-of-order/RTT detection, and the `youtube_tcp`/`youtube_quic` label decision don't change based on how packets later get bucketed into windows. `build_session_dataframes()` does that expensive work exactly once per session directory and reuses the result across every requested window size (`_bucket_into_windows()` is the cheap, vectorized, window-size-dependent remainder). This applies regardless of parallelism -- even a single explicit directory with `--window-seconds all` benefits. Verified two ways: (1) output is numerically identical to the pre-refactor code (`pd.testing.assert_frame_equal` across all 5 window sizes, both for a single directory and for the full 7-directory `"all"` run); (2) a single real session (Twitch, 490s) at all 5 window sizes dropped from 28.07s to 10.91s wall-clock (2.6x), and the full 7-directory parallel `"all"` run dropped from 169.88s to 66.48s of total CPU-time for numerically identical output (2.5x less aggregate work) -- the wall-clock win was smaller here (35.39s -> 16.69s) only because 7 directories all fit on separate cores of a 32-core box regardless of version; the CPU-time reduction is what will actually matter once a batch has more directories than cores.
- **Controlled/lab-condition sessions are now skipped at extraction time, not mixed in.** This is a policy change from earlier in the project -- originally every directory was processed uniformly regardless of whether it was organic or a throttled/controlled run, and the resulting mix was later split apart post-hoc (see `src/scripts/find_controlled_sessions.py`/`src/scripts/filter_controlled_sessions.py`, which produced the `session_statistics_*_second_window_controlled.csv` files from a ~19,672-directory real batch: 18,957 organic, 715 controlled). Going forward, `compute_session_dataframes()` calls `is_controlled_experiment()` (same detection logic: real condition text like `rate+3000kbps` sitting between the service name and `.run` in `exp.txt`, vs. nothing in between for an organic session) on every directory *before* touching the pcap at all, and raises `ControlledExperimentSkip` if it matches -- caught separately from a real processing failure in `main()`, logged as `skipping <dir> (controlled experiment)`, and counted separately in the final summary (`N/total directories succeeded (M skipped as controlled experiments, K failed)`). Detection is live per directory, not looked up from the static `controlled_session_ids.txt` list, so it stays correct for directories that show up on the NAS after that list was last generated. Verified: a synthetic controlled directory with a deliberately-garbage `dump.pcap` (plain text, would crash immediately if actually parsed) was correctly skipped without ever attempting to read it, while 7 real organic sessions in the same batch processed normally.
- A GPU does **not** help this pipeline, unlike the AutoGluon training step. GPUs accelerate bulk uniform numeric work (matrix multiplies, tree building over large tabular arrays); this pipeline's cost is per-packet branchy logic with a strict sequential dependency per flow (`FlowTracker`/`flow_metrics` must see each flow's packets in time order to track state) -- inherently serial per flow, and not vectorizable into the kind of uniform array math a GPU is good at. The real parallelism axis here is across independent *sessions* (a CPU-core-count problem), which is exactly what the process pool above exploits.
- **`src/` is organized into subfolders by category.** The 6 shared library modules (`pcap_reader.py`, `packet_parser.py`, `flow_tracker.py`, `flow_metrics.py`, `requests_log.py`, `window_features.py`) live in `src/core/`, since every other file imports from them. `build_session_csv.py` (the main CLI entry point `run_all_sessions.sh` references directly) stays at `src/` root, one level above `src/core/`. `find_controlled_sessions.py` ended up in `src/scripts/`, not `src/core/`, even though `build_session_csv.py` and both benchmarking scripts import `is_controlled_experiment()` from it -- it went into `core/` first (reasoning: it's imported by things outside `scripts/`), then got moved to `scripts/` once asked "find controlled sessions can go in scripts btw" -- it's genuinely dual-purpose (both a standalone CLI tool and an imported module), so "does something else import this" turned out to be a narrower rule than "is this fundamentally a one-off script," and the latter won. Everything else, grouped by purpose: `src/scripts/` (`find_controlled_sessions.py`, `filter_controlled_sessions.py`, `strip_application_layer.py` -- one-off utilities, no core-module imports to worry about), `src/progress/` (`check_progress.py`, `check_model_failures.py` -- same, pure stdlib+pandas), `src/benchmarking/` (`benchmark_feature_costs.py`, `benchmark_feature_memory.py`), `src/live_capture/` (`live_capture_pipeline.py`, `replay_pcap_as_live.py`). Every file outside `src/core/`/`src/scripts/` that needs either has `sys.path.insert(0, ...)` line(s) before those imports (exact relative path depends on how many directories deep the file sits): `build_session_csv.py` needs both `src/core/` (for `window_features`) and `src/scripts/` (for `find_controlled_sessions`); the two benchmarking scripts need those same two plus `src/` itself, since they additionally import `build_session_csv.py` directly (which stays there). Invocation just gets the new path prepended, nothing else changes, e.g. `python src/benchmarking/benchmark_feature_costs.py --num-sessions 100`. Verified every moved file still imports and runs correctly post-move (after all three rounds of this reorg), and re-ran the full old-vs-new correctness comparison for `replay_pcap_as_live.py` from its final location (50/50 rows, zero duplicates, all 115 non-app-layer columns byte-identical to `build_session_csv.py`'s own output for the same session) to be sure the moves introduced nothing new. `src/train_autogluon.py` deliberately was NOT moved -- it was actively training on the Linux box when this reorganization happened, and it imports none of the above anyway (just pandas/sklearn/autogluon, reading CSVs directly).
- **`src/core_go/` and `src/live_capture_go/` are a Go port**, added because the professor specified real-time/router deployment code should be Go, not Python. `src/core_go/` ports just the live-relevant parts of `src/core/` field-for-field: `packet.go` (packet_parser.py, using gopacket/layers for Ethernet/IPv4/TCP/UDP decoding instead of hand-rolled byte parsing), `flowtracker.go` (flow_tracker.py, including replicating Python's exact `(ip_str, port_int)` tuple-comparison semantics for canonical-key ordering -- Go string comparison is lexicographic by default, matching Python's, so this translates directly), `flowmetrics.go` (flow_metrics.py, same 32-bit modular sequence arithmetic), `distribution.go` (window_features.py's `_distribution()`, replicating numpy's exact default linear-interpolation percentile algorithm so results match bit-for-bit, not just approximately), `windowfeatures.go` (the direction-stats/window-row construction), `accumulator.go` (`WindowAccumulator`, the shared window-closing state machine both Go tools use), `csv.go` (serialization matching `build_session_csv.py`'s column names/order exactly). `requests_log.py`/`find_controlled_sessions.py` are NOT ported -- no `requests_history.json`/`exp.txt` exist on a live capture, so there's nothing for them to do.
  - `src/live_capture_go/replay/` ports `replay_pcap_as_live.py`: a goroutine paces packets out on a channel at their original relative timing (Go's equivalent of the Python version's thread+queue, except Go has no GIL so this is genuinely safe concurrency, not a workaround), the main goroutine drains the channel and closes windows the moment a packet's timestamp crosses a boundary (same architecture, same two documented simplifications: retransmission/RTT recomputed on each flow's full history-so-far every window close, and a late-arriving RTT sample isn't retroactively added to an already-closed row).
  - `src/live_capture_go/live/` ports `live_capture_pipeline.py`, but architecturally better: no `tcpdump` subprocess or filesystem-polling-for-rotated-files needed, since Go's goroutines let capture and window-processing run genuinely concurrently in one process -- a capture goroutine reads off the interface via `gopacket/pcap` (cgo, needs libpcap) and sends packets on a channel, same accumulator consumes it. Captures at the same 200-byte snaplen as the dataset.
  - Both tools share one `core.WindowAccumulator` (in `accumulator.go`) -- originally duplicated into each tool separately, refactored into a shared type once the replay tool was verified working, so there's no logic duplicated between them.
  - **This is its own Go module** (`src/go.mod`, module name `csc5991`) -- Go commands must be run from inside `src/`, not the project root (`cd src && go build ./...`; `go run ./live_capture_go/replay -pcap ../sessions/<id>/dump.pcap`; `sudo go run ./live_capture_go/live -interface eth0`), since that's where `go.mod` lives and Go resolves everything relative to it.
  - **Verification**: ran the replay tool against the same real test session (`9fa4dd9141aaa60a5f590a2c00bca0ad`, Twitch) the batch pipeline already produces known-correct output for, and did the same rigorous `pd.testing.assert_frame_equal` column-by-column comparison used throughout this project (excluding `label`/`session_id`/the 13 app-layer columns, which are expected to differ by design). Result: 6,499 of 6,500 compared values identical. The one outlier (`up_interarrival_time_min`, window 49, the session's last and sparsest window at just 5 packets) was tracked down by dumping the raw per-packet timestamps Python and Go each parsed for that window side by side -- they differ by ~200-240 nanoseconds per packet, traced to Python/dpkt's direct `seconds + microseconds/1e6` float arithmetic versus Go/gopacket's round-trip through `time.Time` as integer nanoseconds before converting back to float64 seconds, which introduces a few nanoseconds of representation noise. It only became visible at all because the minimum interarrival time in that specific window was computed from just 4 gaps, each already in the single-millisecond range, where this noise floor is proportionally large enough to surface in a strict comparison -- every busier window was unaffected. Confirmed as a floating-point artifact, not a logic bug.
  - Also dramatically faster than the Python version: 0.007-0.026s of real compute time per window vs. Python's 0.2-0.4s+ at the same packet volumes (same test session, same speed multiplier) -- every window stayed comfortably `[OK]` within its real-time budget, where Python was occasionally borderline/falling behind at the same settings.
  - **The live-capture tool was tested for real, not just built and left unverified** (true live capture needs root/a NIC, which blocked testing the Python version earlier in the project) -- while checking that it fails gracefully on a bad interface name, a real interface name (`en0`, this Mac's own) was tried too, and it worked with no `sudo` needed at all (this Mac apparently already grants the current user BPF capture access). It successfully captured genuine live traffic for about 140 real seconds, producing 14 correctly-structured real-time windows (all 130 columns present, request_count=0/app-layer columns all NaN exactly as designed, sensible flow/packet/byte/throughput counts) -- first real end-to-end confirmation of the live-capture path working on actual live traffic, not simulated or replayed data.
- **Output-folder conventions: `benchmarks/` and `live_runs/`.** Every benchmarking/importance
  CSV (`benchmark_feature_costs.py`, `benchmark_feature_memory.py`, `train_autogluon.py`'s
  `feature_importance_<run_tag>.csv`) is written under `benchmarks/`
  (`os.makedirs("benchmarks", exist_ok=True)` right before the write), so these outputs stop
  cluttering the repo root and stay grouped together across repeated runs. Every live-traffic
  CSV -- `live_capture_pipeline.py`/`replay_pcap_as_live.py` and their Go ports
  (`live_capture_go/live`, `live_capture_go/replay`) -- is written under `live_runs/` the same
  way (`os.MkdirAll("live_runs", 0755)` on the Go side). Verified for all 4 Python/Go
  live-capture-family tools by actually running the replay tool (both languages) against the
  real `9fa4dd9141aaa60a5f590a2c00bca0ad` test session and confirming the CSV landed at
  `live_runs/replay_statistics_10_second_window.csv` with correct row counts. Existing
  `.gitignore` glob patterns for these filenames (`live_statistics_*_second_window_*.csv`,
  `replay_statistics_*_second_window.csv`, etc.) already match at any directory depth (no
  leading `/`), so moving the output into a subfolder needed no `.gitignore` changes. An
  explicit `--output`/`-output` still overrides the default path, unchanged.
- **`benchmark_feature_memory.go` is the one exception to "Go tools land wherever CWD is."**
  Its output path is hardcoded to `../benchmarks/...` instead of plain `benchmarks/...`, since
  the whole point of this file is to sit next to the Python memory benchmark's output for direct
  comparison, not produce a separate `src/benchmarks/` copy. Confirmed by running it from `src/`
  (as documented) both before and after this fix: before, it landed at `src/benchmarks/`; after,
  it correctly lands at the repo-root `benchmarks/`, alongside the Python CSV.

## Training results and inference (November 2026)

### The `no_applayer` 10s run

`python src/train_autogluon.py --variant no_applayer` on the full 10s dataset
(`session_statistics_10_second_window_no_applayer.csv`, Linux box/NAS only), presets
`best_quality`, session-aware split with the script defaults (`--test-frac 0.2 --seed 0`).
Model directory: `AutogluonModels/window_10s_19468sessions_no_applayer` on the Linux box,
**70GB** once finished (was 9.6GB mid-training). 218 models; best is `WeightedEnsemble_L3`.
- Held-out test set, 212,184 windows: accuracy 0.98, macro F1 0.99. Per class (P/R/F1): amazon
  1.00/1.00/1.00 (18,411), netflix 0.99/0.97/0.98 (59,413), twitch 1.00/1.00/1.00 (38,598),
  youtube_quic 0.99/0.98/0.98 (30,086), youtube_tcp 0.96/0.99/0.98 (65,676).
- The full stdout, including the test-set leaderboard (pandas-truncated in the log) and per-class
  report, lives in `~/train_no_applayer.log` on the Linux box. The full **validation**
  leaderboard (all 218 models, cheap to compute since it reads stored scores) was saved to
  `~/CSC-5991/leaderboard_no_applayer_val.csv` there.
- Two features are ignored by the predictor as unused: `down_urg_count`, `total_urg_count`.
- **Permutation feature importance was still running** as of 2026-11-14 (110 features, 5,000
  rows, 5 shuffle sets, AutoGluon's estimate 187,058s ≈ 52h). It is slow because every shuffle
  scores the full L3 stack. It writes `benchmarks/feature_importance_10s_19468sessions_no_applayer.csv`
  when done. Don't start new training runs on the Linux box until it finishes -- they would compete
  for the same CPUs.

### Why a single model is deployed, not the ensemble

Validation accuracy (choose models on **validation**, never test -- picking by test score and
then reporting that test score is data snooping):

| model | score_val | pred_time_val (s) |
|---|---|---|
| WeightedEnsemble_L3 | 0.985771 | -- |
| WeightedEnsemble_L2 | 0.984805 | -- |
| LightGBM_r131_BAG_L1 (best L1) | 0.984623 | 245.0 |
| LightGBM_r15_BAG_L1 | 0.984592 | 229.5 |
| **XGBoost_r22_BAG_L1 (deployed)** | 0.984536 | 16.0 |
| LightGBM_r130_BAG_L1 | 0.984491 | 23.1 |

The whole stack buys ~0.11 points over the best single model. The top 15 L1 models are within
~0.03 points of each other (noise) but differ ~35x in prediction cost. `clone_for_deployment()`
on the L3 ensemble only got it from 70GB to **50GB**, because every L2 stacker takes all ~75 L1
models' outputs as input, so nearly all of L1 has to be kept. Selection: among the top 15 L1
models (all within noise of the best validation score), the best accuracy/speed balance --
`XGBoost_r22_BAG_L1` is 3rd by val accuracy and 4th by prediction time (16.0s). It is **not**
strictly the fastest: `XGBoost_r194_BAG_L1` (7.0s, val 0.984423), `XGBoost_BAG_L1` (11.6s,
0.984410) and `XGBoost_r89_BAG_L1` (14.3s, 0.984504) are faster at slightly lower val accuracy,
and any of them would be equally defensible given the noise. 6 of the 7 fastest top-15 models are
XGBoost. Full 218-model leaderboard: `~/CSC-5991/leaderboard_no_applayer_val.csv` (Linux box).
Cloned with
`p.clone_for_deployment(path=..., model='XGBoost_r22_BAG_L1')` to
`AutogluonModels/window_10s_19468sessions_no_applayer_xgb22` (**333MB**: just that model plus
`trainer.pkl`). It was copied to the Mac with `rsync`. `clone_for_deployment` copies the whole
source directory first and then deletes from the copy, so it needs ~source-size free disk
temporarily. The original directory is never modified (the long "Deleting model ..." log only
touches the clone). The 50GB `_deploy` L3 clone still exists on the Linux box and isn't used.

Being a `BAG` model, `XGBoost_r22_BAG_L1` is 8 fold models whose predictions AutoGluon averages.
That mattered for the pure-Go port (now done with an in-house evaluator, `src/live_capture_go/xgbmodel`, not `leaves`): either export all 8 and
average them in Go, or `refit_full` into a single model first.

### Local inference environment

The repo `.venv` is Python 3.14 with no AutoGluon. Inference uses `.venv-infer`
(`python3.13 -m venv .venv-infer`), pinned to the Linux box: `autogluon.tabular==1.6.3
xgboost==3.4.1 scikit-learn==1.9.1 numpy==2.5.4 pandas==2.3.3`. It installs only
`autogluon.tabular`, not `autogluon` (which drags in PyTorch, several GB, and the Mac disk is
~98% full). On the Linux box `pip show autogluon` finds nothing for the same reason -- check the
version with `python -c "import autogluon.tabular as t; print(t.__version__)"`. Loading on the
Mac prints a Python micro-version (3.13.5 vs 3.13.2) and a Linux-vs-Darwin warning; both are
harmless, since predictions came out as expected.

### `src/inference/predict.py`

Loads a `TabularPredictor` once and scores rows. Batch mode: `--input file.csv`. Streaming
mode: no `--input`, reads CSV rows from stdin via `csv.DictReader`. It drops
`session_id`/`window_*`/`label` before scoring and coerces everything else to numeric. Extra
columns (e.g. the 13 app-layer columns a Go row still carries) are ignored by AutoGluon.
- Prints one JSON line per row: `window_index`/`window_start_s`/`window_end_s` (when present),
  `label`, `confidence` (max probability), `probabilities`.
- `--output <csv>` also appends `window_index, window_start_s, window_end_s, predicted_label,
  confidence, prob_<class>...` per row, flushed every row. The header is written only if the file
  is new.
- It calls `predict_proba` once and takes the argmax rather than calling `predict` +
  `predict_proba`, which halves the per-window model cost.
- `autogluon.tabular` is imported lazily inside `main()`, so `--help`/argument errors work without
  AutoGluon installed.

### Go → Python sidecar: `src/live_capture_go/inference/sidecar.go`

The Go tools can't load AutoGluon pickles, so for now Go does capture/parsing/windowing and
Python does only the model. With `-model-dir`, both `live_capture_go/live` and
`live_capture_go/replay`:
- start `predict.py --model-dir <dir> --output <predictions csv>` once, as a child process (the
  model loads once, not per window);
- pipe every closed window's `row.Values()` into its stdin as CSV text, header first. Rows go
  straight through the pipe with no intermediate stats file -- the stats CSV is now skipped in
  this mode unless `-output` is given explicitly. Without `-model-dir`, both tools behave exactly
  as before;
- read its JSON-lines stdout back and print `-> window N predicted: <label> (<conf>)`.
  `predict.py`'s stderr (load progress) passes straight through to the terminal.

Two design points, both deliberate:
- **`Send` never blocks the caller.** Rows go onto a 4,096-row buffered channel drained by a
  writer goroutine. If the live tool's main goroutine blocked on the pipe (e.g. while Python is
  still loading the model at startup), the 1,024-packet capture channel would fill and the kernel
  would drop packets. This is also why predictions in a fast replay print after "replay
  complete": rows queue until the model is loaded.
- **The child gets its own process group (`SysProcAttr{Setpgid: true}`).** Otherwise Ctrl+C's
  SIGINT hits Python too and kills it mid-queue. Instead, Go flushes the final partial window,
  closes stdin, and `Close()` waits for Python to score everything queued and exit on EOF.

Flags: `-model-dir`, `-python` (default `../.venv-infer/bin/python`), `-predict-script` (default
`inference/predict.py`), `-predictions-output` (default
`live_runs/live_predictions_<N>_second_window_<timestamp>.csv` for live,
`live_runs/replay_predictions_<N>_second_window.csv` for replay). Every default is relative to
`src/`, the same as the Go module.

### Verification

- **Held-out sample, batch mode:** 3 test sessions per class (15 sessions, 815 windows), chosen
  by reproducing `session_aware_split` (`stratify`, `test_size=0.2`, `random_state=0`) on the
  Linux box → 0.9963 window accuracy, 15/15 sessions correct by majority vote. All 3 errors
  involved `youtube_quic` (1 netflix→quic, 2 quic→tcp). This is a smoke test; the 212k-window test
  report is the number to cite.
- **Replay, all 7 local sessions with `-model-dir`:** every session correct by majority vote --
  twitch 50/50, amazon 49/49, youtube_tcp 74/74 and 49/49, youtube_quic 62/62, netflix 61/61, and
  `lab08_netflix` 44 netflix + 5 youtube_tcp. The predictions file has exactly one row per window.
  These local sessions may be in the training set, so this verifies the pipeline, not
  generalization.
- **Live on the Mac's `en0`, no `sudo`:** with YouTube playing in Chrome, 11 of 13 windows were
  labeled YouTube (8 `youtube_quic`, 3 `youtube_tcp`). Both misses were `netflix` at low
  confidence (0.56, 0.45), and every window at ≥0.85 confidence was YouTube. This was 2026
  traffic against a model trained on 2018-2019 sessions. With nothing streaming, idle traffic gets
  labels at ~0.5 confidence: the model is **closed-world** and always picks one of the 5 classes,
  so low confidence is the only signal that a window isn't one of them. The capture also sees the
  whole interface, so other apps' traffic mixes into each window's features.
- **Ctrl+C:** the last partial window still got a prediction, and no `predict.py` process was left
  running.

## XGBoost feature ablation and final models (November 2026)

### Feature importance (AutoGluon, `no_applayer` 10s)

- Computed by `predictor.feature_importance(test_df)`, so it covers the full `WeightedEnsemble_L3`
  stack, not any single model. It ran for about 52 h.
- The run began before the `benchmarks/` convention, so the output landed in the Linux box's repo
  root and was then copied to `benchmarks/feature_importance_10s_19468sessions_no_applayer.csv`.
- Only 8 of 110 features are significant on their own: `rtt_ms_min`, `down_recv_window_{p75,min,max,p25}`,
  `frac_udp`, `rtt_ms_max`, `num_continuing_flows`.
- The other 102 are near zero because they are **redundant with each other**, not useless. Their
  importances sum to 0.025 against 0.98 accuracy.
- The top features are server/path fingerprints. That was an early warning of shortcut risk,
  confirmed live later (see "Fingerprint features" below).

### `src/train_xgboost_ablation.py`

Trains plain single XGBoost models (`tree_method=hist`, `max_depth=8`, `learning_rate=0.1`, up to 2,000
rounds, early stopping 50).

- **Splits.** Test is the same held-out sessions as `train_autogluon.py` (stratified, `--test-frac 0.2`,
  `--seed 0`). Validation is 15 % of the training sessions. **Every decision is made on validation**:
  early stopping, RFE ranking, recommendations. Test is reported only.
- **Fixed experiments.** `all`, `no_total`, `shape_only`, `infra_only`, `streaming_only` (no
  median/p25/p75), and `no_total_streaming`.
- **RFE.** `--rfe-start <fixed set>` and `--rfe-unit feature|family`. At each step it measures
  permutation importance on 20k validation rows (3 shuffles) and drops the weakest 20 % of units.
  `family` shuffles a whole distribution family together.
- **Recommendations.** Each RFE run prints two: the fewest features, and the cheapest on a router
  (fewest buffered families). Both are within `--tolerance 0.001` of the best validation accuracy
  of any step.
- **Router cost columns** (`dist_families`, `buffered_families`):
  - Cost is per distribution **family**, not per column.
  - min/max/mean update in O(1).
  - median/p25/p75 need the window's values kept in a buffer, and one buffer serves all 6 stats of
    its family.
  - Older CSV rows are backfilled from their `features` column.
- **`--final <feature list file>`** (one name per line, `#` comments allowed, order kept):
  1. stage 1 fits on the fit split, with early stopping on validation;
  2. stage 2 refits from scratch on fit + validation, with rounds scaled by
     `(n_fit + n_val) / n_fit` (skip with `--no-refit`);
  3. scores test once.

  It writes `XGBoostModels/<run>/final_<name>.json`, `final_<name>.features.json` (the input column
  order the model expects) and `classes.json`, and adds a `final_<name>` row.
- **Outputs.** Results go to `benchmarks/xgb_ablation_<N>s_<variant>.csv` (one row per model). Models go
  to `XGBoostModels/<N>s_<variant>/` (gitignored; on the Linux box, rsync what you need).
- **Practical notes:**
  - stdout is line-buffered, so `| tee` logs show progress.
  - `--n-jobs` caps threads. The Linux box is shared, so pin with `taskset -c <cores>` and keep to about
    10 cores.
  - A low-information feature set can run to the 2,000-round cap (10,000 trees: slow on the router and a
    huge JSON file). Use `--learning-rate 0.3 --max-rounds 600` for those.

### Results (`benchmarks/feature_selection_results.md`, feature lists in `benchmarks/feature_sets/`)

| Set | Features | Buffers | Test acc |
|---|---:|---:|---:|
| all | 112 | 13 | 0.9827 |
| no_total | 78 | 9 | 0.9824 |
| no_total_streaming (option B) | 51 | 0 | 0.9810 |
| **option C** (streaming RFE step 4) → `final_option_c` | 22 | 0 | 0.9799 → **0.9801** |
| option D (streaming RFE step 9) | 8 | 0 | 0.9705 |
| `final_option_c_no_fingerprint` | 12 | 0 | 0.9530 |
| `final_shape_streaming` | 28 | 0 | 0.9638 |

The first RFE run's recommended 47-feature set still needed all 13 families (10 buffered), which is why
the counting moved to buffers.

### Model selection corrections

- `XGBoost_r22_BAG_L1` was the best accuracy/speed balance among the top 15 L1 models (3rd most
  accurate, 4th fastest), not the cheapest. The full leaderboard with ranks is in
  `benchmarks/model_leaderboard_10s_19468sessions_no_applayer.csv`.
- For a `_BAG_` model, `pred_time_val` is out-of-fold: each window is predicted by **one** of the
  8 children. Real per-window inference runs all 8, so it costs about **8x** the leaderboard's
  per-window figure.
- **AutoGluon's internal bagging splits by row, not session**, so `score_val` is optimistic. Test
  sessions were fully held out, so test results are unaffected. Pass session IDs as `groups` in
  future runs.

## Router deployment and live evaluation (November 2026)

### The router

- Hardware: OpenWrt 25.12.6, MediaTek Filogic, aarch64 Cortex-A53, ~986 MB RAM, no swap, 6.8 GB free
  on `/`.
- No Python, gcc or git. `tcpdump` and `libpcap` exist. Packages install with `apk`.
- Reached as `root@192.168.1.1` from the Linux box (key login via `/etc/dropbear/authorized_keys`), and
  from the Mac as `prs-router` (`ProxyJump prs` in `~/.ssh/config`). The router's password must never go
  into the repo.
- WAN is `eth1`. The LAN ports `lan1`–`lan5` are DSA ports on `eth0`, bridged as `br-lan`.
- Capture on `lan2`. The Linux box (192.168.1.188) uses the router as its default gateway.
- **GRO must be off for valid features** (approved by the professor):
  `ethtool -K eth1 gro off; ethtool -K lan2 gro off`. **It reverts on reboot**, so check with
  `ethtool -k eth1 | grep generic-receive`. With GRO on, the kernel merges packets into up to 49 KB
  super-packets before capture. Wire frames and the training data never exceed 1,514 B.
- Flow offloading (`flow_offloading`, `flow_offloading_hw`) is off.

### Go-native inference: `src/live_capture_go/xgbmodel/`, `inference/native.go`

- `xgbmodel.Load` reads XGBoost 3.x JSON: `gbtree`, `multi:softprob`, a per-class `base_score`
  vector, `tree_info`, `iteration_indptr`, and numerical splits only (categorical splits are
  rejected). It mirrors the CPU predictor:
  - inputs cast to float32;
  - `x < threshold` goes left; NaN follows `default_left`;
  - float32 margin accumulation and a float32 softmax;
  - `best_iteration` honored, so ablation models work.
- Nodes are flattened to 16 B each. `LoadClassifier` adds `classes.json` and cross-checks
  `<name>.features.json` (optional; the model JSON embeds `feature_names`).
- **Verified:** labels agree with xgboost's `predict_proba` on 28,463 / 28,463 rows, with a largest
  probability difference of 2.4e-7, including 2,888 rows with NaNs. This held for a `--final` model
  and an early-stopped ablation model.
- `inference.StartLabeler` chooses the scorer: a `.json` `-model-dir` is scored by **Native**
  (in-process, synchronous, no Python); anything else goes to the AutoGluon sidecar. Native writes the
  same predictions CSV format as `predict.py`. `predict.py` writes `\n` line endings, so the two files
  are byte-identical.
- `src/live_capture_go/xgbeval` scores a stats CSV in Go, for checking against Python or batch-scoring
  on the router.
- **Scoring cost follows the number of trees** (rounds × 5 classes), not features: about 270 µs per
  window on the Mac, and about 1.1–1.2 µs per tree on the router.
- **Startup peak memory is parsing the JSON**, at about 4x the file: 28 MB → 110 MB, 48 MB → 203 MB.
  `debug.FreeOSMemory()` runs after load. A compact binary model format would remove the peak.

### Capture without cgo, and `deploy_router.sh`

- `live/capture_pcap.go` (`cgo || !linux`) uses libpcap.
- `live/capture_linux_nocgo.go` (`linux && !cgo`) uses `pcapgo.EthernetHandle`: an AF_PACKET raw
  socket, promiscuous, `SetCaptureLength(200)`, with the true length from `PACKET_AUXDATA`.
- So `CGO_ENABLED=0 GOOS=linux GOARCH=arm64` builds a **static 3.6 MB binary**.
- `deploy_router.sh` (repo root, Mac):
  - builds `build/live-router` (gitignored);
  - streams files over plain ssh (`cat > tmp && mv`), not scp, because dropbear may lack sftp, and the
    rename avoids "Text file busy" while an instance runs;
  - uses one ControlMaster connection, so there's one password prompt (control socket in `/tmp`, since
    macOS limits socket paths to 104 chars);
  - takes `MODEL=<json>`, default `final_option_c`; each model lands in `/root/csc5991/model/` under its
    own name;
  - doesn't require `.features.json`.
- zsh on the Mac expands `*` locally: quote remote globs in `rsync`/`scp`.

### `live-router` flags (`src/live_capture_go/live`)

- `-model-dir` (`.json` → Native).
- `-all-features`: compute every column instead of only the model's.
- `-port-direction` (default **true** in live, **false** in replay).
- `-perf-output <csv>`.
- `-max-duration <dur>`: stop as if on Ctrl+C. The router has no `timeout` command. An orphaned
  instance once ran for 5.8 h and peaked at **462 MB** RSS.
- `-output <stats csv>` and `-predictions-output`.
- `-exclude-own-traffic`: drop the router's own packets (its SSH sessions leave as GSO super-packets).
- **Priority** (`live/priority.go`, `live/flows.go`):
  - `-priority`: decide which connections to prioritize, print `▶ PRIORITY ON` / `■ PRIORITY OFF`,
    log events to `-priority-log` (no IPs). A dry run unless `-priority-enforce`.
  - `-priority-by flow` (default): each connection keeps its own accumulator on the main window clock
    (`SelectiveAccumulator.AdvanceTo`/`StartAt`) and is scored on its own packets. `window` = the old
    rule (tag the heaviest connections by the window's label).
  - Tag when a connection's **own** label is the same service at ≥ `-priority-min-confidence` (0.93),
    `-priority-streak` (5) windows in a row, with ≥ `-priority-min-packets` (100) per window, on server
    port 443/80 with the label's transport (UDP for `youtube_quic`, TCP otherwise). An unconfident or
    different label resets the streak; a quiet window doesn't. Release on FIN/RST or `-priority-idle`
    (2 min).
  - `-priority-max-mbps` (25): a window above this rate resets a connection's streak (bulk downloads
    on fast lines; 0 = off). Only gates tagging; a tagged connection isn't released for speeding up.
  - `-flow-log <csv>`: every scored connection's own label per window (flow hash, protocol, server
    port, packets, bytes; no IPs).
  - `-priority-enforce`: `nft add element inet csc5991 video_flows {...}` on ON, refresh every window,
    `destroy element` on OFF, `flush set` on exit. Exits at startup if `router/qos.sh up` hasn't been
    run.

### QoS setup: `router/qos.sh` (runs on the router; test runs only, professor-approved)

- `sh /root/csc5991/qos.sh up|down|status`; `deploy_router.sh` copies it. `RATE` (default `100Mbit`)
  and `IFACE` (default `lan2`) override.
- `up`: installs `kmod-sched-cake` if missing, uses the router's `tc-tiny` (`/usr/libexec/tc-tiny`;
  `tc-full` conflicts with it, so it is never installed automatically), puts `cake bandwidth $RATE
  diffserv4 flowblind` on `lan2` egress, and creates table `inet csc5991` (set `video_flows`: client
  IP . proto . client port . server IP . server port, 3-min timeout; a forward/mangle rule setting
  DSCP AF41 on server→client packets in the set). Rerunning `up` with a new `RATE` replaces the qdisc
  in place.
- `down`: removes the qdisc and the table, nothing else. Run it after every test session.
- Why lan2 egress: ingress shaping runs before netfilter and would never see the marks. Why
  `flowblind`: per-connection fairness alone would protect a light video flow.

### Selective feature extraction: `core_go/selective.go`

- `core.Accumulator` is the interface. `NewAccumulatorFor(features, ...)` returns `WindowAccumulator`
  (all features, the original) for `nil`, otherwise `SelectiveAccumulator`. The default with a Native
  model is the model's own features.
- **No per-window packet buffer:** each packet updates counters and is dropped.
- **Distribution families:** if only min/max/mean are needed, keep running min/max/sum/count; keep a
  full buffer only when a median/p25/p75 is needed (inter-arrival keeps timestamps and sorts them,
  exactly like the original); skip the family when unused.
- **Retransmission/out-of-order/RTT:** advanced incrementally per flow (one iteration of
  `ComputeFlowMetrics` per packet, against persistent state), instead of storing each flow's full
  history and replaying it at every window close. Skipped entirely when the model uses none of these
  features.
- **Always-on diagnostic columns** (O(1), never buffered): `total_packet_count`, `frac_udp`,
  `up/down_packet_size_max`.
- `WindowRow.Values()` blanks uncomputed columns (`only` mask).
- `RetainedState()` reports per-flow entries: stored packets (old) or seen sequence numbers +
  unacknowledged segments (selective; 0 without sequence features).
- **Verified identical** to `WindowAccumulator`: every feature value (relative difference 0.0, no NaN
  mismatches) and every label, across 4 models × 7 sessions (394 windows each).
- Cost, streamed as on the router (Mac; Twitch, YouTube-TCP and YouTube-QUIC sessions):

  | Extraction | CPU per packet | Memory held at end |
  |---|---|---|
  | Old (all features) | 0.76–3.77 µs | 25–57 MB, growing |
  | Option C (22) | 0.22–0.57 µs | 0.1–10.7 MB |
  | Option D (8) | 0.16–0.17 µs | 0.02–0.06 MB |

### Flow direction (`core_go/flowtracker.go`, `DirectionByServicePort`)

- The default first-packet rule (`flow_tracker.py`): for UDP, and for TCP with no SYN seen, the
  client is whoever sent the flow's first seen packet. One router run reversed a QUIC video flow
  (47.5 MB "uploaded", 0.4 MB down), mirroring every directional feature and dropping accuracy to 0.42.
- With `DirectionByServicePort`, a flow first seen **from** a port below 1024 **to** an ephemeral port
  is assigned server-to-client. A pure SYN still wins.
- On recorded sessions, turning it on changes 3/7 sessions. The dataset also has first-packet
  reversals from mid-connection capture starts, and the models learned those, so it lowers option C
  0.878 → 0.816 on `lab08_netflix`. That's why replay keeps it off.
- To make training and deployment consistent: port the rule to `flow_tracker.py`, regenerate the
  dataset, and retrain.

### Per-window performance: `live/perf.go`, `-perf-output`

- Columns: `packets_in_window`, `packet_handling_us`, `window_close_ms`, `inference_us` (Native only),
  `cpu_percent` (process CPU / wall since the last window), `heap_alloc_mb`, `heap_sys_mb`, `rss_mb`
  (Linux `/proc`), `max_rss_mb`, `flows_retained`, `packets_retained`.
- `cpu_percent` is **blank for windows closed within 1 s of the previous one**. Several windows closing
  after a traffic gap used to produce fake 100 % readings.

### Ground-truth eval: `src/live_capture/stream_and_check.py` (runs on the Linux box)

- **Flow of one run:**
  1. start `live-router` over ssh (nohup if present; `-max-duration` = `--minutes` + 5 min);
  2. play the URL in headless Chromium (`/usr/bin/chromium` + chromedriver 156, `pip install selenium`);
  3. log playback every 5 s;
  4. stop the router process and copy back the predictions, stats and perf files;
  5. score them.
- **Chromium setup:**
  - `--disable-background-networking` and related flags (a fresh profile otherwise downloads megabytes
    of components);
  - a normal Chrome user agent with `navigator.webdriver` hidden (YouTube otherwise kills headless
    playback about 30 s in);
  - `--disable-quic` forces TCP.
- **Stall handling:**
  - when the position stops advancing for 15 s, save `_stallN.png/.txt`, click through gates/skip, and
    reload after 30 s (at most 3 times);
  - playback that never starts saves `_nostart.png/.txt`;
  - YouTube's own "Something went wrong" player error at about 0:40 is common, and recovered by reload.
- **Scoring:**
  - all windows after playback start, minus 1 warm-up window and the final partial window;
  - *active* windows: ≥ `--min-packets` 100;
  - *playing* windows: the timeline shows the video advancing, **the number that measures the classifier**;
  - a flag on any packet over 1,514 B (the GRO check).
- **QoS experiment flags:** `--enforce` (passes `-priority-enforce`), `--priority-by flow|window`,
  `--competing-downloads N` (bulk `curl` downloads, round-robin over `--competing-url`'s
  comma-separated list, default 6 speed-test servers: one server allowed only 2 parallel downloads;
  each download restarts if dropped or stalled 15 s; a `downloads: X of N connected` line every 30 s
  via `ss`, and a per-server summary at the end; 16 downloads fill the ~570 Mbit line), `--light-traffic N`
  (N loops of small requests, 1–5 s apart), `--competing-after S` (seconds after playback start,
  default 60). The timeline adds `buffer_ahead_s` and `competing`; the summary adds `qoe_*` columns
  (not-playing seconds, buffer ahead mean/min, resolution) over the load period. Per-window
  `connections in window N` lines show each busy connection's own label, from `_flows.csv`.
- Expected labels: youtube → `youtube_quic`, or `youtube_tcp` with `--disable-quic`; twitch → `twitch`.
  `--service idle` gives a baseline. Netflix and Amazon need logins and DRM, so they can't be tested
  live.
- **Outputs:** `live_runs/eval_<tag>{,_stats,_perf,_timeline,_scored}.csv`, and
  `live_runs/eval_summary.csv` (union-of-columns append; `--rebuild-summary` recreates it from the run
  files). `--score <predictions> [--stats] [--timeline]` re-scores offline.
- Verified sources used: `https://www.youtube.com/watch?v=eRsGyueVLvQ` (Sintel) and
  `https://www.twitch.tv/tfue` (must be live).

### Live results (write-ups in `live_runs/tests/`; raw files deleted, in git history)

- **YouTube over QUIC.** `final_option_c` scored 1.00 on playing windows in 3 separate runs. The
  15- and 8-feature models were perfect on active windows. The 112-feature model scored 0.31: today's
  QUIC sends about 0.176 upload packets per download packet, vs. 0.505 in 2018–19, so its
  `total_*`/percentile features look like TCP.
- **Fingerprint features.** Live Twitch failed for every original model (0.06–0.38). TCP receive
  window (server stack, client OS), RTT (capture point: 0.058 ms at the router vs. 0.006 ms at the
  client), TCP flag counts (Twitch PSH 787 vs. 21) and retransmission/out-of-order have all shifted
  since the dataset was recorded. QUIC (UDP) has none of them, which is why YouTube was unaffected.

| Model | Features | Offline test | Live Twitch | Live YouTube QUIC | Live YouTube TCP |
|---|---:|:---:|:---:|:---:|:---:|
| `all` | 112 | 0.983 | 0.25 | 0.31 | – |
| `final_option_c` | 22 | 0.980 | 0.19 | 1.00 | – |
| `final_option_c_no_fingerprint` | 12 | 0.953 | 0.69 | 1.00 | 0.88 |
| **`final_shape_streaming`** | 28 | 0.964 | **0.88** | **1.00** | – |

- **Cost on live Twitch** (the heaviest load, about 680 packets/s).
  - `final_shape_streaming`: 1.92 % CPU (≈192 ms per 10 s window), 2.0 µs/packet, 0.062 ms window close,
    2.0 ms scoring, 37 MB RSS at end, 111 MB startup peak, **0 per-flow entries**.
  - The fingerprint models: 4.4 µs/packet, 52–61 MB, and 189k–266k growing entries after 3 min.
  - RTT/retransmission are both the expensive features (they need per-flow sequence tracking) and among
    the fragile ones. Receive window and flags are cheap but drifted.
- **All live numbers are single runs** except option C on YouTube over QUIC.
- **QoS / priority (2026-11-18, `live_runs/tests/qos_priority_results.md`):**
  - Whole-window tagging prioritized 2 bulk downloads the moment they started (window labeled
    `netflix` 0.999), which led to per-connection scoring.
  - Per connection, offline on the 7 local sessions: the main connection alone scored 1.00 for YouTube
    and Netflix, 0.94 Twitch, 0.84 Amazon. Live with light background traffic: whole-window accuracy
    0.22, the video connection's own label 1.00 (54/54).
  - Light traffic: 0 of 317 connections tagged, though 52 % had an own label ≥0.93 — each lasted one
    window, so the streak stopped them.
  - Long bulk HTTPS downloads are still tagged eventually: their own label is ≥0.9 in 31–40 % of
    windows at 20 Mbit, and `youtube_tcp` at 0.98–1.00 when rate-limited at 5 Mbit. 5-at-0.93 was tuned
    on the 20 Mbit runs and failed at 5 Mbit. The model is closed-world.
  - No measured video-quality benefit yet (links uncongested, or a run spoiled before priority acted).
  - Twitch (2026-11-19): tagged correctly unshaped and at 500 Mbit shaping. **Heavy shaping (12 Mbit)
    flips Twitch's own label to `youtube_tcp`:** `cake` spaces packets out, so the client ACKs ~5× more
    often (up/down packets 0.74 vs. 0.19 unshaped, 0.24 at 500 Mbit), and upload features carry 52 % of
    the model's gain. Force congestion with load, not a low `RATE`. Side connections of the Twitch page
    can be tagged (tiny packets reach 100; quiet windows don't reset the streak). Memory 58–61 MB under
    heavy connection churn (stats kept 2 min after idle).
  - 2026-11-21 (Twitch `hasanabi` and YouTube QUIC vs. multi-server downloads): **97 % of the bytes marked
    for the Video tier were downloads** at 540 Mbit (16.3 GB counter, Twitch ~0.5 GB); the 25 Mbit/s cap
    cut download tags 5 → 1 there, but 16 were tagged at 25 Mbit (slow downloads look like video). No
    measured video-quality benefit: the video was never short of bandwidth. 100 Mbit shaping kept
    Twitch's label below 0.93 (up/down 0.52). CPU: ~70 % at ~410 Mbit/s, >100 % at the full line.

## Go memory benchmark: a Python-measurement-artifact catch

`src/benchmarking/benchmark_feature_memory.go` is a Go port of `benchmark_feature_memory.py` --
same stat categories (flow identification, retransmission/RTT state, the per-window distribution
buffers now split by direction, app-layer buffers), same CSV schema
(`stat,avg_bytes,p95_bytes,max_bytes`), written to a SEPARATE file
(`benchmarks/feature_extraction_memory_benchmark_go.csv`) so the two can be compared side by
side rather than one overwriting the other.

Built specifically to resolve a real discrepancy, not as a parity exercise for its own sake:
after adding direction splits (`up_*_buffer`/`down_*_buffer`/`total_*_buffer`) to the Python
benchmark, the down:up ratio came out *inconsistent* across metrics that should behave
identically -- `payload_size_buffer`/`packet_size_buffer` showed ~8x, while
`recv_window_buffer`/`interarrival_time_buffer` showed only ~2x, even though all four are
measuring the same underlying packets per direction per window. The likely cause: CPython caches
small integers (-5 to 256) as process-wide singletons, and `up` traffic is mostly bare ACKs with
`payload_size == 0` -- so a long run of repeated `0`s in a Python list costs almost nothing
beyond the first occurrence (`deep_sizeof`'s `seen`-based cycle detection only charges a given
object id once), understating exactly the buffers dominated by that value. `recv_window`/
`interarrival_time` values aren't nearly as often exactly `0`, so they didn't get this same
discount.

Since the actual deployment target is Go, not Python, and Go gives no such singleton-caching
discount (every slice element costs its real, fixed size regardless of value), this mattered
enough to check directly rather than just noting it as a caveat. `core_go`'s `ComputeFlowMetrics`
doesn't expose its internal per-direction bookkeeping state (it only returns final
retransmission/RTT results), so `flowStateSize()` in this file duplicates that bookkeeping loop
locally purely to get at the state for sizing -- same reasoning as the Python benchmark's own
`_flow_state_sizes` duplicating `flow_metrics.py`'s loop for the same reason, not a shortcut
unique to the Go side. Go has no general "deep sizeof" API the way Python's `sys.getsizeof` is,
so this file uses hand-rolled, documented-as-approximate size formulas for Go's own memory shapes
(slice header + backing array; a commonly-cited ~48-bytes/entry rough figure for small-key map
overhead) -- approximate, not byte-exact, the same honest caveat `deep_sizeof` itself carries.

**Result, verified at real 100-session scale**: all four direction-split metrics agreed on a
consistent ~2x down:up ratio in the Go version, confirming the Python benchmark's 8x figure for
`payload_size`/`packet_size` was a measurement artifact of int caching, not a real traffic
asymmetry -- the true down:up asymmetry for this traffic is closer to 2x across the board.
Independently (not an artifact either way, a genuine language-level difference): Go's flat
`FlowState` struct costs ~74 bytes per flow vs. Python's ~520 bytes, and
`retransmission_rtt_state` (total per session) averaged ~8.9MB in Go vs. ~14.7MB in Python (same
shape -- dominated by a few long-lived flows, never pruned -- just leaner per-entry overhead in
Go's maps than Python's dict+set).
- `--window-seconds all` → runs the full pipeline at `(10, 8, 4, 2, 1)` seconds, each to its own correctly-named file. Verified against one session: window counts for 490.28s of traffic came out as 50/62/123/246/491 for 10/8/4/2/1s respectively — matches `floor(duration / window_seconds) + 1` exactly.
- Both combined processes the full cross-product (every directory × every window size). Verified: 3 directories × 1s windows correctly appended into one shared file (736 + 605 + 491 = 1,832 rows, matching exactly).
- `--output` is rejected if more than one directory or window size is being processed (only meaningful for the single-run case).
- Each (directory, window-size) run is wrapped in try/except — one failing session doesn't abort the whole batch; failures are collected and printed in a summary at the end, not silently swallowed.

## The data source

`traffic.pcapng` (485 MB, ~3.39M packets, pcapng format) sits in the project root. Confirmed by direct parsing, not assumed:

- Every packet carries a frame comment of the form `<session_id>,<label>`. Four labels exist: `youtube` (1,244,646 packets / 9,410 sessions), `netflix` (1,048,000 / 6,253), `twitch` (689,230 / 3,352), `amazon` (408,977 / 1,869). No packet lacks a comment.
- The whole file is TCP. There is no UDP/QUIC traffic anywhere in it, so there is no `youtube-quic` class to extract from this capture — `youtube` stays one class, confirmed with the professor directly. Don't reintroduce a QUIC/TCP split unless a new capture actually contains QUIC traffic.
- Packets are header-only: ~73-byte average size, ~200-byte inferred snap length. There is no payload to inspect, which keeps this consistent with the project's no-DPI premise — application-layer features must come from TCP segment lengths/timing, never from reading payload bytes.
- Absolute packet timestamps are bogus (e.g. "Jun 9, 209864" — a synthetic-generation artifact), but relative inter-packet timing within a session is sane (tens-of-microseconds deltas) and safe to use for durations, inter-arrivals, and RTT. Never treat the absolute timestamp as a real calendar date; only use elapsed/relative time.
- `capinfos` reports the whole file as not strictly time-ordered — that's sessions being interleaved in one merged capture, not corruption; packets within a single session's own comment group were checked and are chronologically sane.

## Architecture: sessions, flows, two output tables

Per the professor directly (do not deviate from this without asking): **no time-windowing**. Each unit gets exactly one feature vector over its *entire* duration, not sliced into 1-2 second windows.

- **Session** = every packet sharing the same frame-comment session ID. A session can, and usually does, span multiple simultaneous flows (e.g. parallel connections to different CDN edges).
- **Flow** = one TCP connection within a session, identified by the 4-tuple (client IP, client port, server IP, server port), treated bidirectionally — the SYN sender is the client/upstream side for "up"/"down" direction purposes.
- **Flow-level CSV**: one row per `(session_id, flow 4-tuple)`, with the full feature table computed over that flow's whole lifetime.
- **Session-level CSV**: one row per session. Some features are inherently session-scoped (`# of parallel flows` — how many flows were concurrently active, not a per-flow stat). Everything else is derived by rolling the flow-level rows up: for each numeric flow feature, report the distribution across that session's flows — median, 25th percentile, 75th percentile at minimum (the professor's own example: "median up bytes, 75th percentile up bytes, 25th percentile up bytes"). Include the flow count itself as a session feature.

## Feature table (from the professor)

**Network layer**: throughput up/down (total, video, non-video), throughput-down difference, packet count up/down, byte count up/down, packet inter-arrivals up/down, # of parallel flows (session-level only, see above).

**Transport layer**: # flags up/down (ACK/SYN/RST/PUSH/URG), receive window size up/down, idle time up/down, goodput up/down, bytes per packet up/down, round trip time, bytes in flight up/down, # retransmissions up/down, # packets out of order up/down.

**Application layer**: segment sizes (all previous, last-10, cumulative), segment request inter-arrivals, segment completion inter-arrivals, # of pending requests, # of downloaded segments, # of requested segments. No payload exists, so "segments" here means inferred TCP data bursts (request/response-shaped traffic bursts), not real HTTP/QUIC stream framing — document this inference method wherever it's implemented, don't present it as ground truth.

Verified derivable via tshark's own TCP analysis fields: RTT (`tcp.analysis.ack_rtt`, populated on ~40% of packets — every handshake/ACK pair), retransmissions (`tcp.analysis.retransmission`, ~18% of packets flagged). Verified NOT reliably derivable from tshark's built-in fields: `tcp.analysis.bytes_in_flight` is populated on essentially none of the packets (2 out of 3.39M) — compute it manually per flow (max sent sequence minus max acked sequence over time) instead of trusting that field. `tcp.analysis.out_of_order` reads exactly zero across the entire file; treat that as an open question (genuinely absent in this synthetic dataset, or an artifact of session interleaving) rather than asserting either explanation as fact.

The ~18% retransmission rate is unusually high for real-world traffic. Note it as an observed property of this dataset when reporting results — don't silently normalize it away, and don't claim it's a bug without more evidence either.

Session IDs are up to 20 digits, past int64 range. `build_dataset.py` writes them as exact text, but a plain `pd.read_csv(...)` on the output CSVs will silently round them through float64 and corrupt them (verified: this happens even though the file itself is correct). Always load these CSVs with `dtype={"session_id": str}`.

A flow is identified by a 4-tuple within one session; if a session happens to reuse the same client port for two genuinely separate connections hours apart, they get merged into a single flow, producing a nonsensical multi-hour "duration" and RTT for that row. Confirmed present in `traffic.pcapng` (rare, one observed case). Known and documented in `flow_builder.py`, not fixed — would need a real gap-based flow-splitting heuristic if it turns out to matter at scale.

## Tooling

The professor suggested `dpkt` and `scapy`. In practice, `tshark` (already installed, paired with Wireshark) reads this entire 3.39M-packet file's fields — including frame comments and TCP analysis fields — in under a minute per pass, and its dissectors already implement RTT/retransmission detection correctly for header-only captures. The practical pipeline: use `tshark` to export the needed per-packet fields to a flat file, then use Python (pandas) for session/flow grouping and the aggregate statistics. Reach for `dpkt`/`scapy` for anything tshark's field export can't give directly (e.g. `bytes_in_flight`, segment-burst inference) rather than re-deriving everything tshark already computes correctly.

## The real dataset: all_traffic_time_10.pkl

As of October 2026 the professor provided the actual dataset: `all_traffic_time_10.pkl` (1.5 GB, a Python-2-pickled pandas DataFrame — load with `pickle.load(f, encoding="latin1")`, not plain `pd.read_pickle`, which raises `UnicodeDecodeError`). This supersedes `traffic.pcapng` as the real data to work with; the pcapng pipeline (`src/pcapng_reader.py` etc.) stays as reference/working code but doesn't apply to this file, which is **already fully feature-engineered**, not raw packets.

Verified structure: 728,992 rows x 251 columns. Each row is one **10-second time window** within a session (`relative_timestamp` steps 10, 20, 30, ... — this is what "time_10" in the filename and the `10_` column prefix mean: a 10-second window, not "last 10 chunks"). 13,765 unique sessions (`session_id` == `deployment_session_id`, always), 536 homes (`home_id`), 4,227 distinct videos (`video_id`). Rows per session range 1-5,443, mean ~53.

`service` is the label column: `youtube` (317,135), `netflix` (200,688), `twitch` (122,277), `amazon` (88,892) — same four services as `traffic.pcapng`. Unlike `traffic.pcapng`, **this file does have a real YouTube-QUIC split**: `is_tcp == False` on 82,162 of YouTube's rows (~26%), 394 of Netflix's (negligible), 0 for Amazon/Twitch. The original 5-class ask (YouTube-QUIC, YouTube-TCP, Netflix, Amazon, Twitch) is achievable from this file if wanted — revisit the "YouTube stays one class" rule above specifically for this dataset, since it was decided for `traffic.pcapng`'s data, which didn't have the option.

Column names map closely onto the professor's original feature table (`serverByteCount`/`userByteCount`, `userAvgRTT`/`serverAvgRetransmit`, `10_chunksizes_50/75/85/90` + `R` variants, `allprev_chunksizes_*`, `cumsum_chunksizes`, `n_chunks_down`/`n_chunks_up`, `parallel_flows`, `up_chunk_iat_*`/`down_chunk_iat_*`), plus real QoE ground truth not in the original table (`quality`: bad/good/unknown, `resolution`, `bitrate`, `startup_time`, `c_rebufferings`, `c_bitrate_switches`, `ads`). `index` is a dead column, always `0` across every row — drop it, don't treat it as a real sequence index. Only `avg_flow_age` has nulls (~8% of rows); every other column is fully populated.

`10_EWMA_chunksizes` is the one column that isn't a scalar — it's a 10-element array per row (the EWMA of chunk sizes across the window), which would otherwise serialize to CSV as a bracketed, multi-line text blob. `src/export_streaming_features.py` expands it into `10_EWMA_chunksizes_1..10`; missing positions (rows with fewer than 10 chunks, or none at all) are left as `NaN`, not fabricated as `0`.

`session_id` comes in two genuinely different forms, confirmed by pattern and by `home_id`/`video_id` behavior: a 32-char hex hash for real-world/crowdsourced sessions (13,081 of them, each tied to one of 536 real `home_id`s), and a descriptive string encoding deliberately-shaped network conditions for controlled lab experiments (684 of them, e.g. `netflix-0_rate+20mbps+loss+0.01%+25%.run3`, `youtube-0_10400-30_3900-120_3900-240_8500-180_1600`). `label`/`service`/`home_id`/`video_id` are verified constant within every session, real or lab. `src/aggregate_sessions.py` keeps these two session types in separate output files rather than mixing organic and artificially-shaped traffic together.

True per-flow rollup (the professor's original ask: full-duration stats per flow, then median/p25/p75 across a session's flows) is not reconstructable from this dataset. Verified: a row with `parallel_flows == 1` (exactly one flow active) still shows `userMinRwnd != userMaxRwnd` (2326 vs 3726) — if these were genuinely cross-flow statistics, one flow would force them identical. They're actually per-packet statistics within each 10-second window, blending every packet regardless of which flow it came from; individual flow identity was discarded when this dataset was built. `parallel_flows` only gives the count of concurrently active flows, never their individual values.

What `src/aggregate_sessions.py` rolls up instead is the real available granularity — 10-second time-windows within a session — using three rules per column, chosen from verified column behavior, not naming conventions (naming was actively misleading in testing: `c_rebufferings`/`n_chunks_down` sound cumulative but are actually per-window counts):
- **Cumulative** (take the last window's value): only 7 columns pass both "never meaningfully decreases" and "real net increase from first to last window" — `cumsum_chunksizes`, `n_prev_down_chunk`, `n_prev_up_chunk`, `video_position`, `allprev_max_chunksize`, `all_prev_down_chunk_iat_max`, `all_prev_up_chunk_iat_max`.
- **Count** (sum across windows): per-window event/byte/packet/flag counts, where the session total is the meaningful number — byte/packet counts, the six TCP flag counts (both directions), out-of-order counts, chunk counts, bitrate/resolution-switch and rebuffering counts, retransmit-bucket counts.
- **Distribution** (median/p25/p75 across windows): everything else — RTT, window size, inter-arrival time, bytes in flight, chunk sizes, throughput. Same statistical idea the professor described for flows, applied to windows instead since that's what's actually available.

## Maintenance

`dataset-collection/` holds the entire previous Selenium-based collection phase (its own `AGENTS.md`/`CLAUDE.md` inside describe that phase) — reference only, not active work. Don't resurrect or run anything from it without being asked.

Every claim about this dataset in this file was verified by actually parsing `traffic.pcapng` with `tshark`, not assumed from the professor's description alone — keep that habit. When a new fact is learned about the data (a field's real coverage, a label's real count, a timing quirk), verify it against the file directly and update this document rather than trusting an earlier assumption.
