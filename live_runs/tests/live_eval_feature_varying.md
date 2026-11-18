# Live router evaluation: accuracy and cost vs. number of features (2026-11-16)

Four XGBoost models, each run live on the lab router against the same YouTube video. The question:
as the model uses fewer traffic statistics, how do accuracy and router cost change?

These are the first runs with every fix from `live_eval_results.md` in place:

- packet merging (GRO) off at the router;
- headless Chromium kept quiet and undetected;
- port-based flow direction;
- **selective feature extraction**: the router computes only the statistics the loaded model uses;
- a corrected CPU measurement.

Each model was run once. The raw files are in `live_runs/eval_youtube_20261116_{194613,194924,195236,195547}*`.

## Setup

| | |
|---|---|
| Router | OpenWrt 25.12.6, MediaTek Filogic, aarch64 Cortex-A53, ~1 GB RAM, no swap; capture on `lan2` |
| Traffic | Headless Chromium on the Linux box behind the router, playing *Sintel* (`youtube.com/watch?v=eRsGyueVLvQ`) for 3 min |
| Expected label | `youtube_quic` (Chromium streams YouTube over QUIC) |
| Windows | 10 s; 19 per run, 16 scored (after playback start, minus warm-up and the final partial window) |
| Scored on | **Playing windows**: the video position advanced during the window, which excludes YouTube player stalls |

## Accuracy

| Features | Model | Playing-window accuracy | Active-window accuracy | Labeled as any YouTube | Confidence when correct |
|---:|---|:---:|:---:|:---:|:---:|
| 112 | `all.json` | 0.31 (4/13) | 0.25 (3/12) | 1.00 | 0.87 |
| **22** | **`final_option_c.json`** | **1.00 (13/13)** | **1.00 (12/12)** | **1.00** | **0.99** |
| 15 | `rfe_feature_from_no_total_streaming_step06.json` | 0.85 (11/13) | 1.00 (12/12) | 1.00 | 0.99 |
| 8 | `rfe_feature_from_no_total_streaming_step09.json` | 0.88 (14/16) | 1.00 (14/14) | 1.00 | 1.00 |

- *Active window* means at least 100 packets. *Any YouTube* counts `youtube_quic` and `youtube_tcp` as correct.
- **Every model labeled every window as YouTube.** The only mistakes were QUIC vs. TCP.
- **Option C scored 1.00 again.** That makes three separate runs at 1.00: 144347, 191615 and 194924.
- The 15- and 8-feature models were perfect on active windows. Their 2 misses each were quiet windows
  with little video traffic, labeled `youtube_tcp` at lower confidence.

### Why the 112-feature model does worst

With direction now correct (0.58 MB up, 18.4 MB down), `all.json` still labeled most windows
`youtube_tcp`. The cause is **change in YouTube's QUIC traffic since the dataset was recorded**,
and the all-features model depends on exactly what changed:

| | Training data, youtube_quic (2018–19) | Live (2026) |
|---|---:|---:|
| Upload packets per download packet (median) | 0.505 | 0.176 |
| `total_packet_size_p25` (median) | 81 B | 1,292 B |
| `total_interarrival_time_p75` (median) | 0.488 ms | 0.016 ms |

Today's QUIC client acknowledges about once per 6 data packets instead of about once per 2, so far
fewer small packets are mixed into each window. The all-features model leans on mixed-direction
percentile features (`total_*`, `*_p25`, `*_p75`) that encode that ratio. The new pattern lands
outside the training range for QUIC: 85 % of live windows fall outside the training data's 1st–99th
percentile on those features.

The selected models no longer use `total_*` columns or percentiles, so they aren't exposed to this
change. In this test, **feature selection made the classifier more robust to drift**, beyond making it
cheaper. This rests on one run per model and needs repeat runs before it can be stated firmly.

## Router cost

| Features | Columns computed on router | Trees | Scoring time / window | Window close (mean) | Per-packet handling | CPU (mean) | RAM at end | Peak RAM | Seq-tracking entries at end |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 112 | 112 | 1,965 | 2.30 ms | 0.869 ms | 2.77 µs | 0.33 % | 37.6 MB | 109.3 MB | 258 |
| 22 | 24 | 3,625 | 4.14 ms | 0.075 ms | 2.38 µs | 0.35 % | 42.9 MB | 203.5 MB | 129 |
| 15 | 17 | 2,965 | 3.35 ms | 0.079 ms | 2.34 µs | 0.33 % | 38.8 MB | 190.7 MB | 136 |
| 8 | 11 | 3,065 | 3.50 ms | 0.071 ms | 1.87 µs | 0.34 % | 46.4 MB | 199.7 MB | **0** |

- **Columns computed on router**: the model's features plus 4 always-on diagnostic columns (packet
  count, `frac_udp`, max packet sizes), minus any overlap between the two.
- **Window close**: computing the window's features, once per 10 s.
- **Per-packet handling**: flow tracking and updating the running statistics, per packet.
- **CPU**: the `live-router` process, where 100 % is one core.
- **RAM at end**: resident memory after 3 minutes.
- **Peak RAM**: almost entirely parsing the model's JSON at startup.
- **Seq-tracking entries**: per-flow state for RTT/retransmission features. It is 0 when the model
  uses none of those features.

### What the cost numbers show

- **Computing fewer statistics makes feature extraction 12x cheaper**: window close drops from
  0.869 ms with all 112 features to about 0.07 ms with 8–22. Per-packet handling is lowest for the
  8-feature model, at 1.87 µs.
- **The new extraction ended the memory growth.** The same option C model under the old,
  all-features binary closed windows in 3.4 ms and kept **18,299** packets in memory after 3 minutes.
  Now it closes them in 0.075 ms and keeps **129** sequence entries. The 8-feature model keeps
  **none**, because it uses no RTT or retransmission feature.
- **CPU is not a constraint for any model**: about 0.33–0.35 % of one core. At this traffic level,
  that is dominated by packet capture itself, not by features or the model.
- **Scoring time follows the number of trees, not the number of features**: about 1.1–1.2 µs per
  tree. Option C scores slowest because it has the most trees (725 rounds × 5 classes). To make
  scoring faster, train smaller models (`--max-depth`, fewer rounds), not fewer features.
- **Peak RAM (109–204 MB) is model loading, not running.** It tracks the JSON file's size (28–48 MB)
  at about 4x. Steady-state RAM after 3 minutes is 38–46 MB. A compact binary model format would
  remove the startup peak.
- RAM at end still rose during each run (24–31 MB → 38–46 MB), even for the 8-feature model with no
  per-flow state. That is the Go runtime growing its heap between garbage collections, which is
  normal over 3 minutes. Only a longer run (e.g. 1 hour with `-max-duration 1h`) would show whether
  it levels off.

## Playback notes

- Three of the four runs hit YouTube's own player error ("Something went wrong. Refresh or try again
  later.") at 0:40–0:41. The script saved a screenshot, reloaded the page, and playback resumed.
- Those stalled windows are excluded from playing-window accuracy, which is why three runs score
  13 playing windows out of 16.
- No window in any run had a packet over 1,514 B, which confirms GRO stayed off.

## Takeaways for the thesis

1. **A 22-feature, 0-buffer XGBoost model classified live YouTube traffic correctly on a real router
   in three of three runs**, at under 0.4 % CPU and about 43 MB of steady-state RAM.
2. **Using fewer features cut feature-extraction cost about 12x** and removed the per-flow memory
   growth. With 8 features there is no per-flow state at all.
3. **The full 112-feature model was the least accurate live.** Its mixed-direction percentile features
   are sensitive to how QUIC acknowledgments have changed since 2018–19, which the selected models
   avoid.

## Next

1. Repeat each model 3 or more times, so you can report a mean and spread. These are single runs.
2. Test other traffic:
   - `--disable-quic` (YouTube over TCP);
   - `--service twitch`;
   - `--service idle` (a baseline).
3. Train a 20-feature option C without `rtt_ms_min/max`. That would give option C's accuracy with
   option D's zero per-flow state.
4. Use smaller trees (`--max-depth 4–6`), so scoring time and the startup peak shrink.
5. Convert the model to a compact binary format, which removes the 4x JSON-parsing peak at startup.
6. Run one long test (`-max-duration 1h`) to see whether steady-state RAM levels off.

---

# Twitch (2026-11-16, same four models)

`https://www.twitch.tv/tfue` (a live stream), 3 minutes per model, with the same binary and router
settings as the YouTube runs above. The expected label is `twitch`. All 16 scored windows in every
run were playing and active, and no packet exceeded 1,514 B, so capture and playback were clean.

| Features | Model | Playing-window accuracy | Labels given | Mean confidence | CPU (mean) | Seq-tracking entries at end |
|---:|---|:---:|---|:---:|---:|---:|
| 112 | `all.json` | 0.25 (4/16) | youtube_tcp 11, twitch 4, youtube_quic 1 | 0.79 | 2.90 % | 265,902 |
| 22 | `final_option_c.json` | 0.19 (3/16) | youtube_tcp 10, amazon 3, twitch 3 | 0.73 | 1.92 % | 188,870 |
| 15 | `..._step06.json` | 0.06 (1/16) | youtube_tcp 8, amazon 7, twitch 1 | 0.78 | 2.17 % | 204,964 |
| 8 | `..._step09.json` | 0.38 (6/16) | amazon 8, twitch 6, youtube_tcp 2 | 0.83 | 2.23 % | 0 |

## Why every model fails on Twitch

Live Twitch windows compared with the training data's Twitch windows (medians over active windows,
112-feature run):

| Feature | Live Twitch | Training Twitch | Cause |
|---|---:|---:|---|
| `rtt_ms_min` | 0.058 ms | 0.006 ms | **Capture point.** At the client, the dataset's "RTT" is the client's ACK delay. At the router it also includes the LAN round trip. Every live window is outside the training range. |
| `down_psh_count` | 787 | 21 | **Server stack.** Twitch's servers set PSH on about 37x more packets than in 2018–19. |
| `down_recv_window_mean` (option C's top feature, 24 % of gain) | 618 | 363 | **Server stack.** Training Amazon is 575, hence the `amazon` labels. |
| `up_recv_window_max` | 32,245 | 10,091 | **Client OS.** Debian 2026 vs. the 2018 lab machines. |
| `down_byte_count`, `num_flows` | 7.3 MB, 17 | 3.7 MB, 8 | **Service.** Higher bitrate (1080p) and more connections per window. |

The models rely on **fingerprint features**: TCP receive windows, RTT and flag counts. These identify
the servers, client OS and capture point more than the kind of traffic. They were consistent inside
the 2018–19 dataset and have since changed. This is the shortcut-learning risk the feature-importance
analysis pointed to.

YouTube escaped it because it streamed over **QUIC**. On UDP there is no TCP receive window, RTT or
flag, so those features were absent and the models fell back on traffic shape. Twitch runs over TCP,
so every fingerprint feature was present, and each one had shifted.

The heavier TCP traffic also shows in cost: CPU rose to about 2–3 % and sequence-tracking state to
189k–266k entries for every model that uses RTT/retransmission features. The 8-feature model stayed
at 0.

## Next: models without fingerprint features

Two feature lists in `benchmarks/feature_sets/`, both with no buffers and no sequence tracking:

- **`option_c_no_fingerprint.txt` (12 features)**: option C minus receive window, RTT and PSH counts.
- **`shape_streaming.txt` (28 features)**: sizes, counts, bytes, throughput, timing, `frac_udp` and
  flow counts only.

Train each with `--final`, then test live on Twitch and YouTube. The test checks whether removing
fingerprints fixes Twitch without losing YouTube or much offline accuracy. The ablation's
`shape_only` set reached 0.975 offline. The longer-term fix is to collect labeled training windows at
the router itself with today's traffic; the eval script already produces them.

---

# Models without fingerprint features (2026-11-16)

Two models trained with `train_xgboost_ablation.py --final` on the same 2018–19 dataset, without any
receive-window, RTT, TCP-flag, retransmission or out-of-order feature. Each was trained on 10 cores
with `--learning-rate 0.3 --max-rounds 600`. Both were then tested live with the same setup as above.

| Features | Model | Offline test accuracy | Trees | **Live Twitch** (playing windows) | **Live YouTube** (playing windows) |
|---:|---|:---:|---:|:---:|:---:|
| 112 | `all.json` (reference) | 0.983 | 1,965 | 0.25 (4/16) | 0.31 (4/13) |
| 22 | `final_option_c.json` (reference) | 0.980 | 3,625 | 0.19 (3/16) | 1.00 (13/13) |
| **12** | **`final_option_c_no_fingerprint.json`** | 0.953 | 1,490 | **0.69 (11/16)** | **1.00 (12/12)** |
| 28 | `final_shape_streaming.json` | 0.964 | 1,610 | — (playback never started) | **1.00 (14/14)** |

- **Twitch went from 0.19 to 0.69** when option C lost its fingerprint features: 11 of 16 windows
  labeled `twitch`, with 0.90 mean confidence on the correct ones. The misses were `youtube_tcp` (4)
  and `amazon` (1).
- **YouTube stayed perfect** for both models.
- **The cost is offline accuracy**: 0.980 → 0.953 (12 features) or 0.964 (28). The fingerprint
  features scored well on 2018–19 data, and that advantage doesn't carry over to today's traffic.
- The `shape_streaming` Twitch run never started playing: its timeline is empty, and the script gave up
  after 60 s. The script now saves a screenshot and the page text in that case too (`_nostart.png/.txt`).
  Rerun it.

| Model | Window close (mean) | Scoring time / window | CPU (Twitch / YouTube) | Peak RAM | RAM at end | Seq-tracking entries |
|---|---:|---:|---:|---:|---:|---:|
| `final_option_c_no_fingerprint` | 0.064–0.067 ms | 1.8–2.0 ms | 1.67 % / 0.30 % | 110 MB | 34–37 MB | **0** |
| `final_shape_streaming` | 0.071 ms | 1.9 ms | – / 0.31 % | 112 MB | 34 MB | **0** |

Dropping the fingerprints also makes the models cheaper:

- **No per-flow state**: no RTT or retransmission features, so no sequence tracking.
- **About half the trees**, so scoring takes about 2 ms instead of 4.1 ms.
- **A smaller model file**, so the startup peak is about 110 MB instead of 203 MB.

## What this shows

Removing the features that identify *where and from which servers* the traffic was captured turned a
model that failed on today's Twitch into one that mostly works, while YouTube stayed perfect and cost
went down. The remaining Twitch errors are consistent with Twitch's own changes (about 2x the bitrate
and 2x the flows per window), which no feature selection can undo. Closing that gap needs training data
from today's traffic, e.g. labeled windows collected at the router with the eval script.

Each of these is a single run. Repeat runs are needed before the 0.69 is a firm number.

## Update: Twitch rerun and YouTube over TCP

| Run | Model | Playing-window accuracy | Labels | Confidence when correct |
|---|---|:---:|---|:---:|
| Twitch (rerun, played normally) | `final_shape_streaming` (28) | **0.88 (14/16)** | twitch 14, amazon 1, youtube_tcp 1 | 0.91 |
| YouTube over TCP (`--disable-quic`) | `final_option_c_no_fingerprint` (12) | **0.88 (14/16)** | youtube_tcp 14, netflix 2 | 0.86 |

- The TCP run really was TCP: `frac_udp` had a median of 0.005 over active windows.
- Its 2 misses were `netflix`, the youtube_tcp/Netflix confusion that is also the weakest pair in the
  offline test report.
- Neither run had packets over 1,514 B or any per-flow state.
- Steady-state RAM was about 36–37 MB. The fingerprint models on Twitch were at 52–61 MB, because
  they hold sequence state.

### Live accuracy so far (playing windows, single runs)

| Model | Features | Offline test | Twitch | YouTube (QUIC) | YouTube (TCP) |
|---|---:|:---:|:---:|:---:|:---:|
| `all.json` | 112 | 0.983 | 0.25 | 0.31 | – |
| `final_option_c.json` | 22 | 0.980 | 0.19 | 1.00 (×3 runs) | – |
| `final_option_c_no_fingerprint.json` | 12 | 0.953 | 0.69 | 1.00 | 0.88 |
| **`final_shape_streaming.json`** | **28** | 0.964 | **0.88** | **1.00** | – |

**`final_shape_streaming` is the leading candidate.** It is the best model on Twitch, perfect on
YouTube over QUIC, and needs no buffers and no per-flow state. To confirm it: run it on YouTube over
TCP, then repeat runs for every model.

## Router cost on the heaviest traffic (live Twitch, all six models)

The same Twitch stream for every model, at about 6,000–10,000 packets per 10 s window. Values come
from each run's `_perf.csv`, with window 0 (startup) excluded.

| Model | Features | Trees | CPU mean / max | CPU time per 10 s window | Per packet | Window close | Scoring | RAM at end | Peak RAM | Per-flow seq entries | Twitch accuracy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| `all` | 112 | 1,965 | 2.90 % / 15.4 % | 290 ms | 5.24 µs | 10.48 ms | 2.6 ms | 52.9 MB | 116 MB | 265,902 | 0.25 |
| `final_option_c` | 22 | 3,625 | 1.92 % / 2.6 % | 192 ms | 4.39 µs | 0.076 ms | 4.3 ms | 61.4 MB | 204 MB | 188,870 | 0.19 |
| `step06` | 15 | 2,965 | 2.17 % / 2.7 % | 217 ms | 4.44 µs | 0.066 ms | 3.5 ms | 55.3 MB | 190 MB | 204,964 | 0.06 |
| `step09` (D) | 8 | 3,065 | 2.23 % / 2.6 % | 223 ms | 1.88 µs | 0.056 ms | 3.7 ms | 54.3 MB | 200 MB | 0 | 0.38 |
| `final_option_c_no_fingerprint` | 12 | 1,485 | **1.67 %** / 2.3 % | **167 ms** | 1.98 µs | 0.064 ms | 2.0 ms | **37.2 MB** | **110 MB** | **0** | 0.69 |
| **`final_shape_streaming`** | 28 | 1,605 | 1.92 % / 2.5 % | 192 ms | 2.01 µs | 0.062 ms | 2.0 ms | **37.2 MB** | **111 MB** | **0** | **0.88** |

- *CPU time per window* is CPU % × 10 s: the processor time `live-router` used per 10 s window.
- On YouTube (about 900–1,000 packets/window), both models without fingerprints used 0.30–0.35 %
  CPU (30–35 ms per window), 34–36 MB of steady-state RAM, and 0 seq entries.

What this shows:

- **No RTT or retransmission features means no sequence tracking.** Per-packet cost halves (4.4 → 2.0 µs),
  steady-state RAM drops from 52–61 MB to 37 MB, and per-flow state goes from growing (189k–266k entries
  after 3 min) to none.
- **CPU stays under 2 % of one core on average for every current model**, at about 680 packets/s. Once
  per-flow state is gone, what remains is mostly packet capture, so the models differ little on CPU.
- **Scoring time and peak RAM follow tree count**, which comes from the model file's size. About 1,500
  trees gives 2.0 ms scoring and a 110 MB peak, versus 3.5–4.3 ms and 190–204 MB for the 3,000+-tree
  models.
- The all-features model was the only one with CPU spikes (15 % max, 10.5 ms window closes), because it
  computes every column on heavy TCP traffic.

`final_shape_streaming` is the most accurate model live and is tied for the lowest memory: no buffers,
no per-flow state, under 2 % CPU on Twitch, and 2 ms of scoring per window.

---

# Longer run: `final_shape_streaming`, 5 minutes, live per-window output (2026-11-17)

Big Buck Bunny (`youtube.com/watch?v=aqz-KE-bpKQ`, YouTube over QUIC) for 5 minutes. This is the first
run with the per-window live printout and `-exclude-own-traffic`. With that flag, the router ignores its
own traffic, such as the printout's ssh polling. Before it, that traffic left the router as GSO packets of
1,750–2,150 B and tripped the 1,514 B check. Playback was smooth the whole time, at 1280x720 with no
stalls. Raw files: `live_runs/eval_youtube_20261117_154335*`, including the per-window `_live.log`.

| Windows (playing) | Accuracy, all playing windows | Windows with ≥ 50 packets | Windows with ≥ 100 packets |
|:---:|:---:|:---:|:---:|
| 28 | 0.71 (20/28) | **0.95 (19/20)** | **1.00 (16/16)** |

- **Every window that carried real video traffic was labeled `youtube_quic`**, almost all at confidence
  1.00. Mean confidence was 0.97 on correct windows and 0.74 on wrong ones.
- **All 8 misses were near-empty windows**, with 2 to 68 packets. Once its buffer is full, YouTube fetches
  in bursts. 12 of the 28 playing windows held under 100 packets: almost only background traffic, with
  nothing for the model to recognize. The model is closed-world, so it still has to name one of its 5
  classes, and it guesses (`twitch`, `youtube_tcp`, `netflix`).
- **A deployment rule follows from this:** report "no streaming activity" for windows below about 100
  packets instead of a service label. On this run, that turns 0.71 into 1.00 on the windows it labels,
  and it describes the quiet windows honestly. A confidence threshold would catch most of the same
  misses (mean 0.74 when wrong).

| Router cost | Value |
|---|---|
| CPU, mean / max | 0.43 % / 1.23 % of one core |
| Per packet / window close / scoring | 1.78 µs / 0.069 ms / 1.95 ms |
| RSS | 23 MB at start, 36 MB by 1 min, **37.8 MB at 5 min (flat)** |
| Per-flow seq entries | 0 (flows tracked: 72) |
| Packets over 1,514 B | 0 |

**Memory levels off.** RSS rose during the first minute, as the Go runtime grew its heap, and then stayed
at about 36–38 MB through minute 5. The live heap oscillated between 10 and 21 MB with garbage collection.
The only state that keeps growing is the flow tracker's table (72 flows at about 74 B each), which is
negligible. A 20-minute or hour-long run would confirm the plateau over longer timescales.

---

# 16-minute run with dry-run priority decisions (2026-11-17)

`final_shape_streaming`, Big Buck Bunny, `-priority` on. The run was planned for 20 minutes and ended
at about 16. The Linux-box side was interrupted, and the router side shut down cleanly with all
98 windows. Raw files are in `live_runs/eval_youtube_20261117_175548*`; the router's files were
copied back by hand.

| Part of the run | Windows | Median packets/window | Active windows (≥ 100 pkts) | Accuracy on active windows |
|---|---:|---:|---:|:---:|
| Before the restart (network playback) | 63 | 64 | 29 | **1.00 (29/29)** |
| After the restart (replay from **browser cache**) | 34 | 27 | 3 | 0.67 (2/3) |
| Whole run | 97 | – | 32 | **0.97 (31/32)** |

- **After the video restarted (about 10.5 min), Chromium replayed it from its disk cache.** It
  "played" at 720p for 5+ minutes while the router saw almost no traffic, so those windows held
  nothing to classify.
- The eval script now disables Chromium's HTTP cache (`Network.setCacheDisabled`), so a restarted
  video downloads again.
- The single active miss (window 94, 455 packets, all TCP) was labeled `youtube_tcp`: a TCP fetch by
  YouTube during the cached replay. Right service, other transport.
- **Memory: no growth over 16 minutes.** RSS held at 36.8–37.5 MB for 10 minutes, then *fell* to about
  25 MB when traffic dropped, as Go returned memory. CPU averaged 0.27 %, scoring 1.93 ms, and window
  close 0.079 ms.

## Priority decisions (dry run)

| Time | Event | Connection | Why |
|---:|---|---|---|
| 21 s | ON | `d8def81c` (udp/443) | first active, confident window (1.7 MB) |
| 651 s | ON | `0cfa2b94`, `ac9052ef` (udp/443) | the restarted video's new connections |
| 681 s | OFF | `d8def81c` | idle 122 s (the old connection after the restart) |
| 785 s | OFF | `0cfa2b94`, `ac9052ef` | idle 133–134 s (cached replay, nothing downloading) |

- The first connection kept priority for about 11 minutes through YouTube's quiet gaps, with no
  flapping on and off.
- Each tag was released only when its connection had truly stopped carrying traffic.

## Script fixes from this run

- **Chromium's cache is disabled**, so a restarted video downloads again.
- **The browser restarts itself** after a crash or hang, up to 3 times, instead of ending the run.
- **The router's files are collected even if the run is interrupted.** They are copied in the cleanup
  path, not only after a clean finish.

---

# Clean 20-minute run: `final_shape_streaming` on a 113-minute video (2026-11-17)

*Europe 4K – Scenic Relaxation Film* (`youtube.com/watch?v=0xhzwDXfLds`, 113 min), YouTube over
QUIC, 20 minutes, with every fix in place:

- GRO off;
- the router's own traffic excluded;
- the 443/80 + transport filter on priority tags;
- browser cache off;
- crash recovery;
- the Linux box's received bytes logged per window.

The video is longer than the run, so there was **no restart**: every window is real, network-fed
playback. It played at 1280x720 for the whole run, and the Linux box received 162.7 MB.
Raw files: `live_runs/eval_youtube_20261117_195313*`.

| Playing windows | Accuracy, all windows | Accuracy, ≥ 50 packets | Accuracy, ≥ 100 packets |
|:---:|:---:|:---:|:---:|
| 119 | 0.975 (116/119) | **1.000 (116/116)** | **1.000 (116/116)** |

- **Every one of the 116 windows with real traffic was labeled `youtube_quic`.** Over 20 minutes,
  that's no misses on active traffic.
- The only 3 misses were nearly empty windows. This video streams more continuously than Big Buck
  Bunny, so only 3 of 119 windows were quiet, against about 40 % for Big Buck Bunny.
- No packet exceeded 1,514 B.

**Priority (dry run): one connection, the whole run.** The QUIC video connection (`be70f1f5`, UDP/443)
was tagged in window 1 (2.2 MB) and kept priority for all 20 minutes. There were no other tags, no
released tags, and no SSH or other stray connections. That's the intended behavior.

| Router cost (20 min) | Value |
|---|---|
| CPU, mean / max | 0.37 % / 0.94 % of one core |
| Per packet / window close / scoring | 1.71 µs / 0.075 ms / 1.93 ms |
| RSS | 21.8 MB at start, then **37.2 MB from minute 3 to minute 20 (flat)** |
| Peak RSS | 111.6 MB (startup model parsing) |
| Per-flow seq entries | 0 (flows tracked: 98) |

**Memory is flat for 20 minutes.** After the first few minutes, RSS sits at 37.2 MB, with no
growth. This is the steady-state answer: the router deployment uses a constant ~37 MB on a router
with about 1 GB.

## Earlier long runs, explained

In the three earlier 20-minute runs on Big Buck Bunny (10.5 min long), almost no traffic reached the
**Linux box itself** after the video restarted: `host rx` was 0.0 MB per window while the video
"played" at 720p. The router wasn't missing anything; there was nothing to capture. That happened even
with the HTTP cache off and with a fresh browser. A fresh browser started on its own downloads normally
(1–4 MB per 10 s), so the post-restart "playback" wasn't a real stream. The cause is still unexplained.
Using a video longer than the run avoids the problem entirely. Priority handled those periods
correctly: with no traffic, the tags were released after about 2 minutes idle.
