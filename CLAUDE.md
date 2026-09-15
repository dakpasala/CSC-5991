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
  `browser_profiles.py`, `evidence.py`, `config.py`. Plus `assess_domains.py`
  (separate resumable DNS/HTTP inventory tool, not a capture command) and
  `login_setup.py` (manual login profile setup).
- `plans/*.json` — per-activity session plans (domain/url/verification config) fed to
  the collectors. See `docs/PLANS.md` for the field schema.
- `dataset/` — output: `dataset/<activity>/*.pcap` for accepted captures,
  `dataset/failed/` for rejected/partial ones (never a training label),
  `dataset/metadata/` for per-session JSON + tcpdump logs, `dataset/sessions.jsonl`
  as the combined index. Generated data — mostly gitignored.
- `analysis/` — the top-100 Tranco domain research/assessment (`analysis/README.md`,
  `top100_assessment.csv`), done before the collector code existed.
- `docs/PLANS.md` — plan JSON field reference. `docs/VALIDATION.md` — test/validation
  status and its limits (offline vs. live).
- `tests/` — offline unit/fixture tests (`python -m unittest discover -s tests -v`).
- `tranco_GQNVK.csv` — source Tranco top-list, preserved unaltered.

## Rules worth not forgetting

- Run the six collectors **one at a time**, never in parallel (label contamination).
- A domain resolving or a homepage loading is not proof of the labeled activity —
  each activity has strict, verified pass criteria (see AGENTS.md "Labeling rules").
- `assess_domains.py` is cheap DNS/HTTP inventory only; it does not classify activity
  or imply browser usability, and 100K domains passing it is not license to run 100K
  live captures.
- Never log/read cookies, tokens, passcodes, or private meeting URLs.
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
style in `docs/VALIDATION.md`.
