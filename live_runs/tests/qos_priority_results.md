# Classifier-driven priority (QoS) on the router — 2026-11-18

The classifier's labels drive real traffic priority on the professor's OpenWrt router. Every run used
`final_shape_streaming` (28 traffic-shape statistics) and a YouTube video played by headless Chromium on
the Linux box behind the router, on the 113-minute video `https://www.youtube.com/watch?v=0xhzwDXfLds`.
The professor approved installing `cake` and adding nftables rules **for test runs only**. The setup is
removed after each session (`qos.sh down`).

Raw run files were deleted after this summary. They are in git history: the YouTube runs at commit
`74203fd` (`live_runs/eval_youtube_20261118_*`), the Twitch runs at `50a7aac`
(`live_runs/eval_twitch_20261119_*`), the 2026-11-21 QoS runs at `c7ac56a`
(`live_runs/eval_{twitch,youtube}_20261121_*`), and the multi-stream runs at `7c9db80`
(`live_runs/eval_youtube_20261121_{154340,155843,164600,175102}*`).

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

## The tagging rule went through four versions

| Version | Rule | What broke it |
|---|---|---|
| 1. Window label | A window with ≥100 packets and ≥0.9 confidence; tag its heaviest connections on 443/80 with the label's transport. | The moment 16 downloads started, the *window* was labeled `netflix` at 0.999, and two downloads were tagged. |
| 2. Own label, 3 windows | Each connection is scored on **its own** packets (its own 28 statistics). Tag after its own label is the same service at ≥0.9, 3 windows in a row. | Downloads still got tagged after ~4 min: a download's own label was ≥0.9 in 31–40 % of its windows. |
| 3. Own label, **5 windows at ≥0.93** | The same, stricter. Chosen by replaying version 2's runs, where it tagged only the video. | At 5 Mbit, rate-limited downloads were labeled `youtube_tcp` at 0.98–1.00 window after window, and were tagged again. On a fast line (2026-11-21), 97 % of the bytes marked for the Video tier were downloads. |
| 4. Version 3 + **a 25 Mbit/s rate cap** (current default) | A window in which the connection carries more than 25 Mbit/s resets its streak (`-priority-max-mbps`). Players fetch at bounded rates; downloads on a fast line don't. | Download tags fell from 5 to 1 on a fast line. On a slow link, downloads slow to video-like rates and pass the cap (16 tagged at 25 Mbit). |

In versions 2–4, a scored window that is unconfident or names a different service resets the
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

## Twitch (2026-11-19): priority, and how shaping changes the traffic

All runs are dry runs (no `--enforce`) on `https://www.twitch.tv/kaicenat`, a live channel at 1080p
(about 8.5 Mbit/s on **one** TCP connection for the whole run; no rotation). Background traffic is
`--light-traffic 60`, starting 60 s in. Shaping is `router/qos.sh` at the given `RATE`.

### 10 minutes, no shaping, no background traffic

| | Result |
|---|---:|
| Window accuracy (playing) | 0.85 (`twitch` 50/59) |
| Video | 1080p throughout, 5 s not playing (startup) |
| The video connection's own label | `youtube_tcp` 1.00 at startup and when it carried 2–6 MB per window; `twitch` 0.97–1.00 at full rate (~10 MB per window) |
| Tags | The video connection at 50 s (as `youtube_tcp`), kept all run. Plus 3 small side connections of the page (0.05–2 MB per window, likely chat/ads/analytics), tagged at 170, 250 and 510 s. |
| CPU / RSS | 2.3 % / 38.9 MB |

- **Wrong service, right decision.** The video was tagged under `youtube_tcp`, but priority only needs
  "this is streaming", which is easier than telling the services apart.
- **Two gaps the side connections expose:** a connection of mostly tiny packets reaches 100 packets
  without carrying data; and since quiet windows don't reset the streak, an intermittent connection
  can collect 5 confident windows over many minutes (one did over 47 windows). Possible fixes, not
  built: a minimum volume per window (e.g., 0.5 MB) to count toward the streak, and "5 of the last 8
  windows" instead of "5 in a row, skipping quiet windows".

### 5 minutes each: shaping × background traffic

The shaping can be read from the data itself: the upload/download packet ratio (the client's ACKs per
download packet) and the smallest download packet gap.

| Shaping | Background | Up/down packets | Video connection's own label | Window accuracy | Video tagged? | CPU / RSS |
|---|---|---:|---|---:|---|---|
| None | none | 0.19 | `twitch` 26/31 (26 at ≥0.93) | **0.93** | yes | 2.7 % / 38 MB |
| None | 60 loops | 0.13 | `twitch` 26/31 (12 at ≥0.93) | 0.86 | yes | 5.0 % / 61 MB |
| **500 Mbit** | none | **0.24** | **`twitch` 26/31** (14 at ≥0.93) | **0.93** | yes | 2.2 % / 38 MB |
| 12 Mbit | none (2 runs) | 0.74 | `youtube_tcp` 32/32, 31/31 | 0.86, 0.57 | yes | 5.1–5.4 % / 38 MB |
| 12 Mbit | 60 loops | 0.52 | `youtube_tcp` 26/32, unconfident | 0.29 | **no** | 9.7 % / 58 MB |

Three more attempts produced nothing: one was stopped about 50 s in, and in two Twitch never loaded.

**What changes under heavy shaping** (Twitch alone, same 8.6 Mbit/s download, median of windows):

| Feature | Unshaped | 12 Mbit `cake` | Change |
|---|---:|---:|---:|
| Upload packets per window | 1,099 | 3,918 | 3.6× |
| Upload packet gap, mean | 9.1 ms | 2.5 ms | 0.28× |
| Upload payload, mean | 24 B | 7.6 B | 0.31× |
| Download packet gap, min | 0.009 ms | 0.021 ms | 2.3× |
| Download bytes | 10.84 MB | 10.82 MB | same |

**Why:** `cake` spreads Twitch's bursts out to the shaped rate, so packets reach the client spaced
apart, and the client ACKs far more often (about 1 per 2 download packets instead of 1 per 7). The
ACK pattern comes from the client, so moving the capture point wouldn't help.

**The model depends on upload features** (52 % of its total split gain; download 27 %, flow counts and
UDP share 21 %), but of two kinds:
- **request size** (`up_packet_size_max`, the top feature at 20 %; `up_payload_size_max`): the client's
  HTTP requests for segments, barely changed by shaping (1.07×);
- **ACK pattern** (`up_byte_count`, `up_packet_count`, `up_throughput_bps`, `up_payload_size_mean`,
  `up_packet_size_mean`, `up_interarrival_*`): changed 0.28–3.6×.

### Findings

1. **Heavy shaping flips Twitch's label** from `twitch` to `youtube_tcp`, in every window. Shaping
   alone still leaves a confident streaming label, so priority still tags the video. Shaping plus
   heavy background traffic made the label unconfident, and nothing was tagged.
2. **At a realistic rate, it doesn't.** A QoS router shapes at about 90–95 % of the line, so the queue
   forms in the router instead of at the ISP. At 500 Mbit (on a ~570 Mbit line), the ACK ratio was
   0.24 (vs. 0.19 unshaped), the label `twitch`, and window accuracy 0.93, the same as unshaped. The
   12 Mbit flip is an artifact of forcing congestion with a low rate. Single run, and fewer windows
   reached ≥0.93 (14 vs. 26).
3. **Background traffic doesn't change the video connection's own label** (unshaped: `twitch` either
   way), only the whole-window label and, somewhat, the confidence.
4. **Twitch didn't need priority against light traffic:** at 12 Mbit with 60 loops it held 8.6 Mbit/s
   and 1080p, without priority. The light requests absorbed the shortfall.
5. **Memory grows with connection churn:** 58–61 MB with 60 loops (~200 connections per window) vs.
   38 MB without. Each connection's statistics are kept for 2 minutes after going idle; dropping them
   when the connection closes would fix it (not built).
6. **For the thesis:** the upload ACK pattern is a fingerprint of the client and the network's timing,
   like the receive window and the QUIC ACK ratio. Future work: a model without the ACK-pattern
   features (keeping request sizes), or training on traffic captured under shaping.

## 2026-11-21: congestion at realistic rates, multi-server downloads, and the rate cap

Twitch runs use `https://www.twitch.tv/hasanabi` (1080p, about 9 Mbit/s on one TCP connection). The
competing downloads were changed during the day: at first all 16 used one speed-test server, which let
only **2** run; from run 4 on they are spread round-robin over 6 servers (Hetzner Ashburn, Hillsboro,
Nuremberg, Falkenstein, Helsinki; OVH), restart if dropped, and report how many are connected. One
further attempt (Twitch never loaded) is excluded.

### Runs

| # | Setup | Video's own label | What was tagged | Video Mbit/s (before → during load) | Quality during load | CPU mean / peak |
|---|---|---|---|---|---|---|
| 1 | 100 Mbit, no load, 5 min | `netflix`/`twitch`, never ≥0.93 (up/down 0.52) | nothing | 9.2 | 1080p | 5 % |
| 2A | 100 Mbit, 16 downloads (only 2 ran, ~83 Mbit/s), dry run | not confident | **2 downloads** (`youtube_tcp` 0.98) | 9.2 → 9.2 | 1080p, 0 s stalled | 42 % / 48 % |
| 2B | same, **enforced** | not confident | **2 downloads** | 9.2 → 9.2 | 1080p, 0 s | 35 % / 41 % |
| 3 | Unshaped, no load, 5 min | `twitch` 30/31 (up/down 0.16) | the video at 140 s | 8.9 | 1080p | 3 % |
| 4 | Unshaped, 16 multi-server downloads (~565 Mbit/s, the full line) | `twitch`, but only 12/32 ≥0.93 | **4 downloads** (19–156 Mbit/s), not the video | 9.0 → 7.4–8.0 | 1080p, 0 s | **102 % / 131 %** |
| 5A | 540 Mbit, 16 downloads from 188 s (~405 Mbit/s), dry run | `twitch`; more confident under load | video at 230 s + **6 downloads** | 9.0 → 7.9 | 1080p, 0 s | 73 % / 116 % |
| 5B | same, **enforced** | `twitch` | video at 140 s + **5 downloads** | 9.0 → 8.2 | 1080p, 0 s | 71 % / 106 % |
| 6 | 5B + **25 Mbit/s cap**, enforced | `twitch` | video at **50 s** + **1 download** (16 Mbit/s) | 9.0 → 7.9 | 1080p, 0 s | 73 % / 110 % |
| 7A | YouTube QUIC, 25 Mbit, 32 downloads from 69 s (~24 Mbit/s, ~15 busy), dry run | `youtube_quic` | 4 video conns + **7 downloads** | 2.2 → 1.35 | 720p, buffer ~110 s | 14 % |
| 7B | same, **enforced** | `youtube_quic` | 5 video conns + **16 downloads** (0.1–12.5 Mbit/s) | 1.6 → 1.34 | 720p, buffer ~110 s | 14 % |

Both YouTube runs had one stall at startup (YouTube's player error, before the load began), not
congestion. Memory stayed at 38–42 MB for Twitch and 46–49 MB for YouTube with 32 downloads.

**Where the Video tier's bandwidth went (5B).** The marking counter after the run read **16.3 GB**
(10.9 M packets). Twitch carried about 0.5 GB after its tag; the 5 tagged downloads, about 330 Mbit/s
for ~380 s, account for ~15.8 GB. So **~97 % of the prioritized bytes were bulk downloads.** (The
counter wasn't recorded for run 6; from its tags, about 1.3 GB would be expected.)

**Replaying runs 4, 5A and 5B with the cap** reproduced their live tags exactly without it, and with it:
download tags 4 → 0, 6 → 0 (one tiny side connection remained), 5 → 1. The cap was checked on the runs it
was designed for; run 6 is its independent test, and it held (1 download, at 16 Mbit/s).

### Findings

1. **The shaping-rate effect holds for this channel too.** Up/down packets: 0.16 unshaped, 0.21–0.22 at
   540 Mbit, 0.52 at 100 Mbit. At 100 Mbit the video's own label never reached 0.93, so it was never
   tagged, while two downloads were.
2. **Congestion upstream of the router hurts the label too.** Unshaped with the full line busy (run 4),
   the queue builds at the ISP, and the video's confidence dropped (12/32 windows ≥0.93). With the queue
   in the router at 540 Mbit (run 5), its confidence went *up* (0.95–1.00), and it was tagged.
3. **Bulk downloads look like `youtube_tcp` at every load level** (0.94–0.99), and fill the Video tier:
   97 % of the marked bytes in 5B.
4. **The rate cap fixes it on fast lines** (5 download tags → 1), and makes the video's tag earlier, but
   **not on slow links**, where a download's fair share is a video-like rate (16 tagged in 7B).
5. **No measurable benefit from priority, because the video was never short of bandwidth.** On the fast
   line, 16 downloads at ~400 Mbit/s leave each flow ~25 Mbit/s, above Twitch's 9. At 25 Mbit, YouTube
   at 720p needed ~1.3 Mbit/s and got it (buffer full in both runs). Priority only matters when a
   stream's fair share falls below its bitrate.
6. **The strict streak delays Twitch.** Its confidence in quiet conditions hovers around 0.77–0.99, so
   dips below 0.93 kept resetting the streak: tagged at 50–230 s across runs.
7. **CPU scales with the packet rate:** ~3 % at 9 Mbit/s, 14 % at 25 Mbit, 35–42 % at ~95 Mbit/s,
   70–73 % at ~410 Mbit/s, and **over one core (102 % mean, 131 % peak) at the full ~570 Mbit/s line**.
   At line rate, capture may drop packets, which would itself skew the features.

## 2026-11-21: several streams at once (per-connection classification)

Each video plays in its own headless browser on the Linux box (`--second-url`, `--third-url`, with
`--second-disable-quic` / `--third-disable-quic` to force TCP). All dry runs, no shaping. YouTube is the
113-minute video; Twitch is `hasanabi` (1080p, ~9 Mbit/s).

**Scoring.** A whole window can only carry one label, so each **busy connection-window** (≥0.5 MB) is
scored instead, against the label expected for its transport (`conn_udp_*`, `conn_tcp_*` in the
summary). With two TCP streams, a TCP connection counts as correct if it carries either TCP label, and
the connections are matched to streams by rate (Twitch ~9 Mbit/s, YouTube ~1.2–1.4). The logs hold no
IPs, so this match is inferred; an owner log built from `ss -tunp` on the Linux box (mapping `flow_id` to
its browser) would make it exact (not built).

| Run | Streams | Each video connection's own label | Tags | Whole-window label | Playback | CPU / RSS |
|---|---|---|---|---|---|---|
| 1 (10 min) | YouTube QUIC + YouTube TCP | QUIC `youtube_quic` **58/58**; TCP `youtube_tcp` **60/60** | the 2 videos (60 s, 150 s) | `youtube_quic` 0.93 (0.98 any YouTube), at `frac_udp` 0.50 | both 720p; 5 s startup | 0.9 % / 38 MB |
| 2 (5 min) | YouTube QUIC + Twitch | QUIC **31/31**; Twitch's main **30/31** | the 2 videos (both 60 s) | mostly `twitch` (20/28) | 720p / 1080p | 3.4 % / 39 MB |
| 3 (5 min) | YouTube QUIC + Twitch + 8 light-traffic loops | QUIC **32/32**; Twitch's main **30/30** | the 2 videos (50 s, 110 s) | mostly `twitch` (21/28) | 720p / 1080p, no stalls | 4.2 % / 41 MB |
| 4 (5 min) | YouTube QUIC + YouTube TCP + Twitch | QUIC **16/16**; YouTube TCP **21/24** (3 × `netflix`); Twitch **30/31** | the 3 videos (190 s, 250 s, 150 s), each under its own label | mostly `twitch` (23/28) | QUIC stalled once and reloaded (50 s); TCP 49/58 polls playing; Twitch 58/59 | 3.9 % / 40 MB |

- **Run 1 is the first live result for YouTube over TCP with `final_shape_streaming`:** 1.00 on its own
  connection, next to another stream.
- **Run 3:** 146 other connections (203 connection-windows) were scored, 87 of them at ≥0.93 with a
  streaming label; **0 were tagged** (most lasted a window or two; one lasted the whole run but never
  held ≥0.93 for 5 windows in a row).
- **Run 4:** the two TCP streams kept their own labels side by side (`twitch` at 8.9 Mbit/s,
  `youtube_tcp` at 1.4). Two long-lived small TCP connections (0.05 and 0.24 Mbit/s, likely page
  connections) were mostly labeled `youtube_tcp`, rarely ≥0.93, and were never tagged. The playback
  stalls are most likely YouTube's player or three browsers on one box: the line was unshaped and carried
  only ~12 Mbit/s.

### Findings

1. **Per-connection classification tells simultaneous streams apart**, across transports and across
   services, including two TCP services at once. Every video connection got its own correct label
   (0.88–1.00), and every stream got exactly one priority tag, with nothing else tagged.
2. **The whole-window label can't describe a mix.** It named the heavier service (Twitch) or, for two
   streams of the same service, still the right one (0.93 at half UDP). It is no basis for priority in a
   multi-stream household.
3. **The cost is delay.** The 5-window streak plus reconnects (a reload opens a new connection, which
   starts over) and confidence dips around 0.93 meant 50–250 s before a stream was tagged.
4. **Cost on the router stays small:** under 5 % CPU and ~40 MB for three streams.

## Open questions (for the professor)

- **False positives on bulk downloads:** (a) accept and state as a limitation, (b) add a heuristic such
  as "one stream per device" (a device with a tagged QUIC YouTube connection doesn't also get TCP
  connections tagged as `youtube_tcp`/`netflix`; it would have blocked every download tag in run 3, but
  it is tuned on run 3), or (c) future work: an "other/bulk" class (open-set classification).
- **Reconnections:** let a new connection inherit priority faster (e.g., 1 confident window) when the
  device already has a tagged connection with the same label and transport.
- **A visible benefit** needs a stream's fair share below its bitrate (e.g., YouTube 720p at ~1.3 Mbit/s
  against 32 downloads at `RATE=8Mbit`, ~0.5 Mbit/s per flow). But there, downloads are slow enough to
  pass the rate cap and get tagged too. Is "priority wasn't needed on a fast line, and can't separate
  video from downloads on a slow one" an acceptable conclusion?
- **Faster tagging for real streams:** the streak is tuned to keep downloads out, and takes 1–4 minutes
  on reloads and confidence dips. A shorter streak once a device already has a tagged stream, or
  "5 of the last 8 windows", would trade some of that safety back.
- **Netflix and Amazon:** need a login and DRM (Widevine), which the headless Chromium likely lacks.
- **Shaping rate in experiments:** congestion forced with a low rate (12 Mbit) changes the TCP
  traffic the classifier sees. Realistic rates (~90–95 % of the line) don't. Forcing congestion with
  load instead of a low rate avoids the artifact.
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
  --light-traffic 8 --competing-after 0            # or: --competing-downloads 16 [--competing-after 180]

ssh root@192.168.1.1 'nft list chain inet csc5991 forward_mark'   # bytes marked AF41 after B
                                                                  # (counts since the last qos.sh up)
ssh root@192.168.1.1 'sh /root/csc5991/qos.sh down'
```
