# Tranco assessment — completed before collector code

Source: `tranco_GQNVK.csv`, 4,321,522 rows, numeric rank sorting, ranks 1–100, 100 unique domains. No newer list substituted. Source SHA-256: `0abee43326c3e67f410e15537db48310e055ffc2d2ee2315350b1d2a07eec6fc`.

`top100_assessment.csv` contains all 100 per-domain assessments and sources. Every apex HTTPS URL was attempted through the research browser on 2026-09-14. These are remote research fetches, not local Selenium tests: fetch denial, sparse rendering, and HTTP errors are evidence of uncertainty, not proof that a site is down. Candidate feasibility is an assessment, not a measured success rate. Broad capabilities are proposals; where exact role could not be supported it is explicitly unknown.

A homepage load is web browsing even when its operator sells streaming/conferencing. A CDN/DNS name is not an independent user activity. Redirects (AWS, Fastly, Blogger, Teams, etc.) must be recorded. Twitter/X and YouTube/youtu.be overlap and should not count as independent services.

The initial experiment retains the three requested categories only. Audio streaming, email, messaging, file transfer, gaming, time synchronization, DNS queries, and interactive AI are meaningful future activities shown where relevant, but lack controlled actions/validation in this proof of concept. No empty extra activity folders imply support. Failure files go into `failed/`, which is not a class.

Use a short, curated session list rather than fitting 100 full sessions into 10–15 minutes. Default: public article browsing, a specific YouTube clip attempt, public documentation browsing, a Vimeo player attempt, and a skipped conferencing record unless the experimenter supplies a controlled meeting. Access failures are valid experiment outcomes, never fabricated class examples.

Additional primary references:
- [Skype retirement](https://support.microsoft.com/en-us/skype/4e034bbd-cb7a-48b7-9f5a-594255f62836): consumer Skype retired May 5, 2025; use Teams only with controlled setup.
- [Zoom web app](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0064261): browser meeting availability depends on host settings.
- [Akamai edge hostnames](https://techdocs.akamai.com/edge-hostnames/docs/edge-hn-terminology).
- [Google Domains migration](https://domains.google/): migrated to Squarespace.

The source CSV was untracked when inspected; it is preserved without alteration. There were no project source files or AGENTS.md instructions in the project or its ancestor directories.

## Top-10K login-requirement pass (2026-09-15)

`top10k_login_requirements.csv` covers ranks 1-10,000 from the same `tranco_GQNVK.csv` (SHA-256 `0abee43326c3e67f410e15537db48310e055ffc2d2ee2315350b1d2a07eec6fc`, unchanged from above). Purpose: flag, ahead of a future larger collection run, which domains are known to gate meaningful browsing behind login — the X/Instagram class of problem worked through during collector development — so those sessions can be planned with `--use-login-profile` and cookie-transfer in advance instead of failing mid-run.

This is a **knowledge-based heuristic pass, not a live-tested assessment.** No browser or HTTP request was made against any of the 10,000 domains for this file. Each row's `login_required` column is one of:
- `hard` — the site's core content (feed, inbox, streaming playback) is not reachable without login, based on general knowledge of the platform.
- `partial` — a meaningful public surface exists (marketing pages, public repos, some articles), but the deeper feature is login-gated.
- `unknown` — not evaluated; this is the overwhelming majority (9,946 of 10,000) and must not be read as "no login required." At this rank range most domains are infrastructure, CDN, ad-tech, or API endpoints rather than browsable consumer sites at all, consistent with the top-100 findings above — but that has not been checked domain-by-domain here.

Only 54 of the 10,000 domains were matched against a curated list of recognizable major platforms (34 `hard`, 20 `partial`). Verifying the remaining ~9,946 would require an actual bounded `assess_domains.py`-style pass or live Selenium checks, which was explicitly out of scope for this pass per the project's own scale guidance (no large live-capture runs just to build a classification list). Treat every `hard`/`partial` flag as a planning aid to try first, and every `unknown` as literally unassessed, not cleared.

### Category column (2026-09-17)

`top10k_login_requirements.csv` now also has a `category` column: one of the six collector activity folders (`web_browsing`, `social_media_browsing`, `video_streaming`, `audio_streaming`, `file_download`), or `excluded_conferencing`, or `unknown`. Same heuristic-pass rules as `login_required` above apply — this is knowledge-based, not live-tested, and only 56 of 10,000 domains were confidently categorized:

- `social_media_browsing` (13) — platforms with a personalized scroll feed (facebook.com, instagram.com, x.com, linkedin.com, pinterest.com, etc.), matching how `social_media_browsing` sessions are actually driven in this project (`content_selector` + suppressed media + scroll loop).
- `video_streaming` (11) — dedicated video/streaming platforms, including short-video apps like tiktok.com — grouped here rather than under `social_media_browsing` to match how Instagram Reels ended up categorized during collector development (video playback verification, not feed scrolling).
- `web_browsing` (24) — landing pages, articles, marketing sites, webmail portals — anything read-only rather than a scrollable feed or active media session.
- `audio_streaming` (1) — spotify.com only; no other top-10K domain was confidently recognizable as an audio-streaming service.
- `file_download` (0) — no confident matches. None of the recognizable top-ranked consumer platforms map cleanly onto the project's `file_download` activity (a direct browser-driven file transfer, not a general "has downloadable content somewhere" site).
- `excluded_conferencing` (7) — zoom.us, skype.com, whatsapp.com/whatsapp.net/web.whatsapp.com, messenger.com, discord.com, telegram.org/web.telegram.org. These are deliberately **not** categorized into one of the five active folders, per the project decision to skip video-conferencing collection entirely right now — distinct from `unknown`, which means "not assessed," these mean "assessed and out of scope."
- `unknown` (9,944) — the overwhelming majority, same caveat as the login-requirement pass: mostly infrastructure/CDN/ad-tech at this rank range, not verified domain-by-domain.
