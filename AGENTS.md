# CSC-5991 project instructions

## Purpose and long-term goal

This thesis investigates lightweight machine-learning classification of network activity without deep packet inspection. As of September 2026 the project pivoted from live Selenium-driven traffic collection (archived in `dataset-collection/`, kept only for reference) to feature engineering on a professor-provided capture: `traffic.pcapng`. The goal now is to turn that raw capture into labeled tabular CSVs of flow-level and session-level statistics, which will later feed an ML classifier distinguishing streaming services by traffic shape alone — never by payload content.

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
