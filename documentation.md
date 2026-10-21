# Project Documentation

A chronological log of major steps, decisions, and results for this thesis
(lightweight ML classification of streaming traffic without deep packet
inspection). See `AGENTS.md`/`CLAUDE.md` for the technical architecture
reference — this file is the narrative: what we did, in what order, and
what we found.

---

## Step 1: Building the feature-extraction pipeline

Starting point: real per-session capture directories (`dump.pcap`,
`exp.txt`, optionally `requests_history.json`), one directory per browsing
session, across four streaming services (YouTube, Netflix, Twitch, Amazon).

The pipeline (`src/core/pcap_reader.py` → `src/core/packet_parser.py` →
`src/core/flow_tracker.py` → `src/core/flow_metrics.py` → `src/core/window_features.py` →
`src/build_session_csv.py`) does, per session:

1. Parse every packet's Ethernet/IPv4/TCP-or-UDP headers (payload size
   computed from header-declared lengths, not from truncated captured
   bytes — these captures are snaplen-truncated).
2. Identify flows by 5-tuple, canonicalize direction (client → server =
   "up"), determine the client side via the TCP SYN packet (or
   first-packet-sender assumption for UDP, since QUIC has no handshake).
3. Detect per-flow retransmissions, out-of-order packets, and RTT samples
   from sequence/ack numbers (TCP only — QUIC/UDP has no equivalent
   signal, matching what a real no-DPI observer would see).
4. Bucket everything into fixed-size time windows (10/8/4/2/1 seconds,
   configurable) and compute the full feature table per window.

**Columns produced (~130 per window):**
- Identifiers/position: `label`, `session_id`, `window_index`,
  `window_start_s`, `window_end_s` (dropped before training — not real
  traffic features).
- Flow-level: `num_flows`, `num_new_flows`, `num_continuing_flows`,
  `frac_udp` (the signal that splits YouTube into `youtube_tcp`/
  `youtube_quic`).
- Direction-scoped (`up_`/`down_`/`total_` prefixes, so the model always
  has upstream, downstream, *and* combined totals to work with):
  `packet_count`, `byte_count`, `throughput_bps`, `retransmission_count`,
  `out_of_order_count`, 5 TCP flag counts (syn/ack/rst/psh/urg), and a
  full 6-stat distribution (min/max/mean/median/p25/p75) each for
  payload size, packet size, recv_window, and interarrival time.
- `rtt_ms_*` (6-stat distribution, TCP only).
- Application-layer, from real `requests_history.json` data (not
  inferred): `request_count`, `segment_size_bytes_*` (6-stat, real
  `content-length` headers), `request_duration_ms_*` (6-stat, real
  request→response timing).

Design rule throughout: a well-defined count with zero samples is `0`
(e.g. `request_count` when nothing happened in a window); a statistic
that's undefined with zero samples (median of nothing) is `NaN`, never
`0` — `0` there would be a false claim, not an honest "no data."

No IP addresses or ports ever reach the output — 5-tuples are used
internally to identify flows and immediately discarded.

---

## Step 2: First real training run — 10,000 sessions

At this point the dataset still mixed organic real-world sessions with
controlled/throttled lab experiments (see Step 3 — we hadn't separated
these out yet).

**Dataset:** 556,721 rows / 10,000 sessions (10-second windows). Label
breakdown by row: netflix 167,086, youtube_tcp 166,242, twitch 107,250,
youtube_quic 74,875, amazon 41,268.

**Training setup:** AutoGluon `TabularPredictor`, `presets="best_quality"`
(multi-layer stacking, k-fold bagging, widest hyperparameter search),
no time limit, GPU enabled (`num_gpus=1`), session-aware train/test split
(by `session_id`, not by row — windows from the same session are highly
correlated, so splitting by row would leak information).

**Result: 98% overall accuracy** on 115,531 held-out test rows. Per-class:

| class | precision | recall | f1 |
|---|---|---|---|
| amazon | 1.00 | 1.00 | 1.00 |
| netflix | 0.99 | 0.96 | 0.98 |
| twitch | 1.00 | 1.00 | 1.00 |
| youtube_quic | 0.98 | 0.98 | 0.98 |
| youtube_tcp | 0.96 | 0.99 | 0.97 |

Notably, `youtube_quic` was *not* the trivially-easiest class despite
having the most obvious protocol-level signal (`frac_udp`) available to
it — a real concern we'd flagged earlier (that a model could just learn
"UDP-majority = QUIC" as a cheap shortcut) turned out not to dominate the
result.

**Models that failed:** the leaderboard was entirely RandomForest/
ExtraTrees variants (stacked at layer 2) — every other model type AutoGluon
tried (LightGBM, CatBoost, XGBoost, both neural-net types) failed with
`ImportError` (90 distinct failures total). Root cause: the training
environment's `pip install -r requirements-train.txt` had not actually
pulled in AutoGluon's full model-zoo dependencies, even though the bare
`autogluon` package is supposed to install everything by default. So this
98% result was "best among models that successfully trained," not
literally every model AutoGluon offers — addressed in Step 3.

**Feature importance** (permutation importance on the held-out test set):
the top 3 features dominated, then a long, much flatter tail:

1. `frac_udp` (0.01644) — the protocol-mix signal.
2. `request_duration_ms_min` (0.01504) — a **real**, measured
   application-layer feature from `requests_history.json`, not inferred.
3. `request_count` (0.00848) — also genuine, also from the request log.

After those three, importance drops sharply — everything else (recv_window
percentiles, interarrival times, packet sizes, RTT, byte counts) contributes
in a flat 0.0001–0.0015 range. Two columns (`down_urg_count`,
`total_urg_count`) were automatically dropped by AutoGluon as unused,
since the URG flag essentially never appears in real TCP traffic.

One caveat for rigor: several mid-tier features (e.g. `total_byte_count`
at p=0.083, `up_byte_count` at p≈0.0995) sit above the conventional p=0.05
significance threshold — their importance ranking isn't fully trustworthy
on its own, just the clear top handful.

---

## Step 3: Cleaning up controlled experiments + fixing dependencies — now training on 17,350 sessions

Two separate issues surfaced after the Step 2 run, both fixed before the
current training run:

**1. Controlled/lab-condition sessions were silently mixed into the
dataset.** Some `exp.txt` files encode deliberately-throttled network
conditions (e.g. `youtube-0_rate+3000kbps+loss+1%+25%.run2`) rather than
representing organic real-world traffic. A scan of all 19,672 directories
on the NAS (`src/scripts/find_controlled_sessions.py`, detects condition text
sitting between the service name and `.run`) found **18,957 organic**
sessions and **715 controlled** ones. `src/filter_controlled_sessions.py`
split the already-processed CSVs accordingly — controlled rows moved out
to separate `session_statistics_*_second_window_controlled.csv` files
(kept around for potential separate research later), organic-only rows
remain in the main files. Going forward, `src/build_session_csv.py` now
detects and skips controlled sessions automatically at extraction time
(before even opening the pcap), logging exactly what made it a controlled
run rather than silently dropping or miscounting it.

**2. AutoGluon's full model zoo wasn't actually installed.** Forced a
clean reinstall (`pip install --force-reinstall --no-cache-dir
"autogluon.tabular[all]"`, plus `pip install xgboost` separately) and
verified all four previously-missing packages (`torch`, `lightgbm`,
`catboost`, `xgboost`) now import cleanly. No more model-fitting failures
expected on the next run — LightGBM/CatBoost/XGBoost/neural nets should
now actually compete against RandomForest/ExtraTrees instead of winning
by elimination.

**Current dataset: 17,350 sessions** — roughly 18,000 directories
processed from the NAS, minus the controlled-experiment sessions that get
filtered out (automatically, per the fix above).

**Status: training is currently running** (`best_quality`, no time limit,
GPU-enabled, on the 10-second window CSV) with the fixed dependencies —
no model failures so far, the full model zoo participating this time.
Model will save to `AutogluonModels/window_10s_17350sessions/`, feature
importance to `feature_importance_10s_17350sessions.csv` (both
automatically tagged with the real session count now, so re-running later
on an even bigger dataset will never collide with or overwrite this run).

---

## Step 4: Professor redirect — memory, not CPU cycles

The original plan for "what does this cost to compute" was CPU time:
`src/benchmarking/benchmark_feature_costs.py`, using `time.process_time()`
(excludes idle/wait, unaffected by what else is running on the machine),
single-threaded (matching a router's one-core reality), averaged over 100
real sessions. After discussion with the professor, the framing changed —
for a router, the binding constraint is how much *memory* each stat needs
to hold, not how many cycles a calculation takes. Literal CPU-cycle counts
would need `perf stat` anyway (Linux-only), so the pivot was also a
practical simplification, not just a reframing.

Built `src/benchmarking/benchmark_feature_memory.py` using a hand-written
recursive `deep_sizeof()` (`sys.getsizeof()` alone only measures a
container's own shallow size, not what it points to — badly understates a
list of boxed Python floats/ints). First 100-session result:
`retransmission_rtt_state` — the `seen_seqs` set inside
`flow_metrics.py`'s per-flow bookkeeping, tracking every sequence number a
flow has ever carried — dominates at ~111KB per flow on average, and
critically, it's the one structure that's *never pruned*, growing for a
flow's entire life. The four window-level distribution buffers
(interarrival_time/recv_window/packet_size/payload_size) each cost
roughly 54-66KB per window, since every raw value has to sit in memory
until the window closes before `_distribution()` can compute
min/max/mean/median/p25/p75 — these can't be done with a running
accumulator the way a sum or count can.

## Step 5: Real-time infrastructure — live capture, replay, and a Go port

The professor's direction shifted toward actually deploying this: can the
pipeline run against real, live traffic, and can it run fast enough on a
router. Built in order:

1. **`src/live_capture/replay_pcap_as_live.py`** — replays an existing
   session's `dump.pcap` as if it were arriving live (paced by real
   relative packet timing via a thread+queue), closing out windows the
   moment a packet's timestamp crosses a boundary rather than polling a
   clock. Chosen over true live capture first since it needs no root and
   poses no privacy risk on a shared box. Two real bugs were caught and
   fixed here by explicitly re-verifying against the batch pipeline's
   known-correct output (not just "does it run"): a window-count explosion
   bug (incremental calls were re-deriving the window count from scratch
   each time) and a retransmission-count divergence bug (the per-flow
   cursor into each flow's full recomputed history was being reset to 0
   every window close instead of persisting across closes). After both
   fixes: 50/50 rows match the batch pipeline, zero duplicate windows, all
   115 non-app-layer columns byte-identical.
2. **`src/live_capture/live_capture_pipeline.py`** — true live capture off
   a real NIC (`tcpdump` subprocess rotating files by `-G`, a consumer
   loop processing each finished rotation). Tested for real on this Mac's
   own `en0` interface — worked with no `sudo` needed (macOS apparently
   grants this by default) — and successfully captured ~140 seconds of
   genuine live traffic into 14 correctly-structured windows (app-layer
   columns correctly 0/NaN, no `requests_history.json` on a live capture).
3. **A full Go port** (`src/core_go/`, `src/live_capture_go/`), because
   the professor specified real-time/router deployment code should be Go,
   not Python. Field-for-field port of the live-relevant parts of
   `src/core/` (packet parsing, flow tracking, retransmission/RTT,
   window-stat computation, including replicating numpy's exact
   percentile interpolation and Python's tuple-comparison semantics for
   flow-key ordering, bit-for-bit). Verified against the same real test
   session: 6,499 of 6,500 compared values identical, the one outlier a
   ~240-nanosecond floating-point rounding artifact between Python/dpkt's
   and Go/gopacket's timestamp pipelines (confirmed by dumping raw
   per-packet timestamps side by side), not a logic bug. Also
   dramatically faster: 0.007–0.026s per window vs. Python's 0.2–0.4s+ at
   the same packet volumes. The Go live-capture tool was tested for real
   too, same `en0` interface, same clean result.

## Step 6: Repo reorganization + output-folder conventions

With this many moving pieces, `src/` was reorganized into `core/`
(shared library modules), `scripts/` (one-off utilities, including
`find_controlled_sessions.py`), `progress/`, `benchmarking/`, and
`live_capture/`, each with a Go-ported sibling (`core_go/`,
`live_capture_go/`) where real-time relevance applies. `build_session_csv.py`
and `train_autogluon.py` stay at `src/` root. Every moved file was
re-verified working post-move, not just re-imported successfully.

Two output-folder conventions were added so generated CSVs stop
cluttering the repo root: every benchmarking/feature-importance CSV now
lands under `benchmarks/`, and every live-capture/replay CSV lands under
`live_runs/` — both created with `os.makedirs(..., exist_ok=True)`
(`os.MkdirAll` on the Go side) right before the write. The Go tools'
default output paths are relative to wherever the binary is actually run
from (normally `src/`, where `go.mod` lives), not automatically the repo
root — worth remembering when checking where a Go tool's output actually
landed.

## Step 7: Memory benchmark round two — direction splits and a Go-side sanity check

Revisited `benchmark_feature_memory.py` to get much more granular: fixed
a real gap where `rtt_ms_buffer` was named in the module's own docstring
but never actually measured; added `p95`/`max` alongside the average
(a router has to provision for its worst session/window, not its typical
one); added `(total per session)` rows for the two per-flow categories
(since neither is ever pruned, the sum across a session's flows — not
just one flow's average — is the number that answers "can a router
actually afford this"; the real 100-session result: `retransmission_rtt_state`
averages ~14.7MB per session, peaking at ~85.9MB); and split every
distribution buffer by direction (`up_*_buffer`/`down_*_buffer`/
`total_*_buffer`) instead of pooling them, to see whether streaming's
download-heavy asymmetry shows up in memory cost the way it does in
bytes transferred.

It does, but inconsistently across metrics in the Python numbers:
`payload_size`/`packet_size` buffers showed an ~8x down:up ratio, while
`recv_window`/`interarrival_time` showed only ~2x. The likely cause:
CPython caches small integers (-5 to 256) as process-wide singletons, and
`up` traffic is mostly bare ACKs with `payload_size == 0` — so a long run
of repeated `0`s in a Python list costs almost nothing extra beyond the
first occurrence, understating exactly the buffers dominated by that
value. Since the actual deployment target is Go, not Python, this
mattered enough to check directly: built a Go port of this same benchmark
(`src/benchmarking/benchmark_feature_memory.go`, hand-rolled Go-side
memory-size approximations since Go has no general "deep sizeof" API,
duplicating `flow_metrics`'s bookkeeping loop locally the same way the
Python version does, since `ComputeFlowMetrics` doesn't expose its
internal state). Go doesn't get CPython's singleton-caching discount —
every slice element costs its real, fixed size regardless of value — and
at real 100-session scale, all four metrics agreed on a consistent
**~2x** down:up ratio, confirming the Python benchmark's 8x figure for
two of the four metrics was a measurement artifact, not a real traffic
asymmetry. Also surfaced, independent of that artifact: Go's flat
`FlowState` struct costs ~74 bytes per flow vs. Python's ~520 — a real,
language-level memory advantage for the actual router deployment, not
just a benchmarking quirk.

---

## Current status

**Training in progress**: a `--variant no_applayer` run (option 3 from the
old list, chosen first) is running on the Linux box in a
`train_no_applayer` tmux session, on the full accumulated 10-second-window
dataset (`session_statistics_10_second_window_no_applayer.csv`, lives on
the Linux box/NAS only — not mirrored locally), confirmed via `ps -ef` to
actually be passing the flag (not just named that way). Once it finishes,
the plan is to look at both its accuracy (per-class report) and its
feature importance together — not just compare to the old full-dataset
run — to decide which features are worth dropping going forward. The
tmux session's scrollback is being captured into `~/train_no_applayer.log`
on the Linux box (via `tmux capture-pane` + `tmux pipe-pane`, after the
original log file was lost to a repo reset) so progress can be tailed
without needing a true interactive reattach.

## Next up

Two directions still open once the `no_applayer` run finishes and its
results are reviewed:

1. **Train on the remaining window sizes** (8, 4, 2, 1 seconds) — four
   more training runs, same dataset, to compare how window granularity
   affects accuracy and which features matter at each scale.
2. **Go back to the 10s dataset and try trimming low-importance
   features** — the long flat tail from Step 2's feature importance table
   suggests many columns contribute very little; worth testing whether
   dropping them changes (helps, hurts, or doesn't move) the model's
   predictions, and whether a simpler feature set is worth the tradeoff.

Not yet decided which happens first.
