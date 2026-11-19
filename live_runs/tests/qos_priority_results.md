# Classifier-driven priority (QoS) on the router — 2026-11-18

The classifier's labels drive real traffic priority on the professor's OpenWrt router. Every run used
`final_shape_streaming` (28 traffic-shape statistics) and a YouTube video played by headless Chromium on
the Linux box behind the router, on the 113-minute video `https://www.youtube.com/watch?v=0xhzwDXfLds`.
The professor approved installing `cake` and adding nftables rules **for test runs only**. The setup is
removed after each session (`qos.sh down`).

Raw run files were deleted after this summary. They are in git history at commit `74203fd`
(`live_runs/eval_youtube_20261118_*`).

## How priority is enforced

```
classifier labels a connection ──▶ live-router adds it to an nftables set ──▶ firewall rule marks its
                                                                              download packets DSCP AF41
video packets    ──▶ marked AF41  ──▶ cake (lan2, diffserv4): Video tier, sent first
download packets ──▶ unmarked     ──▶ cake: Best Effort, waits its turn
```

| Piece | What it does |
|---|---|
| `router/qos.sh up` | Installs `kmod-sched-cake` (first time only), and uses the router's own `tc-tiny`. Puts `cake` on `lan2` egress at `RATE` (default 100 Mbit) with `diffserv4 flowblind`. Creates nftables table `inet csc5991`: set `video_flows` (client IP . protocol . client port . server IP . server port, 3-min timeout) and one rule that marks server→client packets of a connection in the set with DSCP AF41. |
| `router/qos.sh down` / `status` | Removes both and touches nothing else (fw4 is untouched). / Shows them. |
| `live-router -priority-enforce` | Turns the dry-run decisions into real ones: `nft add element` on PRIORITY ON, refresh every window, `destroy element` on OFF, flush the set on exit. Refuses to start if the table is missing. |

Design choices:
- **Shaping on `lan2` egress, not WAN ingress:** ingress shaping runs before the firewall, so it would
  never see the marks.
- **`flowblind`:** `cake`'s normal per-connection fairness would protect a light video flow by itself.
  Turning it off leaves one shared queue, like a basic home router, so the only protection is the
  classifier's priority.
- **The Video tier is capped at about half the link**, so a wrongly tagged connection can never starve
  everything else.

## The tagging rule went through three versions

| Version | Rule | What broke it |
|---|---|---|
| 1. Window label | A window with ≥100 packets and ≥0.9 confidence; tag its heaviest connections on 443/80 with the label's transport. | The moment 16 downloads started, the *window* was labeled `netflix` at 0.999, and two downloads were tagged. |
| 2. Own label, 3 windows | Each connection is scored on **its own** packets (its own 28 statistics). Tag after its own label is the same service at ≥0.9, 3 windows in a row. | Downloads still got tagged after ~4 min: a download's own label was ≥0.9 in 31–40 % of its windows. |
| 3. Own label, **5 windows at ≥0.93** (current default) | The same, stricter. Chosen by replaying version 2's runs, where it tagged only the video. | At 5 Mbit, rate-limited downloads were labeled `youtube_tcp` at 0.98–1.00 window after window, and were tagged again. |

In versions 2 and 3, a scored window that is unconfident or names a different service resets the
streak, and a quiet window (the connection has <100 packets) neither counts nor resets it.

### Offline check before building per-connection scoring

Each connection of the 7 recorded sessions was scored on its own packets with the router's own feature
code. (These sessions may be in the training set, so the absolute numbers may be optimistic. The
comparison with whole windows is fair.)

| Session | Whole windows | Main connection alone |
|---|---:|---:|
| YouTube QUIC | 1.00 | 1.00 (99 % of bytes) |
| YouTube TCP ×2 | 1.00 | 1.00, 1.00 |
| Netflix ×2 | 1.00 | 1.00, 1.00 |
| Twitch | 0.98 | 0.94 |
| Amazon | 1.00 | 0.84 (video spread over ~12 connections) |

The dataset's sessions were one service at a time, so a single connection already looks like what the
model learned. No retraining was needed.

## Runs

### 1. 100 Mbit, 16 downloads at 60 s — window-label rule (v1)

| | A: dry run | B: enforced |
|---|---:|---:|
| Downloads tagged | 2 (window 6, `netflix` 0.999) | 2 (window 6, `netflix` 0.999) |
| Video buffer ahead, mean | 76.5 s | 64.3 s |
| Mean resolution | 674p | 689p |
| Not playing | 25 s | 25 s |
| `live-router` CPU | 25.8 % | 24.6 % |

The video stayed at 720p in both. 100 Mbit shared with the downloads still left it enough, so there was
no congestion effect to measure.

### 2. 20 Mbit, 16 downloads at 60 s — own label, 3 windows at ≥0.9 (v2)

| | A: dry run | B: enforced |
|---|---:|---:|
| Video tagged | 44 s | 35 s |
| Downloads tagged | 1 (at 250 s) | 2 (at 210 s, 250 s) |
| Download windows with own label ≥0.9 | 15 / 48 (31 %) | 19 / 48 (40 %) |
| Video Mbit/s, before → during downloads | 2.25 → 1.28 | 2.12 → 1.24 |
| Video buffer ahead, min | 77 s | 78 s |
| Resolution / not playing | 720p / 0 s | 720p / 0 s |
| CPU | 7.4 % | 7.8 % |

- No instant mistake this time: the downloads were tagged only after minutes.
- **Only 2 of the 16 downloads ran** (every window shows exactly 2 TCP connections, about 15–16 Mbit/s).
  The speed-test server probably limits connections per client; this wasn't confirmed.
- The video's rate dropped in both runs only because its buffer was full and YouTube fetched at playback
  speed. It never lacked bandwidth, so priority had nothing to improve.

### 3. 5 Mbit, downloads from 0 s — own label, 5 windows at ≥0.93 (v3)

| | A: dry run | B: enforced |
|---|---:|---:|
| Downloads tagged | 2 (`youtube_tcp` 0.98, 0.999) | 1 (`youtube_tcp` 0.997) |
| Not playing | 5 s | **127 s** |
| Video buffer ahead, mean | 99 s | 14 s |
| Resolution | 720p throughout | 720p / 360p, 4 YouTube errors |
| CPU | 3.6 % | 3.9 % |

B was worse, but mostly not because of priority:
1. **The first failure came before any tag.** At about 40 s, the two downloads took the whole 5 Mbit,
   YouTube showed "Something went wrong", and the page reloaded. In A, the video happened to hold about
   half the link. With one shared queue, this is luck.
2. **Every reload opens new video connections, and each starts its streak at 0.** With 5 windows needed,
   the video waited ~50 s for priority after every reload. It was tagged only at window 12, and that
   connection was replaced a minute later.
3. **A download was tagged and enforced**, and in windows 13–16 the downloads had the link to themselves.

The tuned threshold didn't hold. Throttled to ~1.5 Mbit/s each, a download looks like TCP YouTube. No
confidence threshold separates a rate-limited bulk transfer from a video stream by shape.

### 4. No shaping, 8 light-traffic loops — dry run, 10 min

`--light-traffic 8`: eight loops, each fetching a web page or a 100–300 KB chunk, then pausing 1–5 s.

| | Result |
|---|---:|
| Whole-window label accuracy (playing windows) | **0.22** |
| The video connection's own label | **`youtube_quic` in 54 / 54** busy windows, ≥0.9998 confidence |
| Light connections (scored on their own) | 317 |
| … with own label ≥0.93 (`netflix`, `youtube_tcp`) | 165 (52 %) |
| … lasting more than 1 window | **0** |
| Light connections tagged | **0** |
| CPU / RSS | 1.65 % / 41.1 MB |

- **The whole-window label fell apart:** the light traffic added 5–10 extra HTTPS connections per window,
  about as many packets as the video, and the UDP share fell from ~1.0 to 0.3–0.5. Training sessions
  were one service at a time, so the model never saw a mixed window.
- **The video connection's own label was perfect.**
- **The streak, not the packet threshold, is what kept light traffic out.** Light requests did reach
  100 packets (median 171, max 1,073), and half were labeled confidently. But every one was a fresh
  connection lasting exactly one window.

### 5. 5 Mbit, 8 light-traffic loops — enforced, 10 min

| | Result |
|---|---:|
| Tags | 1, the video's QUIC connection, at 50 s |
| Video / all light traffic / total | 0.78 / 0.81 / **1.6 of 5 Mbit/s** |
| Light throughput vs. run 4 (no shaping) | 0.81 vs. 0.80 Mbit/s: unchanged |
| Resolution / not playing / buffer ahead (mean) | 480p throughout / 5 s / 113 s |
| CPU / RSS | 2.34 % / 41.1 MB |

The link was never congested, so priority had nothing to decide. There is no dry-run counterpart at
5 Mbit yet. YouTube picked 480p at startup under the cap, probably conservatively, and stayed there.

## Findings

1. **Per-connection classification is what makes this work in a real network.** With concurrent
   traffic, the whole-window label dropped to 0.22, while the video connection's own label stayed at
   1.00. The earlier whole-window live results held only because the Linux box was otherwise quiet.
2. **The streak separates short-lived from long-lived traffic.** Everyday requests are never tagged,
   even when half of them get a confident streaming label.
3. **Long bulk HTTPS downloads are the remaining false positive.** On their own, they look like TCP
   video, most of all when rate-limited (`youtube_tcp` at 0.98–1.00). Thresholds delay that; they can't
   prevent it. The model is closed-world: it has no "other" class.
4. **The traffic that needs priority is the traffic the classifier confuses.** Light traffic doesn't
   compete with video (run 5); bulk downloads do, and those are exactly what gets mislabeled. A tagged
   download shares the Video tier with the real video and cancels the benefit.
5. **Cost stays small.** Per-connection scoring adds a few hundred bytes per active connection (RSS
   38 → 41 MB with hundreds of short connections). CPU tracks the packet rate: 1.7–2.3 % with light
   traffic, 3.6–7.8 % at 5–20 Mbit with downloads, ~25 % at 100 Mbit.
6. **No measured video-quality benefit from priority yet.** Either the link wasn't congested (runs 1, 2,
   5), or the comparison was spoiled by luck before priority acted, plus a tagged download (run 3).
   Single runs throughout.

## Open questions (for the professor)

- **False positives on bulk downloads:** (a) accept and state as a limitation, (b) add a heuristic such
  as "one stream per device" (a device with a tagged QUIC YouTube connection doesn't also get TCP
  connections tagged as `youtube_tcp`/`netflix`; it would have blocked every download tag in run 3, but
  it is tuned on run 3), or (c) future work: an "other/bulk" class (open-set classification).
- **Reconnections:** let a new connection inherit priority faster (e.g., 1 confident window) when the
  device already has a tagged connection with the same label and transport.
- **A clean demonstration:** light traffic at `RATE=2Mbit` (below video + light demand, so priority has
  to decide), with and without `--enforce`, repeated 2–3 times.
- Fair queuing (normal `cake`, `fq_codel`) also protects a light video flow without any classifier; a
  reviewer may ask about it.

## Reproduce

```bash
# Mac, repo root
MODEL=XGBoostModels/10s_no_applayer/final_shape_streaming.json ./deploy_router.sh   # also copies qos.sh

# Router setup (test runs only), from the Linux box
ssh root@192.168.1.1 'RATE=5Mbit sh /root/csc5991/qos.sh up'

# Linux box, ~/CSC-5991: A (dry run), then B (add --enforce)
python src/live_capture/stream_and_check.py --service youtube --url 'https://www.youtube.com/watch?v=0xhzwDXfLds' \
  --minutes 10 --router root@192.168.1.1 --model model/final_shape_streaming.json \
  --light-traffic 8 --competing-after 0            # or: --competing-downloads 16

ssh root@192.168.1.1 'nft list chain inet csc5991 forward_mark'   # bytes marked AF41 after B
ssh root@192.168.1.1 'sh /root/csc5991/qos.sh down'
```
