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

## Next up

Three directions under consideration once the current 17,350-session, 10s
run finishes:

1. **Train on the remaining window sizes** (8, 4, 2, 1 seconds) — four
   more training runs, same dataset, to compare how window granularity
   affects accuracy and which features matter at each scale.
2. **Go back to the 10s dataset and try trimming low-importance
   features** — the long flat tail from Step 2's feature importance table
   suggests many columns contribute very little; worth testing whether
   dropping them changes (helps, hurts, or doesn't move) the model's
   predictions, and whether a simpler feature set is worth the tradeoff.
3. **Train a "router-realistic" variant that drops all 13
   application-layer columns** (`request_count`, `segment_size_bytes_*`,
   `request_duration_ms_*`). These come from the browser's own
   `webRequest` log — real, decrypted HTTP data the browser can see
   because it's the TLS endpoint. A real deployed router sitting in the
   middle of the network path is not a TLS endpoint and has no way to see
   this without actively decrypting traffic (a TLS-terminating
   man-in-the-middle), which is exactly the kind of deep packet inspection
   this whole project is built to avoid needing. Two of Step 2's top 3
   most important features (`request_duration_ms_min` at #2,
   `request_count` at #3) are app-layer, meaning the 98% accuracy number
   partially leans on information a real router couldn't observe. Training
   without those 13 columns would give the honest, deployable accuracy
   number — separate from the oracle-level one that assumes full
   application-layer visibility.

Not yet decided which of these three happens first.
