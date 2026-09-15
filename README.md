# CSC-5991 traffic collection

Six activity collectors share Selenium Chrome, tcpdump, verification, and metadata logging. No ML training is implemented. Persistent project goals are in [AGENTS.md](AGENTS.md), following [Codex's project-instruction convention](https://developers.openai.com/codex/guides/agents-md).

## Setup on your Mac

From this project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Chrome and macOS tcpdump are required. This Mac's `Google Chrome 2.app` is detected automatically; override with `--chrome-binary` if needed. Selenium obtains ChromeDriver on first launch; startup/download time consumes the run budget. `--chromedriver` can supply an existing matching driver.

Your normal user needs BPF capture access (already worked on this Mac outside the Codex sandbox). Check `/usr/sbin/tcpdump -D`. On a new Mac, install Wireshark's ChmodBPF component if needed. Do not run the collector as root. Choose the correct interface; `en0` is the default, and VPNs may change the appropriate interface.

## Run one category at a time

Each command below uses a **10-minute total budget**, with sessions lasting **at most 30 seconds**. Running all five sequentially takes about 50 minutes, not 10 minutes overall. Stop with Ctrl+C.

```sh
python src/collect_browsing.py
python src/collect_social.py
python src/collect_video.py
python src/collect_audio.py
python src/collect_downloads.py
```

Optional, after configuring an actual controlled call:

```sh
python src/collect_conferencing.py
```

First try a short test, or show Chrome to troubleshoot a blocked player/feed:

```sh
python src/collect_video.py --total-seconds 45 --session-seconds 30 --once
python src/collect_social.py --total-seconds 45 --once --headed
python src/collect_audio.py --dry-run
```

Edit the corresponding JSON in `plans/`, or pass `--plan /path/to/custom.json`. `--once` visits each entry once; otherwise eligible entries repeat until the total deadline. Capturing begins after Chrome starts, before navigation. The terminal prints the active capture filename and final status. A project-wide lock prevents overlapping collectors.

## Output

Successful PCAPs go into the matching `dataset/<activity>/` folder. In-progress files initially sit in `dataset/failed/`; failed attempts stay there with no actual-activity label. `dataset/metadata/` contains per-session JSON and tcpdump logs; `dataset/sessions.jsonl` is the combined index. Files include UTC timestamps, domain, and activity.

Social browsing also records parent `web_browsing` and subtype `social_media`. PCAPs are session-labeled; unrelated interface traffic may be included. Close other network-heavy apps. Default snapshots retain the first 128 bytes and original packet length; use `--snaplen 0` for full packets. Snapshotting is not a guarantee of payload-free data.

## Assess Tranco domains

```sh
python src/assess_domains.py --limit 100 --dry-run
python src/assess_domains.py --limit 100
```

DNS resolution and bounded HTTP GET checks are saved in `assessment/domains.sqlite3`. Re-running skips completed domains; `--retry-failed` retries unsuccessful HTTP checks. Defaults: four workers, at least 0.5 seconds between starts, 15-second overall domain deadline, at most 64 KiB of sampled HTTP body. ICMP ping is not used because blocked ping does not imply a website is unavailable. Browser usability is explicitly not tested by this cheap assessment.

The existing 100-domain research supplies proposals, not verified labels. Other domains remain unclassified. **This does not automatically classify 100K sites or generate media actions.** Reviewed activity plans remain necessary.

## What is ready and what needs setup

All six commands and validators are implemented. Public social pages may be login-blocked; the collector must actually find post/feed content to pass. YouTube previously succeeded in 30-second sessions, but playback is not guaranteed. Vimeo is omitted from the default video plan because its earlier playback attempts failed.

Audio and download defaults are external demonstrations, explicitly tagged in metadata. The audio is a short loop, not representative music streaming. The download is a small PDF transferred into browser memory then discarded, not a bulk download or saved document. Replace these with reviewed, representative sources before thesis evaluation.

Conferencing stays disabled until a controlled link, participant setup, join actions, and verified selector are supplied. The generic adapter requires bidirectional video. **The unfinished dedicated-profile/Zoom camera-off work is not integrated**, and there is no automatic Zoom login claim.

See [docs/VALIDATION.md](docs/VALIDATION.md) for test evidence and [docs/PLANS.md](docs/PLANS.md) for plan fields. Source under `src/` is still visible if your repository is public. Generated data and local credentials are ignored by Git.
