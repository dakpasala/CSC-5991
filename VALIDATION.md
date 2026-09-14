# Validation — 2026-09-14

Implemented after the top-100 assessment was written. The existing source CSV was preserved. No ML code was added.

## Verified

- Source CSV: 4,321,522 rows; numeric top ranks 1–100; 100 distinct domains. Every row in the assessment has an action, feasibility note, uncertainty, and source URL. All 100 HTTPS apexes were attempted through the research browser before collector implementation; those results are not local access tests.
- Six automated tests pass: playback rejection for paused/seek-only/missing media; call rejection without bidirectional video; mandatory meeting setup; failed-capture quarantine even if browser reports success; classic PCAP validation including empty/truncated files; complete top-100 assessment.
- Live headless Chrome **152.0.7977.82**, matching ChromeDriver, Selenium **4.49.0**, Python **3.14**, and macOS tcpdump tested against controlled HTTP fixtures on `127.0.0.1:18765`.
- A 16-second browsing session scrolled and followed the specified second article link. It produced one accepted browsing PCAP.
- A 16-second session played a locally served 30-second WebM test pattern and produced one accepted streaming PCAP with advancing time/frame evidence.
- A missing video failed and produced no labeled streaming PCAP.
- Missing conferencing setup produced an unavailable record and no conferencing PCAP.
- All produced smoke-test PCAPs were readable by tcpdump.
- SIGINT after seven seconds stopped an active 30-second session, exited with status 130, and kept the partial capture unlabelled. Total observed runtime was 7.344 seconds.
- An 8-second total budget with a 30-second session maximum ran only one shortened session; total observed runtime was 6.732 seconds. A normal four-entry smoke pass completed in 35.033 seconds, with two successes, one failure, and one unavailable entry.
- Syntax compilation, plan validation, and the six automated tests passed after the final metadata/readability adjustments.

Synthetic smoke-test PCAPs are deliberately separate from the project's research dataset. Dataset activity directories start empty. The test exercised actual browser traffic and tcpdump, not mocked capture output. It validates collection plumbing and local playback detection, not representative Internet streaming behavior.

## Still requires experiment setup

Install project dependencies in its `.venv`, use the correct Chrome path (this Mac has `Google Chrome 2.app`), and select the intended network interface. Existing user BPF access worked outside the Codex sandbox; the collector does not change permissions. A fresh machine may need ChmodBPF setup.

No full 10–15 minute Internet experiment was run. YouTube/Vimeo URLs were checked through the research browser, but their local headless playback, consent/ads/login behavior, and any regional restrictions remain unverified. Review failures rather than treating them as activity examples.

No actual conferencing call was joined: no private browser meeting link, credentials, joined-call selector, host settings, or second participant was supplied. Generic call evidence is tested at the decision-function level only. Proprietary/worker-based media clients may require an adapter and will otherwise fail closed.

See USAGE.md for isolation, buffering, autoplay, labeling, and snapshot-length limitations. No successful PCAP should be treated as process-isolated or uniformly active for its entire duration without reviewing the evidence timestamps.
