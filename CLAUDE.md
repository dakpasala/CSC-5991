# CLAUDE.md

This file orients Claude Code sessions in this repo. **[AGENTS.md](AGENTS.md) is the
authoritative project-instructions file** (Codex convention) — read it in full before
doing any nontrivial work here. This file is a shorter map on top of it.

## What this project is

A CSC-5991 thesis: lightweight ML classification of network activity *without* deep
packet inspection. As of September 2026 the active phase is feature engineering — turning
a professor-provided packet capture (`traffic.pcapng`) into labeled flow-level and
session-level CSVs of traffic statistics (throughput, TCP flags, RTT, retransmissions,
inferred segment behavior, etc.) for four streaming services: YouTube, Netflix, Twitch,
Amazon. No model training exists yet. The prior phase (live Selenium capture of browsing
sessions) is done and archived — see below.

## Layout

- `traffic.pcapng` — the actual data source, 485 MB / ~3.39M packets. Every packet has a
  frame comment `<session_id>,<label>`; see AGENTS.md for the verified label counts,
  protocol makeup, and timestamp caveats before writing any parsing code against it.
- `dataset-collection/` — the entire previous project phase (Selenium collectors, the
  domain-categorization CSV pipeline, `collect_loop.sh`, its own `AGENTS.md`/`CLAUDE.md`,
  everything). Reference only. Don't run or modify anything in here unless explicitly
  asked — it's not part of the active pipeline.
- `src/pcapng_reader.py` — streams a pcapng file's Enhanced Packet Blocks, keeping
  each packet's frame comment (dpkt's own reader discards it; see the module
  docstring for why this reimplements the block loop instead of subclassing).
- `src/packet_parser.py` — parses Ethernet/IPv4/TCP fields into a `TcpPacket`.
- `src/flow_builder.py` — groups packets into `Session`/`Flow` objects (session =
  shared comment ID, flow = 4-tuple within a session). Known limitation documented
  in its docstring: a reused 4-tuple within one session merges into a single flow.
- `src/feature_extraction.py` — the actual feature table (network/transport layers
  exact; application layer inferred from downstream data bursts, since there's no
  payload to read real segment boundaries from — see its module docstring before
  trusting those columns).
- `src/build_dataset.py` — CLI entry point; writes `flow_features.csv` (one row per
  flow) and `session_features.csv` (one row per session: `num_flows` plus median/
  p25/p75 of every flow feature). Read the output CSVs with
  `dtype={"session_id": str}` — session IDs are up to 20 digits and a plain
  `pd.read_csv` will silently corrupt them through float64.
- `src/inspect_session.py` — scapy-based, human-readable dump of one session's
  packets, for spot-checking `build_dataset.py` output against the raw capture.
  (dpkt does the bulk parsing; scapy is only used here, where per-packet
  readability matters more than throughput.)

## Rules worth not forgetting

- **No time-windowing.** The professor was explicit: full-session/full-flow statistics,
  not 1-2 second windows. Don't reintroduce windowing without asking first.
- **No QUIC/TCP split for YouTube.** `traffic.pcapng` has zero UDP/QUIC traffic; the
  professor confirmed YouTube stays a single class. Only revisit this if a different
  capture file with real QUIC traffic shows up.
- **There is no packet payload** (header-only captures, ~73-byte average size) — this is
  a deliberate continuation of the project's no-DPI premise, not a limitation to work
  around. Application-layer "segment" features must be inferred from TCP segment
  lengths/timing, and any such inference needs to be documented as inferred, not
  presented as real HTTP/QUIC-layer ground truth.
- **Don't trust absolute packet timestamps** — they're a synthetic-generation artifact
  (dates far in the future). Relative/elapsed time within a session is fine to use.
- **Verify against the file, don't assume from the professor's description alone.**
  Every fact currently in AGENTS.md about this dataset was checked directly with
  `tshark`/`capinfos`. Keep doing that before writing anything down as fact.
- `tcp.analysis.bytes_in_flight` from tshark is essentially unpopulated in this dataset
  (2 packets out of 3.39M) — compute bytes-in-flight manually per flow instead of
  trusting that field. `tcp.analysis.out_of_order` reads exactly zero file-wide; treat
  that as unresolved (real property of the data vs. a session-interleaving artifact),
  not settled fact.

## Maintenance expectations

No test suite or formatter convention has been established yet for this new phase
(the old phase's `pyproject.toml`/`requirements*.txt`/`tests/` all live under
`dataset-collection/` and don't apply here unless reused deliberately). When real
pipeline code gets written, this section should be updated with whatever testing/
formatting approach is actually adopted — don't invent one preemptively.
