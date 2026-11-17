# Live router evaluation: results so far (2026-11-16)

This file replaces the raw per-run CSVs from the first round of live tests (`live_runs/eval_*`).
Every number below comes from those files. Nothing here is estimated.

## Setup

- **Router.** The professor's lab router: OpenWrt 25.12.6, MediaTek Filogic, aarch64 Cortex-A53,
  about 1 GB of RAM, no swap, no Python or gcc. Packets are captured on `lan2`.
- **Classifier on the router.** `live-router` is a single static Go binary (`./deploy_router.sh`).
  It captures packets with a pure-Go AF_PACKET socket, builds 10-second windows, and scores each
  window with an XGBoost model evaluated in Go (`src/live_capture_go/xgbmodel`). The Go evaluator
  matches xgboost's `predict_proba` on every label, with a maximum probability difference of 2.4e-7.
- **Ground truth.** `src/live_capture/stream_and_check.py` runs on the Linux box (192.168.1.188),
  whose default route is the router. It starts `live-router` over ssh and plays a known video in
  headless Chromium, logging playback every 5 s. Then it stops `live-router` and scores every window
  against what was actually playing.
- **Video.** Blender's *Sintel* on YouTube (`https://www.youtube.com/watch?v=eRsGyueVLvQ`),
  3 minutes per run. Chromium streams YouTube over QUIC, so the expected label is `youtube_quic`.
- **How a run is scored.**
  - *All windows* means every window after playback started.
  - *Active windows* means windows with at least 100 packets.
  - *Playing windows* means windows where the timeline shows the video position advancing.
  - Playing-window accuracy is the number that measures the classifier. The other two also count
    silence between YouTube's download bursts, and stalls.

## What had to be fixed before the numbers meant anything

The first runs scored 0.19 accuracy. None of the causes was the model.

1. **The router merged packets (GRO).** The kernel combined incoming packets into super-packets of
   up to 49,298 bytes before capture. Wire frames are at most 1,514 bytes, and so is the training data,
   so the size features were far outside anything the model had seen.
   - Fix, approved by the professor: `apk add ethtool`, then `ethtool -K eth1 gro off` and
     `ethtool -K lan2 gro off` on the router.
   - Both settings revert on reboot, so check with `ethtool -k eth1 | grep generic-receive` before
     each round.
   - The eval script now flags any window containing packets over 1,514 B.
2. **Headless Chromium downloaded its own components.** A fresh profile pulls component updates and
   safe-browsing lists, megabytes of non-video TCP traffic in the same windows as the video.
   - Fix: `--disable-background-networking`, `--disable-component-update` and related flags.
3. **YouTube stopped playback about 30 s in, on every run.** The timeline shows the video position
   frozen at 0 and the size at 0x0 from about 40 s on. YouTube had detected the automated browser
   (`HeadlessChrome` in the user agent, `navigator.webdriver = true`).
   - Fix: a normal Chrome user agent, with the automation flag hidden.
   - The script now detects stalls. It saves a screenshot and the page text, then retries and reloads.
     `youtube_stall_example.png` shows one: YouTube's own "Something went wrong" player error at 0:41,
     recovered by a reload.
   - Stalled windows are excluded from playing-window accuracy.
4. **One run had its upload and download directions swapped** (see `all.json` below). For UDP flows,
   the pipeline takes whoever sent the flow's first seen packet as the client. In that run, the QUIC
   video flow's server was taken as the client, so 47.5 MB counted as upload and 0.4 MB as download,
   with every directional feature mirrored. The healthy runs show 0.5 to 0.7 MB up and 16 to 19 MB down.
   - Fix: `live-router -port-direction`, on by default. A flow first seen coming from a service port
     (below 1024) to an ephemeral port is assigned server to client.
   - `replay` leaves the rule off, for exact parity with the Python pipeline and the training data.
     The training data itself contains some flows mis-assigned this way, so turning the rule on there
     lowers option C from 0.878 to 0.816 on one local session.
   - Fully consistent fix: apply the same rule in `flow_tracker.py`, regenerate the dataset, and retrain.

## Accuracy

### How the runs got fixed

All of these runs used model option C (22 features).

| Run | State | Max packet (B) | Video played | Active-window accuracy | Playing-window accuracy | All-window accuracy |
|---|---|---|---|---|---|---|
| 131907 | GRO on, video died | – | 38 s | – | – | 0.19 |
| 132918 | GRO on, video died | 49,298 | 37 s | 0.25 (4 windows) | – | 0.19 |
| 143355 | GRO off, video died | 1,514 | 37 s | 0.50 (2 windows) | – | 0.31 |
| **144347** | **all fixes** (one stall, reloaded) | 1,514 | 112 s | **1.00 (14/14)** | **1.00 (14/14)** | 0.88 |

In the runs where the video died, the windows that carried real video traffic were labeled YouTube
at high confidence (`youtube_quic` at 0.9999 and 1.000). Every other window was idle traffic scored
as if YouTube were playing.

### Model comparison

Same video, 3 minutes each, run back to back with GRO off and every fix except the direction rule.

| Model | Features | Playing-window accuracy | Active-window accuracy | Mean confidence | Up / down (active windows) |
|---|---|---|---|---|---|
| `all.json` | 112 | 0.29 (5/17) ⚠️ | 0.42 | 0.57 | **47.5 MB / 0.4 MB** (flipped) |
| **`final_option_c.json`** | **22** | **1.00 (16/16)** | **1.00** | **0.99** | 0.68 MB / 18.8 MB |
| `..._step06.json` | 15 | 0.88 (14/16) | 0.93 | 0.95 | 0.51 MB / 16.3 MB |
| `..._step09.json` (option D) | 8 | 1.00 (13/13) | 1.00 | 0.96 | 0.51 MB / 16.5 MB |

- ⚠️ The `all.json` run is **invalid**, not a model result. It is the direction flip from issue 4,
  and any model would have failed on it. Rerun it with the fixed binary.
- **Option C scored 1.00 on two separate runs** (144347 and 191615).
- Each model has only one run, so a 2-window difference (step06's 0.88) is within run-to-run noise.
  Three or more runs per model are needed before comparing them.

## Cost on the router

These four runs used the binary from **before** selective feature extraction. Every model's run
computed all ~130 statistics and kept every packet of every flow, so feature-extraction cost is the
same across them. Only the model differs.

| Model | Trees | Scoring time per window | CPU, mean | Window close, mean | Peak RAM | RAM at end | Live heap, max | Packets kept at end |
|---|---|---|---|---|---|---|---|---|
| `all.json` | 1,965 | 2.30 ms | 1.22 % | 9.0 ms | 109.5 MB | 52.8 MB | 33.0 MB | 42,223 |
| `final_option_c.json` | 3,625 | 4.15 ms | 0.41 % | 3.4 ms | 203.4 MB | 55.4 MB | 40.5 MB | 18,299 |
| `..._step06.json` | 2,965 | 3.45 ms | 12.07 %* | 3.5 ms | 189.8 MB | 49.5 MB | 34.7 MB | 15,853 |
| `..._step09.json` | 3,065 | 3.46 ms | 0.35 % | 3.0 ms | 216.5 MB | 50.5 MB | 34.3 MB | 15,970 |

\* This is a measurement artifact, since fixed. Windows closed together after a traffic gap had their
CPU time divided by microseconds of wall time, giving a fake 101 to 109 % reading. With those windows
excluded, the run is below 1 %.

- **CPU is not the constraint.** Every model used under about 1.3 % of one core. Per-packet handling
  cost about 2.2 µs.
- **Scoring time follows the number of trees, not the number of features**: about 1.1 to 1.2 µs per
  tree on the Cortex-A53. Option C is the slowest to score because it has the most rounds (725).
  Making a model faster means fewer or shallower trees (`--max-depth`, fewer rounds), not fewer features.
- **Peak RAM is about 4x the model file**: 29.6 MB of JSON gives 110 MB, and 50.7 MB gives 203 MB.
  That peak is parsing the model's JSON at startup, not steady-state use. A compact binary model format
  would remove it.
- **Memory grew for the whole run.** RAM went from 27 MB to 50 MB over 3 minutes, and the packets kept
  grew without limit, because the old accumulator stored every packet of every flow.
  - An orphaned `live-router` from an interrupted idle test kept running for 5.8 hours and peaked at
    **462 MB** of RSS, on a router with about 1 GB and no swap.
  - That run is why `live-router` now has `-max-duration`. The eval script always sets it, so a
    leftover process stops itself.

### Selective feature extraction

Added after these runs. With a Go-native model, `live-router` now computes only that model's features,
plus four cheap diagnostic columns. `-all-features` restores the old behavior.

- Statistics a model needs only the min/max/mean of are kept as running values, with no buffer.
- Retransmission/RTT tracking is updated incrementally, or skipped when the model uses none of it.
- Output is identical to the old accumulator: every feature value (difference 0.0) and every label
  matched, across 4 models × 7 recorded sessions.

Benchmark on the Mac, packets streamed one at a time as on the router, over three sessions (Twitch,
YouTube over TCP, YouTube over QUIC):

| Extraction | CPU per packet | Memory held after the session |
|---|---|---|
| Old (all features, full flow history) | 0.76–3.77 µs | 25–57 MB, growing |
| Selective, option C (22) | 0.22–0.57 µs | 0.1–10.7 MB |
| Selective, option D (8) | 0.16–0.17 µs | 0.02–0.06 MB |

- Option C still needs per-flow sequence state, because two of its features are `rtt_ms_min` and
  `rtt_ms_max`. That state reached 10.7 MB on the YouTube-over-TCP session.
- A 20-feature option C without RTT would have option D's memory profile. Its accuracy has not been
  measured yet.

## Other live observations

- **Mac, before the router.**
  - With YouTube playing, the AutoGluon `XGBoost_r22` bag (110 features, through the Python sidecar)
    labeled 11 of 13 windows as YouTube, mostly `youtube_quic`.
  - Both misses were low-confidence `netflix` predictions (0.56 and 0.45).
- **Idle traffic always gets a label.** The model is closed-world and always picks one of its 5
  classes. Idle windows get about 0.5 confidence, and active video windows get 0.95 to 1.0, so a
  confidence or minimum-activity threshold should come before reporting a label.

## Next

1. Rerun all four models with the current binary: direction rule, selective extraction and CPU fix.
   Run each 3 times or more, so you can report a mean and spread.
2. Confirm that upload stays far below download in every run (the direction check). Confirm that
   option D keeps 0 packets with flat memory.
3. Run `--disable-quic` to test `youtube_tcp`, `--service twitch` to test Twitch, and `--service idle`
   for a baseline.
4. Train option C without RTT using `--final` with a 20-feature list. Measure its accuracy and memory.
5. Apply the port-direction rule in `flow_tracker.py`, regenerate the dataset, and retrain, so that
   training and deployment compute direction the same way.
6. Convert the model to a compact binary format, to remove the 4x JSON-parsing peak at startup.

## Reproduce

```bash
# Mac, repo root: build and deploy
./deploy_router.sh
MODEL=XGBoostModels/10s_no_applayer/all.json ./deploy_router.sh

# Router: packet merging off (reverts on reboot)
ethtool -K eth1 gro off; ethtool -K lan2 gro off

# Linux box, ~/CSC-5991
python src/live_capture/stream_and_check.py --service youtube \
  --url 'https://www.youtube.com/watch?v=eRsGyueVLvQ' --minutes 3 \
  --router root@192.168.1.1 --model model/final_option_c.json
```
