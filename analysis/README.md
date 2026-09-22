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

## Top-10K fetched category proposals (2026-09-21)

Assessed all 9,944 originally unknown rows of `top10k_login_requirements.csv` against the unchanged `tranco_GQNVK.csv` (SHA-256 `0abee43326c3e67f410e15537db48310e055ffc2d2ee2315350b1d2a07eec6fc`). The 56 existing categories and every rank/domain/login_required/basis/note value were preserved. Input inventory SHA-256: `e534a5cf11efbfb8cff7a9cd75d5e5421c1dae35051a16aebc42373af845e027`; output SHA-256: `49fa1e7108d39be021c08b29b3e1c755bcac1405c32af2370fd58ef3d8aa5960`.

**These are conservative planning proposals, not verified activity labels.** HTTP metadata and static media elements do not establish visible feed content, advancing playback, a completed file transfer, or a controlled call. `actual_activity` remains null. Human review and an executable, verified activity plan remain necessary under AGENTS.md. No social or download proposals are inferred merely from platform capabilities.

Extended the existing `src/assess_domains.py` thread pool, launch spacing, isolated subprocess deadlines, source-hash check, and SQLite checkpoints. Each valid domain gets at most one `getaddrinfo` call and one apex HTTPS GET, using the first resolved address with certificate/hostname verification. DNS/TLS failures can prevent the GET. There are no redirects, HTTP fallback, retries, subresource requests, browser sessions, or Selenium. A DNS resolver call may internally perform multiple DNS protocol transactions; the bound here is on application lookups.

The single response body is held in memory up to 65,536 bytes and parsed for title, meta description, og:type, and actual HTML video/audio tags. Comments, scripts, and templates do not supply media tags. Only bounded extracted signals and response outcomes are saved; raw HTML, response headers/cookies, and authentication material are not retained. Missing tags mean absent from the sample, not necessarily absent from the full page. The initial pass accumulated text from encountered title elements; a few saved titles therefore include duplicate or SVG accessibility title text (for example, 9to5Mac). These observations are shown verbatim rather than reconstructed. The final parser uses only the first title element for future probes; no domain was refetched to alter saved evidence. Non-success, compressed, non-HTML, login/challenge, and ambiguous responses remain unknown.

Browsing proposals require article metadata corroborated by reading-related text, or matching news/documentation/encyclopedia/tutorial wording in both title and description, with no sampled media tags or explicit streaming intent. Video/audio proposals require explicit viewing/listening text plus the corresponding raw media tag and no conflicting media tag. Calling/messaging exclusions require explicit product wording without news/review/article/tutorial wording, paired video-chat wording in title and description, or the explicit Russian description phrase meaning “send all types of messages and make calls.” Signals are English-oriented and incomplete; low recall is intentional. A bare og:type=article was rejected as insufficient (for example, Substack and Myspace). Listening, radio, podcast, watching, trailer, streaming, meme, GIF, or feed wording blocks browsing proposals even if news is mentioned (for example, TuneIn and 9GAG). Existing top-100 research is retained separately and never supplies new CSV categories.

Live result timestamps (UTC): 2026-09-21T21:21:44.057683+00:00 through 2026-09-21T21:41:16.435166+00:00. Settings: `--workers 16 --interval 0.1 --timeout 8`; at most 10 launches/second, 16 concurrent child probes, and an eight-second overall child deadline. A top-100 pilot saved 86 unknown-domain results; the top-10K continuation resumed those records without fetching them again. No `--retry-failed` was used. 1,225 responses reached the byte cap.

Local resumable evidence: `assessment/category_signals_20260921.sqlite3` (the existing `assessment/` Git exclusion applies). Results preserve per-domain rank, checked_at, outcomes, extracted signals, proposal reason, signal version, and null actual_activity. Final conservative rules were reapplied offline to saved signals without additional DNS/GET requests.

Commands (from project root):

```sh
.venv/bin/python src/assess_domains.py --category-csv analysis/top10k_login_requirements.csv --limit 10000 --database assessment/category_signals_20260921.sqlite3 --workers 16 --interval 0.1 --timeout 8
.venv/bin/python src/assess_domains.py --category-csv analysis/top10k_login_requirements.csv --limit 10000 --database assessment/category_signals_20260921.sqlite3 --apply-categories
```

### Results

Of 9,944 original unknowns, **38 became non-unknown proposals** and **9,906 stayed unknown**.

| New category | Count |
| --- | ---: |
| web_browsing | 33 |
| social_media_browsing | 0 |
| video_streaming | 0 |
| audio_streaming | 0 |
| file_download | 0 |
| excluded_conferencing | 5 |

Outcomes (not activity evidence): `200`: 1,985, `202`: 12, `204`: 7, `247`: 1, `301`: 3,967, `302`: 647, `303`: 9, `307`: 47, `308`: 97, `400`: 7, `401`: 9, `403`: 464, `404`: 116, `405`: 2, `406`: 6, `411`: 1, `412`: 2, `417`: 1, `418`: 1, `423`: 1, `429`: 8, `445`: 3, `451`: 2, `500`: 6, `502`: 7, `503`: 11, `507`: 1, `520`: 2, `521`: 4, `522`: 1, `526`: 1, `530`: 1, `ConnectionRefusedError`: 19, `ConnectionResetError`: 22, `DNS resolution failed`: 1,629, `OSError`: 9, `RemoteDisconnected`: 5, `SSLCertVerificationError`: 222, `SSLEOFError`: 8, `SSLError`: 140, `SSLZeroReturnError`: 2, `invalid_domain`: 1, `overall_timeout`: 458.

### Concrete fetched evidence for every changed row

All examples below came from the single successful apex HTTPS response, not general knowledge. `video`/`audio` indicate raw elements within the bounded sample.

| Domain | Proposed category | Fetched title | Fetched meta description | og:type | video / audio |
| --- | --- | --- | --- | --- | --- |
| time.com | web_browsing | TIME \| Current & Breaking News \| National & World Updates | Breaking news and analysis from time.com. Politics, world news, photos, video, tech reviews, health, science, and entertainment news. | website | False / False |
| techcrunch.com | web_browsing | TechCrunch \| Startup and Technology News | TechCrunch \| Reporting on the business of technology, startups, venture capital funding, and Silicon Valley | website | False / False |
| people.com | web_browsing | People.com \| Celebrity News, Exclusives, Photos and Videos | Get breaking news and trending scoops on your favorite celebs, royals, true crime sagas, and more. | website | False / False |
| max.ru | excluded_conferencing | MAX — быстрое и легкое приложение для общения и решения повседневных задач | MAX позволяет отправлять любые виды сообщений и звонить даже на слабых устройствах и при низкой скорости интернета. |  | True / False |
| sciencedaily.com | web_browsing | ScienceDaily: Your source for the latest research news | Breaking science news and articles on global warming, extrasolar planets, stem cells, bird flu, autism, nanotechnology, dinosaurs, evolution -- the latest discoveries in astronomy, anthropology, biology, chemistry, climate & environment, computers, engineering, health & medicine, math, physics, psychology, technology, and more -- from the world's leading universities and research organizations. | article | False / False |
| arstechnica.com | web_browsing | Ars Technica - Serving the Technologist since 1998. News, reviews, and analysis. | News and reviews, covering IT, AI, science, space, health, gaming, cybersecurity, tech policy, computers, mobile devices, and operating systems. | website | False / False |
| blog.google | web_browsing | News from Google \| Google Product and Technology News and Stories | Get the latest news and stories about Google products, technology and innovation on News from Google, Google's official blog. | website | False / False |
| variety.com | web_browsing | Variety - Entertainment news, film reviews, awards, film festivals, box office, entertainment industry conferencesVariety – Entertainment news, film reviews, awards, film festivals, box office, entertainment industry conferences | Entertainment news, film reviews, awards, film festivals, box office, entertainment industry conferences |  | False / False |
| abcnews.com | web_browsing | ABC News - Breaking News, Latest News and Videos | Your trusted source for breaking news, analysis, exclusive interviews, headlines, and videos at ABCNews.com | website | False / False |
| indianexpress.com | web_browsing | Latest News Today, Breaking News, India News, Top Headlines \| The Indian Express | Get the latest news today, breaking news, and top news headlines updates from India and around the world. Stay updated on politics, business, sports, entertainment, and more with The Indian Express. | website | False / False |
| phys.org | web_browsing | Phys.org - News and Articles on Science and Technology | Daily science news on research developments, technological breakthroughs and the latest scientific innovations | website | False / False |
| deadline.com | web_browsing | Deadline – Hollywood Entertainment Breaking News | Deadline.com is always the first to break up-to-the-minute entertainment, Hollywood and media news, with an unfiltered, no-holds-barred analysis of events. |  | False / False |
| orf.at | web_browsing | news.ORF.at | news.ORF.at: Die aktuellsten Nachrichten auf einen Blick - aus Österreich und der ganzen Welt. In Text, Bild und Video. |  | False / False |
| gulfnews.com | web_browsing | Gulf News: Latest UAE news, Dubai news, Business, travel news, Dubai Gold rate, prayer time, cinema | Get the latest update on UAE, business, life style, UAE jobs, gold rate, Exchange rate, UAE holidays, Dubai police, RTA and prayer times from UAE’s largest news portal. | website | False / False |
| ew.com | web_browsing | Entertainment Weekly: Entertainment News for Pop Culture Fans | Get your daily dose of the latest TV, movie, music, and book news from Entertainment Weekly, your go-to source for all things entertainment. | website | False / False |
| patch.com | web_browsing | Patch - Everything Local: Breaking News, Events, Discussions | The best breaking news, stories, and events from the Patch network of local news sites | website | False / False |
| laravel-news.com | web_browsing | Laravel News: Tutorials, Packages & Framework News | Laravel News is the official blog of Laravel. Every day bringing you the latest news, tutorials, and packages for the framework. | website | False / False |
| iol.co.za | web_browsing | IOL \| Breaking News, Business, Sports & Lifestyle from South AfricaWatch VideoWatch Video | Stay informed with IOL your trusted news outlet covering stories in South Africa: politics, crime, business, tech, culture, sports, lifestyle & more | website | False / False |
| cointelegraph.com | web_browsing | Cointelegraph Bitcoin & Ethereum Blockchain News | The most recent news about crypto industry at Cointelegraph. Latest news about bitcoin, ethereum, blockchain, mining, cryptocurrency prices and more | website | False / False |
| gamerant.com | web_browsing | GameRant - Breaking News, Reviews & Everything Else in the World of Video Games | GameRant delivers content written by gamers for gamers with an emphasis on news, reviews, unique features, and interviews. | website | False / False |
| trueconf.com | excluded_conferencing | Video Conferencing Software for Secure Communication — TrueConf | Video conferencing software with UltraHD 4K support and great H.323/SIP interoperability. Super fast on-premises deployment in LAN or VPN. | website | False / False |
| wn.com | web_browsing | World News | Latest headlines from WN Network. WorldNews delivers latest Breaking news including World News, U.S., politics, business, entertainment, video, science, weather and sports news. Search News and archives in 80 languages. |  | False / False |
| 9to5mac.com | web_browsing | 9to5Mac - Apple News & Mac Rumors Breaking All Day9to5Mac Logo9to5Google LogoDrone DJ Logo | Apple News & Mac Rumors Breaking All Day | website | False / False |
| tango.me | excluded_conferencing | Tango Live - Live Stream & Video Chat | Tango is your free video chat and live streaming platform to meet new people, stream live, and connect in real time, anytime. Join now and start chatting. | website | False / False |
| wwd.com | web_browsing | WWD – Women's Wear Daily brings you breaking news about the fashion industry, designers, celebrity trend setters, and extensive coverage of fashion week. | Women's Wear Daily brings you breaking news about the fashion industry, designers, celebrity trend setters, and extensive coverage of fashion week. |  | False / False |
| futurism.com | web_browsing | Futurism \| Science and Technology News | Discover the latest science and technology news on breakthroughs that are shaping the world of tomorrow with Futurism. | website | False / False |
| punchng.com | web_browsing | Punch newspapers - Breaking News, Nigerian News & Top Stories | Punch Newspapers homepage - Breaking News, Nigerian News, Nigerian newspapers, Entertainment, Videos, Sports, Business and Politics | website | False / False |
| ome.tv | excluded_conferencing | OmeTV Video Chat — Omegle Random Cam Chat Alternative 2026 | Discover Omegle Alternative to meet new people, make friends, or even find love. OmeTV is a random video chat, offering a seamless mobile app and an exciting global social experience. | website | True / False |
| observer.com | web_browsing | News, data and insight about the powerful forces that shape the world. \| Observer | News, data and insight about the powerful forces that shape the world. | website | False / False |
| medicalxpress.com | web_browsing | Medical Xpress: Health & Medical Research News | Medical Xpress delivers daily medical research news and health breakthroughs across neuroscience, cardiology, oncology, genetics, and more — trusted by millions of readers worldwide. | website | False / False |
| net.hr | web_browsing | Najnovije vijesti iz Hrvatske i svijeta - Net.hr | Pratite najnovije vijesti iz Hrvatske i svijeta. Donosimo teme iz politike, sporta, showa, lifestylea i biznisa na prvom hrvatskom news portalu. | article | False / False |
| appleinsider.com | web_browsing | Apple News, Rumors, Reviews, Prices & Deals \| AppleInsider | For Apple News, Rumors, Reviews, Prices, and Deals, trust AppleInsider. Serving Apple product enthusiasts since 1997. | website | False / False |
| mangakatana.com | web_browsing | MangaKatana - Read Manga Online | Read manga online at MangaKatana, free and updated hourly! | article | False / False |
| flirtify.com | excluded_conferencing | Free Opposite-Sex Video Chat: Random 1-on-1 Video Calls with Single Girls \| Flirtify | Meet hot single girls worldwide on Flirtify’s FREE opposite-sex video chat roulette. Skip lengthy sign-ups and start instantly with an Omegle-style 1v1 HD video call with real-time translation for smooth conversations |  | False / False |
| theweek.com | web_browsing | The Week \| Because the news needs a curator | The Week brings you all you need to know about everything that matters. More than a news digest – it's an original take on world news | website | False / False |
| maxroll.gg | web_browsing | Maxroll - News, Guides & Tools for Diablo 4, Lost Ark, PoE & more | Maxroll - News, Resources, Character Planners & Build Guides for Diablo 4, Diablo 3, Diablo 2, Lost Ark, Path of Exile & Torchlight Infinite. | article | False / False |
| aniagotuje.pl | web_browsing | Ania Gotuje \| Tylko najlepsze przepisy | Blog kulinarny z najlepszymi przepisami i zdjęciami krok po kroku. Proste i pyszne ciasta, serniki i desery. Sprawdzone przepisy na śniadania, obiady i sałatki. Sprawdź! | article | False / False |
| tv9telugu.com | web_browsing | TV9 Telugu News: Latest Telugu News, Breaking News Telugu \| తెలుగు వార్తలు \| News in Telugu \| TV9 Telugu | Telugu News - Get Latest Telugu News, తెలుగు వార్తలు and Live Coverage updates online on Andhra Pradesh (AP), Telangana, Hyderabad, Politics, Crime, Sports, Cricket, Business, Education, Jobs, Entertainment, Technology, Health. TV9 Telugu covers Today’s breaking News, Top Headlines in Telugu Language Online at tv9telugu.com. | website | False / False |

Validation: syntax checks; the full offline unittest suite (22 passed, four opt-in Chrome tests skipped); all six collector dry-runs; CSV preservation assertions for all 10,000 rows. Tests cover one DNS/GET without redirect following, ignored script/comment/template media, ambiguous/challenge rejection, and preservation of prior categories and other fields. These are offline tests plus a real DNS/HTTPS inventory, not live browser or capture validation.

## `current_origins_verified.csv` — a separate, real-visited-traffic source (2026-09-22)

Different provenance from everything above: `analysis/current.csv` (not `tranco_GQNVK.csv`) is a CrUX-style list of real visited *origins* (full `scheme://host` URLs, not bare domains), ranked by bucketed popularity tier (1000, 5000, 10000, ...), not a unique per-row ordinal — many origins share the same rank value. `current_top20k.csv` is the first 20,000 rows of that file (header + data, confirmed monotonically sorted ascending by rank bucket).

**Methodology, in contrast to the Codex pass above:** the earlier pass on `top10k_login_requirements.csv` over-restricted category assignment by requiring structural proof (`<video>`/`<audio>` tag presence) rather than treating title/description as sufficient evidence on its own — that conflated "identify the site's purpose" with "prove the activity technically works," and rejected clear cases like Netflix and SoundCloud for lacking a tag that a bare unauthenticated HTTP fetch on a JS-rendered SPA will never show. This pass corrects that: category comes primarily from real fetched title/meta-description/og:type evidence, with structural signals only as supplementary confirmation, never a gate.

All 20,000 origins were fetched directly (single HTTP(S) GET, 65KB sample, same DNS+HTTP inventory posture as `assess_domains.py` — no browser, no Selenium, no live verification of any activity). For each:
- **Explicit/adult content is dropped from the output entirely** (not merely flagged) — determined from real fetched title/description text, checked across English and numerous other languages/scripts (Japanese, Korean, Thai, Vietnamese, Arabic, Persian, Portuguese/Spanish with accented forms, Chinese euphemisms, etc.), refined iteratively against five ~20-domain spot-check samples per batch of ~5,000 fetched. This is a best-effort, not exhaustive, filter — some explicit content phrased in ways not covered here may still be present in the `web_browsing` bucket, and the reverse (a false positive on borderline/ambiguous terms) is possible though checked conservatively.
- **A domain that fetch genuinely failed for (DNS/HTTP error, timeout) or that returned no usable title/description at all is skipped — not force-labeled `unknown` or defaulted to any category.** This is the honest resolution to "no unknown category allowed": unassessable domains are absent from the file, not mislabeled.
- **`category`** is one of `web_browsing`, `social_media_browsing`, `video_streaming`, `audio_streaming`, `excluded_conferencing` (no `file_download` entries — same reasoning as the Tranco-based pass: this activity needs a specific `download_url`, not derivable from a homepage fetch).
- **`login_required`** (`hard`/`partial`/`no`) is a weaker signal than `category` here: it only catches sites with an *immediate* login wall/redirect in the fetched response (e.g. `accounts.spotify.com` redirecting to `/login`). It does **not** catch sites whose public marketing page loads fine but whose core feature requires an account (e.g. `www.spotify.com`'s own homepage shows `login_required: no` here despite Spotify requiring login for real playback, confirmed elsewhere in this project's live collector testing). Treat `no` as "no obvious wall on this fetch," not "confirmed no login needed."
- A handful of domains (`category_basis: known_platform_override`) are categorized from direct real-world recognition rather than fetched text — used only for universally-recognized major platforms whose marketing page returned blank title/description (a JS-rendered signup redirect, e.g. `primevideo.com`) or off-pattern phrasing a keyword list can't reasonably anticipate. Same precedent as the OnlyFans/Spotify/TikTok overrides in the pass above; deliberately not extended to less-certain domains.

**Final counts, all 20,000 origins processed:**

| Outcome | Count |
|---|---|
| Classified (kept in file) | 12,367 |
| — `web_browsing` | 12,066 |
| — `video_streaming` | 227 |
| — `audio_streaming` | 46 |
| — `social_media_browsing` | 14 |
| — `excluded_conferencing` | 14 |
| Dropped, explicit content | 1,588 |
| Skipped, fetch failed | 4,846 |
| Skipped, no usable evidence | 1,199 |

Domain values in the output all pass the project's domain validator (`[a-z0-9.-]+`, matching `config.py`). 393 duplicate domains exist (same effective host reached via multiple origin URLs, e.g. `m.youtube.com` and `www.youtube.com`); harmless since `build_plans.py` already dedupes by domain when merging into `plans/*.json`.

### Second explicit-content pass (2026-09-22)

Re-scanned the full output against a broadened multi-language explicit-content filter and found 64 rows (58 unique domains) that the original per-batch spot-checks had missed, across several additional languages/scripts not covered earlier. All 64 removed from `current_origins_verified.csv`; the 56 of those domains that had already been merged into `plans/web_browsing.json` (52) and `plans/video_streaming.json` (4) were removed from there too. Verified against known false positives from the same broadened check (Stripe, Sensex/`chartink.com`, the legitimate webcomic `questionablecontent.net`) to confirm they weren't caught. File went from 12,367 → 12,303 rows.
