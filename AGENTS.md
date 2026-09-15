# CSC-5991 project instructions

## Purpose and long-term goal

This thesis investigates lightweight machine-learning classification of network activity without deep packet inspection. Later compare classification accuracy, model size, memory usage, and prediction latency. The current scope is collecting and validating labeled PCAP sessions, not implementing ML training.

Start with the existing Tranco CSV and the researched top-100 assessment. Eventually assess the top 100,000 domains by numeric rank, retaining failures and unknowns rather than guessing. Preserve the original Tranco data and research provenance. A ranked domain can be infrastructure, a redirect, an API, or inaccessible; it need not be a usable website.

## Architecture and commands

Keep production Python in `src/`, session configurations in `plans/`, tests in `tests/`, and reference notes in `docs/` and `analysis/`. `src/` is organization, not access control. Do not create skills yet; the user wants those later.

Six entry points use one shared sequential capture runner:
- `python src/collect_browsing.py`
- `python src/collect_social.py`
- `python src/collect_video.py`
- `python src/collect_audio.py`
- `python src/collect_downloads.py`
- `python src/collect_conferencing.py` (optional; requires setup)

Default runs are 600 seconds total, maximum 30 seconds per session. A total budget is never interpreted as 10–15 minutes per domain. Run these commands one at a time; parallel captures contaminate labels. Use `--dry-run`, `--once`, and short budgets for checks. `assess_domains.py` is a separate resumable DNS/HTTP inventory, not a capture or classification command.

## Labeling rules

Folders: web_browsing, social_media_browsing, video_streaming, audio_streaming, file_download, video_conferencing. Social browsing also records parent activity web_browsing and subtype social_media. This overlap must be considered in later model evaluation.

Label the performed and verified action, never the site's capabilities or topic. A video site's homepage is browsing. A DNS/HTTP response is reachability evidence, not activity proof. Social browsing needs visible posts/feed content and media suppressed. Video and audio require advancing media playback; a seek, lobby, login page, autoplay failure, or absent media must not pass. Downloads require a successful completed response and minimum bytes. Conferencing requires a controlled call and observed media; keep unavailable setups unavailable.

Store accepted captures only in the verified category. Keep failed/partial captures in dataset/failed with actual_activity null. Record domain/rank, source scope, actions, evidence, timestamps/duration, status/reason, capture path, interface, filter, and snapshot length. Never write authentication cookies, tokens, passcodes, or private meeting URLs into logs, metadata, or diagnostics.

Cookie values may be handled only in memory, only via Selenium/CDP `get_cookies()` / `add_cookie()`, and solely to carry the user's own already-authenticated session (established through `login_setup.py`) into a fresh automated session so login state survives Selenium's automation fingerprint. Never decrypt Chrome's on-disk cookie store directly (no reading `Cookies` SQLite files, no OS-keychain access) — only ever read cookies back out of a live, already-authenticated browser session through the browser's own API. A cookie value must never be written to disk, printed, or included in any log/metadata/diagnostic output; it exists only for the duration of the in-memory transfer.

## Collection and evaluation limits

Use Selenium Chrome plus tcpdump, with timeouts and cleanup on exceptions/SIGINT/SIGTERM. Bound child processes as well as individual browser commands. Do not run Chrome as root. Use fresh profiles normally. Dedicated persistent Zoom profile work is unfinished; do not claim the current generic conferencing adapter supports automatic Zoom login/join or camera-off verification.

Interface PCAPs may contain other apps, DNS, advertising, CDNs, and OS traffic. Session labels do not establish packet-by-packet attribution. Headless browsing, disabled autoplay, short demonstration media, looped clips, buffered playback, and cold caches change representativeness. Separate external demo fixtures from top-100 or top-100K research samples using metadata. Real sites may need login, current selectors, suitable media URLs, or regional access. Do not bypass access controls or fabricate success. Session-cookie transfer (see Labeling rules) moves the user's own already-granted login into the automated session; it does not defeat 2FA, CAPTCHA, or device-verification challenges. A site that still blocks or challenges after cookie transfer remains a valid, recorded failure, never something to force past with stealth/anti-detection tricks.

Scale assessment with bounded concurrency, rate spacing, process deadlines, on-disk resume state, and explicit failure reasons. Classifications remain proposals until reviewed and paired with an executable activity plan. Do not run 100K active browser captures just because 100K domains passed DNS.

## Maintenance

Use readable Python, four-space indentation, and a consistent formatter. Check syntax, run `python -m unittest discover -s tests -v`, and dry-run all six commands. Test label rejection, partial/empty captures, timing, and cleanup rather than just mirroring implementation. Distinguish offline/simulated tests from real browser/network verification in reports.

The user authorized clearing the old generated dataset for the September 2026 refactor. That is a one-time reset, not ongoing permission to delete future captures. Preserve source lists, analyses, login profiles, and unrelated files. Future resets need an explicit user request.

The user authorized the in-memory cookie-transfer exception above on 2026-09-15, on their professor's advice, after `--use-login-profile` alone proved insufficient (X/Instagram walled the persistent profile once Selenium drove it, despite valid cookies). This is a narrow exception to the secrets-handling rule, not a broader relaxation of it: still no on-disk cookie decryption, still no persisted/logged cookie values, still no CAPTCHA/2FA bypass.
