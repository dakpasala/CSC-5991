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

## Tooling

The professor suggested `dpkt` and `scapy`. In practice, `tshark` (already installed, paired with Wireshark) reads this entire 3.39M-packet file's fields — including frame comments and TCP analysis fields — in under a minute per pass, and its dissectors already implement RTT/retransmission detection correctly for header-only captures. The practical pipeline: use `tshark` to export the needed per-packet fields to a flat file, then use Python (pandas) for session/flow grouping and the aggregate statistics. Reach for `dpkt`/`scapy` for anything tshark's field export can't give directly (e.g. `bytes_in_flight`, segment-burst inference) rather than re-deriving everything tshark already computes correctly.

## Maintenance

`dataset-collection/` holds the entire previous Selenium-based collection phase (its own `AGENTS.md`/`CLAUDE.md` inside describe that phase) — reference only, not active work. Don't resurrect or run anything from it without being asked.

Every claim about this dataset in this file was verified by actually parsing `traffic.pcapng` with `tshark`, not assumed from the professor's description alone — keep that habit. When a new fact is learned about the data (a field's real coverage, a label's real count, a timing quirk), verify it against the file directly and update this document rather than trusting an earlier assumption.
