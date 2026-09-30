# Activity plans

Each command requires a nonempty JSON list whose `activity` matches that command. Each item has `domain`, `activity`, and (except conferencing) an HTTP(S) `url`. Use `rank` for Tranco provenance and `source_scope` to distinguish source lists and external demos. Supplying a domain is not evidence that its content loaded.

Browsing supports explicit same-host `links` and an optional `content_selector`. Social browsing requires `content_selector` pointing to posts/feed content. Login pages must not be labeled social browsing. Autoplay suppression reduces media contamination but does not isolate all background traffic.

Video/audio support `media_selector` (`video`/`audio` defaults), optional `frame_selector`, and `loop`. Timed playback progress must exceed `--min-verified-seconds` (default five), with recent progress at completion. `--stall-seconds` bounds absent/stalled playback. These checks cannot distinguish ads from the intended clip or buffered from freshly downloaded frames. A looping short audio sample is only a plumbing test.

Downloads add `download_url`, on the same origin as `url`. Selenium uses the browser's fetch/stream APIs, verifies HTTP status and completed bytes, rejects HTML error pages, and discards file contents. `--min-download-bytes` defaults to 1024 and `--max-download-bytes` to 10 MiB. This is browser-driven HTTP file transfer, not operating-system download-manager automation.

Optional `steps` are ordered dictionaries with `type` (`click`, `wait`, `fill`) and CSS `selector`. Fill steps require `value_env` naming a local environment variable. No filled values are logged. They run after navigation and before switching to `frame_selector`. Selectors are site-specific and may need maintenance. There is no CAPTCHA bypass or universal login solver.

Conferencing needs `enabled: true`, `controlled_test: true`, `participants_ready: true`, `meeting_url_env`, and `joined_selector`. Join steps must match the controlled client; no camera-off Zoom adapter is integrated. Synthetic camera/microphone flags are used by this generic path. Success requires connected WebRTC peers with increasing inbound/outbound video bytes and decoded frames. A waiting room/local preview/audio-only session must not pass. Worker-based media stacks may not be observable through the hook and will fail closed.

All captures include setup/navigation and associated traffic as well as the verified activity. Metadata provides evidence timestamps, but PCAPs are not trimmed to those windows. Failed files are never training labels. A valid, nonempty PCAP alone proves neither the desired activity nor process attribution.

## Dedicated manual login profile

Run `.venv/bin/python src/login_setup.py` from the project directory. It opens normal Chrome tabs for Google, Instagram, and X. Sign in manually, then press Enter in the setup terminal to close that dedicated browser. No tcpdump, Selenium page logging, cookie inspection, or token extraction runs during setup. Login setup shares the collection lock so collectors cannot run while it is open.

Use `.venv/bin/python src/collect_social.py --use-login-profile --once --total-seconds 65 --session-seconds 30` afterward. All six wrappers accept `--use-login-profile`; it reuses `.browser-profiles/collection`, forces visible Chrome, and records `profile_mode: persistent` without asserting that a login succeeded. Without this option, fresh temporary profiles remain the default. Conferencing remains disabled.

The local profile is excluded from Git and preserved after errors and cleanup. Do not share it or open two Chrome processes against it. Collection still requires verified activity and a valid nonempty PCAP. If a session expires, rerun login setup; never place passwords or JWTs in plans or source files.
