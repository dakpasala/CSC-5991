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
