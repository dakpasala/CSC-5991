# Session configuration and limitations



Edit `sessions.json` to select from the assessed sites. This short default is a sample, not coverage of all 100 sites. It attempts a public YouTube clip and a Vimeo player; current availability, ads, consent, and headless restrictions can cause failures. Replace URLs with explicit playable clips you can access. Browser selectors can change.

- **Browsing:** visit the URL, verify readable content and non-error HTTP responses, scroll, and navigate explicit same-host `links` every eight seconds. Choose article/documentation links, not sign-out, purchase, download, or other state-changing endpoints. `content_selector` can assert a page-specific content region. Media play calls are suppressed and playback events paused, reducing autoplay contamination.
- **Video:** optionally click configured controls, find `video_selector` (default `video`), request muted playback, then sample current time, decoded/displayed frame counter, ready state, and source identity. At least five seconds of advancing playback and recent progress are required. No element, seek-only movement, stalled player, or failed playback means failure. `frame_selector` optionally selects a player iframe. The collector never silently labels a failed video attempt as browsing.
- **Conferencing:** disabled without explicit controlled meeting setup. A visible joined-call indicator plus advancing incoming/outgoing WebRTC **video** bytes and decoded frames are required. A lobby, local camera preview, or audio-only call cannot pass. It uses synthetic camera/microphone sources; it does not activate the physical camera/microphone.

Each plan item may contain `steps`: ordered objects with `type` (`click`, `wait`, or `fill`) and a CSS `selector`. A `fill` step uses `value_env` naming an environment variable, never a plaintext credential in the plan. Optional `frame_selector` is applied after these steps. Consent/login automation is site-specific; there is no CAPTCHA bypass or general login solver.

### Controlled test call

Prepare a private meeting and a consenting second participant who sends video. Enable browser joining at the host. Supply a **browser client URL**, not a desktop-app launch URL. In an ignored `meeting.local.json` file, add one entry like:

```json
[
  {
    "domain": "zoom.us",
    "activity": "video_conferencing",
    "controlled_test": true,
    "participants_ready": true,
    "meeting_url_env": "COLLECTOR_MEETING_URL",
    "joined_selector": "REPLACE_WITH_VERIFIED_IN_CALL_CSS_SELECTOR",
    "steps": [
      {"type": "fill", "selector": "REPLACE_WITH_NAME_FIELD", "value_env": "COLLECTOR_TEST_NAME"},
      {"type": "click", "selector": "REPLACE_WITH_JOIN_BUTTON"}
    ]
  }
]
```

Configure actual selectors from your controlled client's DOM; these placeholders intentionally cannot pass. Extra login/passcode steps can read separate environment variables. Do not store or commit meeting links or credentials:

```sh
read -s 'COLLECTOR_MEETING_URL?Private browser meeting URL: '
export COLLECTOR_MEETING_URL
export COLLECTOR_TEST_NAME='Traffic experiment'
python collector.py --plan meeting.local.json --once --total-seconds 120 --session-seconds 120
unset COLLECTOR_MEETING_URL COLLECTOR_TEST_NAME
```

The input command above is for macOS zsh. Missing setup creates an unavailable metadata record and no PCAP. No actual conferencing session was tested during development because no meeting/participant setup was supplied. The generic WebRTC hook can miss worker-based or proprietary media stacks; such clients fail closed and need a client-specific adapter. This is not a universal Zoom/Teams integration.

## Outputs and limitations

`dataset/web_browsing/`, `dataset/video_streaming/`, and `dataset/video_conferencing/` contain only captures whose browser evidence and PCAP validation both passed. Files are named with UTC timestamp, unique ID, domain, and requested activity. `dataset/failed/` holds rejected/incomplete attempts and is **not a training class**. Captures begin there and move to a category only after validation. Empty directories are expected when a class cannot be collected.

`dataset/metadata/<session-id>.json` and `dataset/sessions.jsonl` record requested/actual activity, domain, timestamped actions/evidence, start/end/duration, capture times, status/reason, PCAP path, packet count, browser version, interface/filter, and snap length. tcpdump diagnostic logs sit beside session metadata. Queries/fragments are removed from ordinary navigation URLs; meeting paths and filled values are not logged. PCAPs can still contain private network information. `actual_activity: null` means no valid class label, even when some traffic occurred.

One fresh headless profile per session, disabled cache/extensions/background features, startup before capture, and non-promiscuous capture reduce unrelated traffic. Close other browsers and pause sync/downloads during the experiment. **tcpdump records interface traffic, not just the Selenium process.** A dedicated VM/device/network is preferable for stronger isolation. DNS/OS caches and server caches still persist. Headless mode, disabled media during browsing, cold profiles, and fake call devices affect representativeness.

The default 128-byte snapshot retains original packet lengths and packet prefixes; it may truncate headers or payload and does not guarantee payload-free captures. Use `--snaplen 0` for full packets if required. No content is decrypted or inspected by this program. Session PCAPs include navigation/setup and buffering as well as verified activity; timestamps identify the evidence window, but files are not trimmed. Playback progress does not prove each displayed frame was freshly downloaded (buffering/cache), nor does it distinguish an ad from the requested clip. Successful browsing checks are conservative heuristics, not a universal CAPTCHA/login detector. Inspect metadata before treating this initial dataset as research ground truth.

Ctrl+C/SIGTERM stops capture, flushes tcpdump, closes the browser, kills remaining worker-group processes, and logs failure. SIGKILL/power loss cannot run cleanup; partial files remain under `failed/`, and metadata can be incomplete. No automatic retraining, feature extraction, or domain-only labels are produced.
