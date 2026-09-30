# CLAUDE.md

This file orients Claude Code sessions in this repo. **[AGENTS.md](AGENTS.md) is the
authoritative project-instructions file** (Codex convention) — read it in full before
doing any nontrivial work here. This file is a shorter map on top of it.

## What this project is

A CSC-5991 thesis: lightweight ML classification of network activity *without* deep
packet inspection. The current phase is only data collection — capturing labeled PCAP
sessions across six activity categories. No ML training exists yet.

## Layout

- `src/` — six capture entry points (`collect_browsing.py`, `collect_social.py`,
  `collect_video.py`, `collect_audio.py`, `collect_downloads.py`,
  `collect_conferencing.py`), sharing `collector_core.py`, `browser_session.py`,
  `browser_profiles.py`, `evidence.py`, `config.py`. Plus `build_plans.py` (merges
  `analysis/current_origins_verified.csv` into `plans/*.json`, additive-only),
  `assess_domains.py` (cheap DNS/HTTP inventory, not classification),
  `assess_current_origins.py` (a separate, still-unfinished large-scale origin
  classifier with a known `<video>`/`<audio>`-tag-gating bug — treat its output as
  provisional, not as a replacement for the verified CSV), and `login_setup.py`
  (manual login profile setup plus session-cookie capture for `--use-login-profile`).
- `plans/*.json` — per-activity session plans (domain/url/verification config) fed to
  the collectors. See `docs/PLANS.md` for the field schema. Rebuilt from
  `analysis/current_origins_verified.csv` via `build_plans.py`; entries proven
  through real live testing (e.g. `instagram.com`, `youtube.com`) are hand-kept with
  `rank` stripped since it would otherwise be misleading next to the CSV's bucketed
  ranks.
- `dataset/` — output: `dataset/<activity>/*.pcap` for accepted captures,
  `dataset/failed/` for rejected/partial ones (never a training label),
  `dataset/metadata/` for per-session JSON + tcpdump logs, `dataset/sessions.jsonl`
  as the combined index. Generated data — gitignored (`dataset/**`).
- `analysis/` — `analysis/README.md` documents the full domain-categorization
  methodology and every cleanup pass; `analysis/current_origins_verified.csv` is the
  current evidence-based source (~12,300 origins: `category`, `login_required`,
  `basis`/`note` per row), built from real browsing-history origins rather than raw
  Tranco rank. This is the sole input to `build_plans.py`. Explicit/adult content is
  excluded outright, not just deprioritized.
- `docs/PLANS.md` — plan JSON field reference. `docs/VALIDATION.md` — test/validation
  status and its limits (offline vs. live). `docs/LINUX_SETUP.md` — running headless
  on Linux: Chrome binary path, tcpdump capabilities, and the two ways to get
  login-gated sites (Instagram, X, LinkedIn, Amazon, etc.) working without a display.
- `tests/` — offline unit/fixture tests (`python -m unittest discover -s tests -v`).
- `tranco_GQNVK.csv` — source Tranco top-list, preserved unaltered; no longer feeds
  the plans pipeline (see `analysis/` above).
- `collect_loop.sh` — loops the five non-conferencing collectors sequentially and
  indefinitely, one `--once` pass per category with a `--total-seconds` budget sized
  to that category's real plan length. Accepts `CHROME_BINARY`/`INTERFACE` env vars
  for non-Mac setups instead of editing the script.

## Rules worth not forgetting

- Run the six collectors **one at a time**, never in parallel (label contamination).
  `collect_loop.sh` already respects this — prefer it over manual per-category runs.
- A domain resolving or a homepage loading is not proof of the labeled activity —
  each activity has strict, verified pass criteria (see AGENTS.md "Labeling rules").
- Explicit/adult content is hard-excluded from `analysis/current_origins_verified.csv`
  and `plans/*.json` — a safety constraint, not a traffic-classification call. Never
  re-add such origins.
- Login-gated sites need `--use-login-profile` plus a one-time `login_setup.py` run.
  Every collector run, including `--use-login-profile`, is headless by default —
  only `login_setup.py` itself needs a real display, since login state can only be
  confirmed by a human watching actual page content.
- `assess_domains.py` is cheap DNS/HTTP inventory only; it does not classify activity
  or imply browser usability, and 100K domains passing it is not license to run 100K
  live captures. `assess_current_origins.py` is a separate, unfinished classifier —
  don't treat its output as equivalent to the verified CSV.
- Never log/read cookies, tokens, passcodes, or private meeting URLs. The one
  narrow exception (in-memory cookie transfer for login persistence) is scoped and
  documented in AGENTS.md — don't extend it further without the same rigor.
- Conferencing (`collect_conferencing.py`) is optional/unfinished — no automatic
  Zoom login/join or camera-off verification exists; don't claim otherwise.
- Dataset resets are one-time, explicitly user-authorized events, not standing
  permission — always ask before deleting captured data.
- No skills for this project yet — the user wants those added later, not now.

## Maintenance expectations

Four-space indentation, Black formatting (`pyproject.toml`, line-length 100),
`python -m unittest discover -s tests -v` before calling something done, and a
dry-run of any collector command touched. Distinguish offline/simulated test results
from real browser/network verification when reporting status — see the phrasing
style in `docs/VALIDATION.md`. See `docs/LINUX_SETUP.md` before assuming anything
about running collection off the primary Mac.
