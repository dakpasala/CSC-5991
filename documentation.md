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

## Step 8: The `no_applayer` run finishes — 0.98 accuracy, and one small model instead of a 70GB ensemble

The `no_applayer` training run on the full 10-second dataset (19,468
sessions, every application-layer column removed) finished. On the
held-out test set of 212,184 windows, from sessions the model never saw
during training, it scored **0.98 accuracy and 0.99 macro F1**:

| class | precision | recall | F1 | test windows |
|---|---|---|---|---|
| amazon | 1.00 | 1.00 | 1.00 | 18,411 |
| netflix | 0.99 | 0.97 | 0.98 | 59,413 |
| twitch | 1.00 | 1.00 | 1.00 | 38,598 |
| youtube_quic | 0.99 | 0.98 | 0.98 | 30,086 |
| youtube_tcp | 0.96 | 0.99 | 0.98 | 65,676 |

This is the first real `youtube_quic` result. The TCP/QUIC split had only
been reasoned about before, and QUIC turned out *not* to be the trivially
easy class we expected. Amazon and Twitch were the near-perfect ones. The
weakest spot is Netflix vs. `youtube_tcp`: Netflix's lower recall plus
`youtube_tcp`'s lower precision suggest some Netflix windows get called
`youtube_tcp`. A confusion matrix would confirm it.

The finished model directory was **70GB** (218 models, best one
`WeightedEnsemble_L3`). Running AutoGluon's `clone_for_deployment()` on it
only got it down to **50GB**, because the L3 ensemble sits on L2 stackers
that each take every L1 model's output as input, so almost nothing can be
dropped. That's unusable on a laptop, let alone a router.

The validation leaderboard showed the size wasn't buying much. The L3
ensemble scored 0.985771 on validation; the best single model scored
0.984623, a gap of ~0.11 points. The top 15 single models were all within
~0.03 points of each other, but their prediction cost varied ~35x. We
picked **`XGBoost_r22_BAG_L1`** (0.984536), the best balance of the two
among those near-tied models: 3rd most accurate and 4th fastest of the
top 15, ~15x faster to predict than the most accurate one. It wasn't
strictly the fastest. Three other XGBoost variants were quicker (down to
7.0s vs. its 16.0s) at slightly lower validation accuracy, and given how
close the scores are, any of them would have been an equally reasonable
pick. It was
chosen on validation, not test, so reporting its test score later isn't
data snooping. A single-model clone came out to **333MB**, small enough to
copy to the Mac. That's a 70GB → 333MB reduction for ~0.1 points of
validation accuracy, which is the lightweight-classifier argument in one
number.

Locally, inference runs in a separate `.venv-infer` (Python 3.13) pinned to
the Linux box's exact `autogluon.tabular`/`xgboost`/`scikit-learn`/`numpy`/
`pandas` versions, since pickled models are version-sensitive. As a first
check, we pulled 15 held-out test sessions (3 per class, reproducing the
training script's exact session split). The local model scored 0.9963 over
their 815 windows, and every one of the 15 sessions was correct by majority
vote across its windows.

Permutation feature importance for this run is still computing on the
Linux box: AutoGluon estimated ~52 hours, because each shuffle has to
score the full 70GB ensemble.

## Step 9: Real-time labeling — Go captures, Python labels, every 10 seconds

The Go tools can't load AutoGluon's pickled models, so we connected the two
halves with a sidecar. With `-model-dir`, the Go live-capture and replay
tools start `src/inference/predict.py` once as a child process. They pipe
each finished window's feature row straight into it, as CSV text over
stdin, with no intermediate file. Python appends a line per window
(window number, time range, predicted label, confidence, per-class
probabilities) to a predictions CSV in `live_runs/`, and Go prints
`-> window N predicted: <label> (<confidence>)` as it goes.

Two details keep it reliable on live traffic. Go never waits on Python:
rows queue in a buffer, so a slow model load at startup can't stall packet
capture into dropped packets. And Python runs in its own process group,
so Ctrl+C stops only Go, which then sends the last partial window and
waits for every queued prediction before exiting.

Results:
- **Replaying all 7 local sessions:** every session correct by majority
  vote. The only stray windows were 5 of `lab08_netflix`'s 49 called
  `youtube_tcp`, the same confusion as the full test report. (These local
  sessions may have been in the training data, so this shows the pipeline
  works, not that the model generalizes.)
- **Live on the Mac's Wi-Fi with YouTube playing:** 11 of 13 windows
  labeled YouTube, mostly `youtube_quic`, which matches Chrome streaming
  YouTube over QUIC today. Both misses were low-confidence `netflix` (0.56
  and 0.45), and every window at 0.85 confidence or above was YouTube.
  This is a model trained on 2018-2019 traffic, classifying 2026 traffic.
- **With nothing streaming,** idle laptop traffic still gets one of the 5
  labels, at ~0.5 confidence. The model is closed-world, so low confidence
  is the only sign a window isn't one of the known services. Live capture
  also sees the whole interface, so other apps' traffic mixes into the
  windows. Both are limitations to state in the thesis.

## Step 10: Thesis writing starts

Started a `thesis/` directory. `thesis/related_work.md` is a first draft
of the related work chapter (~1,250 words, about 4.5 double-spaced pages).
It covers traffic classification without DPI, encrypted video traffic
analysis, in-network classification, and the two closest papers (Bronzino
et al.'s streaming-quality inference and Traffic Refinery). It ends with a
paragraph on how this thesis differs from them. Its citations were written
from memory and still need checking against the actual papers. It is also
still unconfirmed whether the session dataset is Bronzino et al.'s; ask the
professor.

---

## Step 11: Feature importance, then choosing features with single XGBoost models

The permutation feature importance for the `no_applayer` run finished after
about 52 hours. It was computed for the whole `WeightedEnsemble_L3` stack on
the test set. Only **8 of 110 features** were clearly important on their own:
`rtt_ms_min`, four `down_recv_window_*` stats, `frac_udp`, `rtt_ms_max` and
`num_continuing_flows`. The other 102 were within noise of zero. That doesn't
mean they're useless. One-at-a-time shuffling can't see redundancy: if six
stats of the same distribution carry the same information, shuffling any one
of them costs nothing. The top features pointed to something important,
though: receive window, RTT and `frac_udp` describe *which servers and network
path* produced the traffic, more than the traffic's shape. That flagged a
possible shortcut early.

To decide what to drop, AutoGluon was the wrong tool (two weeks per run). A
new script, `src/train_xgboost_ablation.py`, trains single XGBoost models in
minutes:

- **Same test sessions** as the AutoGluon run (session-aware split, seed 0),
  so the numbers are directly comparable.
- **A separate validation split** carved out of the training sessions. Every
  decision is made on it: early stopping, importance ranking, which features to
  drop, and the recommendation. Test accuracy is recorded but never used to
  choose anything.
- **Recursive feature elimination (RFE)**: train, rank by permutation
  importance on validation, drop the weakest 20 %, repeat. Ranking again after
  every drop handles the redundancy problem.

Three rounds of runs (70 models in total, results in
`benchmarks/xgb_ablation_10s_no_applayer.csv`):

1. **Feature-by-feature RFE from all 112 features.** Accuracy stayed flat from
   112 down to about 31 features (0.9827 → 0.9820) and collapsed below 4. The
   recommended 47-feature set lost only 0.05 points, **but it still needed all
   13 distribution buffers**. Counting features is the wrong measure of router
   cost.
2. **Counting router cost properly.** Cost is per distribution *family*, not
   per column. min/max/mean can be updated one packet at a time. median, p25 and
   p75 need every value in the window kept in a buffer, and one buffer serves all
   six stats of its family. With that measure:
   - Dropping every `total_*` column cost nothing (0.9824).
   - **Dropping every median/p25/p75 (no buffers at all) cost only 0.17 points
     (0.9810).**
   - Family-by-family RFE reached 6 buffers at 0.9818.
3. **RFE inside the no-buffer set.** It recommended **22 features with 0
   buffers at 0.9799**: "option C". Below 10 features accuracy fell off
   (8 features: 0.9705, "option D").

`benchmarks/feature_selection_results.md` documents all of this, with candidate
options A–D and their feature lists in `benchmarks/feature_sets/`. The script's
`--final <feature list>` mode trains one deployable model on any list (fit on
the training split with early stopping, then refit on training + validation).
`final_option_c` scored **0.9801** test accuracy.

Two corrections from this step:

- **`XGBoost_r22` was not the cheapest model.** Among the top 15 single models
  it was 3rd by accuracy and 4th by speed. The best balance, not the fastest.
  The docs were corrected. The full 218-model leaderboard, with accuracy and
  speed ranks, is in `benchmarks/model_leaderboard_10s_19468sessions_no_applayer.csv`.
- **AutoGluon's validation scores are optimistic.** Its internal 8-fold bagging
  splits by row, not by session, so windows from the same session land on both
  sides of a fold. The 0.98 test result is still valid, since test sessions were
  fully held out. Future AutoGluon runs should pass the session IDs as `groups`.

## Step 12: The router, and a Python-free pipeline

The professor provided a real deployment target: an OpenWrt 25.12 router
(MediaTek Filogic, aarch64 Cortex-A53, about 1 GB RAM, no swap, no Python or
gcc), reachable as `root@192.168.1.1` from the Linux box, capturing on `lan2`.
The Linux box's default route goes through it.

The router can't run Python or AutoGluon, so the whole pipeline moved into one
Go binary:

- **A Go XGBoost evaluator** (`src/live_capture_go/xgbmodel`) reads XGBoost's
  JSON model format directly. It agrees with xgboost's own `predict_proba` on
  **every label across 28,463 windows**, with a largest probability difference
  of 2.4e-7, including rows with missing values. It also honors an ablation
  model's early-stopping point.
- **The live and replay tools score in-process** when `-model-dir` points at a
  `.json` model. An AutoGluon directory still uses the Python sidecar. On three
  replayed sessions, Go and Python wrote byte-identical prediction files.
- **Packet capture without libpcap.** Builds with `CGO_ENABLED=0` use a pure-Go
  Linux raw socket (`pcapgo.EthernetHandle`), with the same 200-byte capture
  limit and the true packet length. That makes the router build a single
  **3.6 MB static ARM64 binary**.
- **`deploy_router.sh`** builds that binary on the Mac and copies it and a model
  to the router (`/root/csc5991/`) with one password prompt. Any model works
  (`MODEL=... ./deploy_router.sh`). It copies to a temp file and renames, so a
  running instance can't block a redeploy.

## Step 13: Testing live on the router with known ground truth

`src/live_capture/stream_and_check.py` runs on the Linux box. One command:

1. starts `live-router` on the router over ssh;
2. plays a known video in headless Chromium;
3. logs playback every 5 s;
4. stops the router process and copies its predictions back;
5. scores every window against what was actually playing.

It reports accuracy three ways:

- *all windows*;
- *active windows*: at least 100 packets;
- *playing windows*: the video position actually advanced.

Playing windows is the honest measure of the classifier.

The first runs scored **0.19**. None of the causes was the model:

1. **The router merged packets (GRO).** The kernel combined packets into
   super-packets of up to 49 KB before capture. The training data never exceeds
   1,514 bytes. Fixed, with the professor's approval: `ethtool -K eth1 gro off`
   and `ethtool -K lan2 gro off`. These revert on reboot, so check them before
   every round of tests.
2. **Headless Chromium downloaded its own components**: megabytes of non-video
   traffic. Fixed with flags.
3. **YouTube stopped playback after about 30 s**, every run, because it detected
   the automated browser. Fixed by presenting as normal Chrome. The script now
   saves a screenshot when playback stalls or never starts, reloads, and
   excludes stalled windows from scoring.

With those fixed, option C labeled live YouTube correctly in **14 of 14**
playing windows. Write-up: `live_runs/tests/live_eval_results.md`.

## Step 14: Measuring router cost, and making extraction cheap

`live-router -perf-output` records, per window:

- CPU %;
- packet-handling, window-close and scoring time;
- heap and resident memory;
- retained per-flow state.

The eval script summarizes all of it next to the accuracy.

That immediately showed a memory problem. The Go accumulator (a port of the
Python one) **kept every packet of every flow for the whole run** and re-analyzed
each flow's full history at every window close. An orphaned `live-router` left
over from an interrupted test ran for 5.8 hours and **peaked at 462 MB** on a
router with about 1 GB and no swap. Since then, `live-router` has
`-max-duration`, and the eval script always sets it.

The fix is **selective feature extraction** (`core_go/selective.go`), now the
default with a Go-native model:

- The router computes **only the features the loaded model uses**.
- Distributions it needs only the min/max/mean of are kept as running values,
  with no buffer.
- RTT and retransmission state is updated incrementally, or skipped entirely
  when the model doesn't use it.
- Results are **identical** to the old accumulator: every value and label
  matched across 4 models × 7 sessions.
- It costs **5–23x less CPU per packet**, and holds **0.02–0.06 MB instead of
  25–57 MB** for a model with no RTT features.

One live run also showed **swapped upload/download directions**: 47.5 MB
"uploaded". For UDP, the pipeline takes whoever sent a flow's first seen packet
to be the client, and once at the router the server's packet was seen first.
`live-router` now has `-port-direction` (on by default): a flow first seen
*from* a service port (below 1024) is assigned server-to-client. `replay` keeps
it off by default, because the training data was computed with the original
rule. Turning it on for recorded sessions changes some flows and lowers option C
slightly on them (0.878 → 0.816 on one session). The fully consistent fix is to
apply the rule in `flow_tracker.py`, regenerate the dataset, and retrain.

## Step 15: Fewer features, live (YouTube)

Four models were run on the same live YouTube video: 112, 22, 15 and 8 features.

- **Option C: 1.00 again.** Three separate runs at 1.00.
- **15 and 8 features: perfect on active windows**, with 2 misses each on quiet
  windows.
- **The 112-feature model scored lowest (0.31)**, even with directions correct.

The cause is **drift in YouTube's QUIC traffic**. Today's client sends about
one acknowledgment per 6 data packets (0.176 per download packet), against one
per 2 (0.505) in the 2018–19 data. The full model relies on mixed-direction
percentile features (`total_packet_size_p25`: 1,292 B live vs. 81 B in
training) that encode exactly that ratio. The selected models had dropped those
features.

Cost:

- Window close went from 0.87 ms with all 112 features to **about 0.07 ms**
  with 8–22.
- Option C's per-flow state went from 18,299 kept packets (old binary) to 129
  entries.
- CPU stayed at about 0.35 % of one core.
- **Scoring time follows tree count, not feature count**: about 1.1–1.2 µs per
  tree on the router's CPU.
- The startup memory peak (110–204 MB) is parsing the model's JSON, about 4x
  the file size.

## Step 16: Twitch, fingerprint features, and the leading model

On live Twitch (`twitch.tv/tfue`) **every model failed (0.06–0.38)**, despite
clean capture and playback. Comparing live and training Twitch windows showed
why. The models rely on **fingerprint features**, and those changed:

| Feature | Live | Training | Cause |
|---|---:|---:|---|
| `rtt_ms_min` | 0.058 ms | 0.006 ms | capture point (router vs. client) |
| `down_psh_count` | 787 | 21 | server stack |
| `down_recv_window_mean` | 618 | 363 | server stack (now close to training *Amazon*) |
| `up_recv_window_max` | 32,245 | 10,091 | client OS |

YouTube had escaped this because QUIC is UDP: no TCP receive window, RTT or
flags, so the model had to use traffic shape.

Two models were trained without any receive-window, RTT, TCP-flag,
retransmission or out-of-order feature
(`benchmarks/feature_sets/{option_c_no_fingerprint,shape_streaming}.txt`,
`--learning-rate 0.3 --max-rounds 600`, on 10 cores pinned with `taskset`):

| Model | Features | Offline test | Live Twitch | Live YouTube (QUIC) | Live YouTube (TCP) |
|---|---:|:---:|:---:|:---:|:---:|
| `final_option_c` (reference) | 22 | 0.980 | 0.19 | 1.00 | – |
| `final_option_c_no_fingerprint` | 12 | 0.953 | 0.69 | 1.00 | 0.88 |
| **`final_shape_streaming`** | 28 | 0.964 | **0.88** | **1.00** | – |

`final_shape_streaming` is **the most accurate model live and among the
cheapest**. On Twitch, the heaviest traffic tested:

- 1.9 % CPU (about 190 ms of processor time per 10 s window);
- 2.0 µs per packet;
- 37 MB steady-state RAM;
- 2 ms scoring per window;
- a 111 MB startup peak;
- **no per-flow state**.

The fingerprint models used 4.4 µs per packet and 52–61 MB with growing state.
That isn't a coincidence. RTT and retransmission features are both the
expensive ones (they need per-flow sequence tracking) and among the fragile
ones. Receive window and flag counts were cheap, but had drifted too.

The thesis result: **the model that scored best offline failed live**. A
28-feature, traffic-shape-only model gave up about 1.6 points of offline
accuracy and was both the most accurate on today's traffic and the cheapest to
run on the router. Full tables: `live_runs/tests/live_eval_feature_varying.md`.
These are single runs.

---

## Step 17: The final model, long live runs, and dry-run priority (2026-11-17)

This step settles on a deployable model, measures it over 20-minute live runs on the router, and
adds the first piece of the QoS application: deciding which connections to prioritize.

### The model: `final_shape_streaming`

A single XGBoost model with **28 traffic-shape statistics**, computed per 10-second window. It has
no TCP receive-window, RTT, TCP-flag, retransmission or out-of-order features: the "fingerprint"
features that broke on live Twitch (Step 16).

| Property | Value |
|---|---|
| Features | 28 (26 used: the two throughput columns duplicate the byte counts) |
| Trees | 1,605 (321 boosting rounds × 5 classes) |
| Model file | 25.9 MB JSON |
| Offline test accuracy (2018–19 held-out sessions, 212,184 windows) | **0.964** (macro F1 0.966) |
| Buffers on the router | **0**: every statistic is a running min, max, mean or count |
| Per-flow state on the router | **None**: no RTT or retransmission tracking |

### The 28 statistics

"Up" is client → server (requests and acknowledgments); "down" is server → client (the video).
*Share* is the statistic's share of the model's total split gain.

**Flows and protocol**

| Statistic | Meaning | Share |
|---|---|---:|
| `frac_udp` | Share of packets that are UDP (QUIC) rather than TCP | 17.7 % |
| `num_flows` | Connections active in the window | 1.3 % |
| `num_continuing_flows` | Connections already open in the previous window | 1.3 % |
| `num_new_flows` | Connections that started in this window | 0.7 % |

**Upload (client → server)**

| Statistic | Meaning | Share |
|---|---|---:|
| `up_packet_size_max` | Largest packet the client sent | 20.0 % |
| `up_payload_size_max` | Largest data payload the client sent | 9.5 % |
| `up_interarrival_time_max` | Longest gap between client packets | 6.2 % |
| `up_byte_count` | Total bytes the client sent | 6.0 % |
| `up_interarrival_time_min` | Shortest gap between client packets | 4.1 % |
| `up_payload_size_mean` | Average client payload | 2.5 % |
| `up_packet_size_mean` | Average client packet size | 1.9 % |
| `up_packet_size_min` | Smallest client packet | 0.9 % |
| `up_interarrival_time_mean` | Average gap between client packets | 0.6 % |
| `up_packet_count` | Number of client packets | 0.4 % |
| `up_payload_size_min` | Smallest client payload | 0.1 % |
| `up_throughput_bps` | Client bits per second (duplicates `up_byte_count`) | 0 % |

**Download (server → client)**

| Statistic | Meaning | Share |
|---|---|---:|
| `down_packet_size_mean` | Average video packet size | 7.9 % |
| `down_interarrival_time_max` | Longest gap between server packets (pauses between bursts) | 5.7 % |
| `down_byte_count` | Total bytes received | 2.8 % |
| `down_interarrival_time_min` | Shortest gap between server packets | 2.2 % |
| `down_payload_size_max` | Largest payload received | 2.0 % |
| `down_packet_count` | Number of packets received | 1.7 % |
| `down_packet_size_min` | Smallest packet received | 1.4 % |
| `down_payload_size_mean` | Average payload received | 1.2 % |
| `down_interarrival_time_mean` | Average gap between server packets | 1.1 % |
| `down_packet_size_max` | Largest packet received | 0.7 % |
| `down_payload_size_min` | Smallest payload received | 0.3 % |
| `down_throughput_bps` | Bits per second received (duplicates `down_byte_count`) | 0 % |

The two strongest signals:

- **The client's largest request packet** (`up_packet_size_max`). It reflects how each service's
  player asks for video, so it identifies the app rather than the network.
- **`frac_udp`**, which separates QUIC from TCP.

The inter-arrival extremes capture each service's rhythm: YouTube's bursts and silences against
Twitch's steady segment fetches.

### Live accuracy on the router

Ground truth comes from `stream_and_check.py`: headless Chromium on the Linux box behind the router
plays a known stream while `live-router` classifies every 10-second window. *Active* windows carry
≥ 100 packets, i.e. real video traffic, as opposed to the quiet gaps between download bursts.

| Traffic | Run | Windows | Accuracy, active windows | Accuracy, all windows |
|---|---|---:|:---:|:---:|
| YouTube over QUIC | 20 min, 113-min video | 119 | **1.000 (116/116)** | 0.975 |
| YouTube over QUIC | 3 min | 14 | **1.000 (14/14)** | 1.000 |
| Twitch, TCP-only windows | 20 min, live stream | 102 | **0.912 (93/102)** | – |
| Twitch, all windows | 20 min, live stream | 119 | 0.807 | 0.807 |
| Twitch | 3 min | 16 | 0.875 (14/16) | 0.875 |

- **YouTube over QUIC:** every window with real traffic over 20 minutes was correct.
- **Twitch:** the misses come almost entirely from UDP that today's Twitch mixes in. Windows with
  5–50 % UDP scored 0.20.
  - That UDP comes in bursts about every 4–5 minutes, consistent with mid-roll ads delivered over QUIC.
  - The 2018–19 training data has no Twitch traffic with any UDP.
  - The remaining TCP misses came at the stream's start (likely a pre-roll ad) and after it dropped to
    480p.
- YouTube over TCP has been tested live only with the 12-feature sibling model
  (`final_option_c_no_fingerprint`): 0.88. It hasn't been tested with this model yet.

**Compared with the earlier models** (live, single runs):

| Model | Features | Offline test | Live Twitch | Live YouTube (QUIC) |
|---|---:|:---:|:---:|:---:|
| All features | 112 | 0.983 | 0.25 | 0.31 |
| Option C (with fingerprint features) | 22 | 0.980 | 0.19 | 1.00 |
| Option C without fingerprints | 12 | 0.953 | 0.69 | 1.00 |
| **`final_shape_streaming`** | **28** | 0.964 | **0.88–0.91** | **1.00** |

The model that was best offline was the worst live. The fingerprint features it relied on (TCP
receive window, RTT, flags) changed since the dataset was recorded.

### Resource use on the router

Measured by `live-router -perf-output` on the router: OpenWrt, aarch64 Cortex-A53, about 1 GB RAM,
no swap. "Steady RAM" is resident memory after the first 2–3 minutes.

| Run | Traffic load | CPU, mean / max (one core) | Steady RAM | Per packet | Feature calc per window | Scoring per window | Per-flow state |
|---|---|---:|---:|---:|---:|---:|---:|
| YouTube, 20 min | 162.7 MB total | 0.37 % / 0.94 % | **37.2 MB, flat** | 1.71 µs | 0.075 ms | 1.93 ms | 0 |
| Twitch, 20 min | 1,074.5 MB total | 1.99 % / 4.28 % | **38.1 MB, flat** | 2.11 µs | 0.065 ms | 2.10 ms | 0 |

| Summary | Value |
|---|---|
| Average steady-state RAM | **~37–38 MB**, flat for 20 minutes, with no growth |
| Peak RAM | ~112 MB, at startup only, while parsing the 25.9 MB model JSON |
| CPU | under 2 % of one core on average, even on 1080p Twitch |
| Binary | one static 3.6 MB Go program; nothing else installed on the router |

For comparison, the original all-features build peaked at **462 MB** in a long run. It kept every
packet of every flow and analyzed them again at every window.

### Dry-run priority (the first step toward QoS)

`live-router -priority` decides which connections a router *would* prioritize, and prints and logs
those decisions. It doesn't change anything on the router.

| Rule | Setting |
|---|---|
| Start priority | A window with ≥ 100 packets and ≥ 0.9 confidence. Tag its heaviest connections, but only ones that could be the service: server port 443 or 80, UDP for QUIC labels, TCP otherwise. |
| Keep priority | Through quiet windows. An idle tagged connection uses no bandwidth, and YouTube's next burst reuses the connection. |
| End priority | The connection closes, or it is idle for 2 minutes. |

Results:

- **YouTube, 20 min:** one QUIC connection, tagged in window 1 and kept for all 20 minutes.
- **Twitch, 20 min:** all 6 events were on TCP/443. Priority followed the stream as Twitch rotated
  connections.
- The port/transport filter was added after an SSH transfer on the Linux box got tagged in an earlier
  run.

### Problems found and fixed today

| Problem | Effect | Fix |
|---|---|---|
| The router's own SSH traffic left as GSO super-packets | Packets of 1,750–2,150 B, plus stray TCP in every window | `live-router -exclude-own-traffic` |
| Restarting a 10.5-min video mid-run | The box received ~0 MB for 9 min while "playing", so windows had nothing to classify | Use a video longer than the run (113-min video) |
| Priority picked an SSH transfer as the heaviest flow | Wrong connection tagged | Only tag 443/80 flows with the label's transport |
| Ctrl+C right after start | An orphaned `live-router` ran for 25 min | The script stops it on early Ctrl+C; `-max-duration` as a backstop |
| Browser crash or interruption mid-run | Result files never copied back | The browser restarts itself; router files are always collected |

---

## Current status

- **Final model:** `final_shape_streaming`. 28 traffic-shape statistics, 0 buffers, no per-flow
  state, offline test 0.964.
- **Live on the router (20-minute runs):**
  - YouTube over QUIC: **1.000** on every active window.
  - Twitch: **0.91** on TCP windows, and 0.81 overall (lowered by UDP bursts that look like ads).
- **Router cost:** about 37–38 MB of steady RAM, flat; under 2 % CPU; 2 ms scoring per window.
- **Priority:** the dry-run layer works and follows the right connections. Enforcement (nftables +
  `cake`) needs the professor's approval.
- **Write-ups:** `live_runs/tests/live_eval_results.md` and `live_runs/tests/live_eval_feature_varying.md`.
  Raw run files were deleted after summarizing.

## Next up

1. **Repeat runs:** 2–3 more 20-minute runs each for YouTube and Twitch, so the numbers have a spread.
2. **YouTube over TCP** (`--disable-quic`) with `final_shape_streaming`.
3. **Enforce priority**, with the professor's OK. Mark tagged connections with nftables and DSCP, then
   shape with `cake`. Measure video resolution and stalls with and without priority, under a competing
   download.
4. **Smaller startup footprint:** a compact binary model format or fewer trees. This removes the
   ~112 MB startup peak.
5. **Thesis:** results chapter from the live write-ups. A "pitfalls of live evaluation" methods section
   (GRO, bot detection, flow direction, the router's own traffic, cached playback). Limitations: Twitch's
   UDP/ads and the 2018–19 training data. Verify the related-work citations.
6. **Future work** (out of scope for now): retrain with traffic captured at the router today.
