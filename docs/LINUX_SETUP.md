# Running this on a headless Linux box

This project was built and tested on macOS. Running it on Linux works the same
way in principle, but a few things need attention: the Chrome binary path is
hardcoded for macOS, packet-capture permissions work differently, and there's
no display server to speak of. This doc covers what changes, and the two ways
to get login-gated sites (Instagram, X, LinkedIn, Netflix, etc.) working.

**Read [AGENTS.md](../AGENTS.md) first** if you haven't — it's the actual
project-instructions file (label rules, scale limits, what "success" means for
each activity). This doc is just the Linux-specific mechanics on top of it.

## Getting the code

Use `git clone`, not a zip of the project folder. `.gitignore` excludes the
local Chrome profile (`.browser-profiles/`), the Python virtualenv (`.venv/`),
and generated capture data (`dataset/*`) — but that only applies to what git
tracks. A raw folder zip would include all of it, including live, real
session cookies for whatever accounts are logged in on the sending machine.
Don't transfer the project that way.

```sh
git clone <repo-url>
cd CSC-5991
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Chrome / chromedriver on Linux

`src/browser_profiles.py`'s `CHROME_BINARY` constant and `collector_core.py`'s
`--chrome-binary` default both point at a macOS app bundle path
(`/Applications/Google Chrome 2.app/...`), which doesn't exist on Linux.
**Every command needs `--chrome-binary` passed explicitly**, e.g.:

```sh
--chrome-binary /usr/bin/google-chrome
# or, if using Chromium instead of Chrome:
--chrome-binary /usr/bin/chromium-browser
```

Selenium Manager resolves a matching `chromedriver` automatically based on
that binary's version — same mechanism used throughout this project, no
separate chromedriver install should be needed unless you want to pin one
explicitly via `--chromedriver`.

`login_setup.py` and every `collect_*.py` script accept this flag.

## Network interface and packet capture (tcpdump)

The interface default is `en0` (macOS). On Linux it's typically something
like `eth0`, `ens3`, `wlan0`, etc. — check with `ip link` and pass
`--interface <name>` to every collector command.

tcpdump needs raw-socket capture permission. The collector explicitly refuses
to run as root (`Run as your normal user with BPF access, not root`), so
don't `sudo` the whole thing. Instead, grant the `tcpdump` binary itself the
capability it needs:

```sh
sudo setcap cap_net_raw,cap_net_admin=eip $(which tcpdump)
```

Verify with `getcap $(which tcpdump)`. If tcpdump still can't open the
interface after that, you may need your user in the right group (varies by
distro) — check `tcpdump -D` runs without `sudo` first, same sanity check
the macOS README already recommends.

## Headless is already the default — no display needed for collection

As of this doc, `--use-login-profile` no longer forces a visible browser
window (it used to, back when login state could only be checked by a human
watching). Every collector run — with or without `--use-login-profile` — uses
`--headless=new` by default unless you explicitly pass `--headed`. So the
actual data-collection step (`collect_browsing.py`, `collect_10k_loop.sh`,
etc.) needs **no display server at all** on the Linux box.

The one piece that *does* need a real, visible browser is logging in in the
first place — covered next.

## Getting login-gated sites working: two options

Sites like Instagram, X, LinkedIn, Netflix, etc. need an authenticated
session. That session lives in two files/directories, both under
`.browser-profiles/`:
- `collection/` — the actual Chrome profile (cookies, local storage, etc.)
- `session_cookies.json` — a cookie snapshot captured at login time, which
  the automated collector re-injects into every session (`--use-login-profile`)
  to survive Selenium's automation fingerprint being detected by some sites
  even when the profile itself is already authenticated.

### Option A: Use the student's credentials

The student runs `login_setup.py` on their own machine (a Mac, or any
machine with a real display), logs into whatever's needed, and transfers the
resulting `.browser-profiles/` directory to you over a **secure** channel —
`scp`/`rsync` over SSH, not email or a public link. It contains live session
cookies for real accounts; treat it like a credentials file, because it is
one.

```sh
# from the student's machine
rsync -avz .browser-profiles/ you@linux-box:/path/to/CSC-5991/.browser-profiles/
```

Then on the Linux box, everything just works with `--use-login-profile`,
using the student's authenticated session.

**Caveat — this goes stale.** Several cookies involved (Cloudflare's
`__cf_bm`, Google's `SIDTS`/`SIDCC`, X's `ct0`) are short-lived, server-side
rotating security tokens. Observed behavior: sessions work fine same-day,
and start failing (`login_prompt: true` showing up in the failure metadata)
around ~24 hours later, regardless of what the cookies' own client-side
`expiry` field claims. If you're running collection across multiple days,
you'll need the student to periodically re-run `login_setup.py` and resend
a fresh `.browser-profiles/` — there's no way for you to refresh it yourself
on a headless box without a display.

### Option B: Use your own credentials

This avoids the resend problem entirely, at the cost of needing a display
*once* to log in. Two ways to get that:

1. **Temporary GUI access to the Linux box itself** — VNC, X11 forwarding
   over SSH (`ssh -X`), or a monitor plugged in once. Run `login_setup.py
   --chrome-binary /usr/bin/google-chrome` directly there, log in through
   whatever window appears, press Enter in the terminal when done.
2. **Log in on a different machine with a display**, then transfer your own
   resulting `.browser-profiles/` to the Linux box the same way as Option A —
   just using your own accounts instead of the student's.

Either way, once you're logged in, your own copy goes stale on the same
~24h-ish timeline as anyone else's — but since it's *your* session, you can
refresh it yourself (rerun `login_setup.py` wherever you have display access)
without depending on the student to resend anything.

## Running the actual collection

Once login is sorted (or skipped, for domains that don't need it):

```sh
# one category, one pass, dry-run first to sanity check the plan
python src/collect_browsing.py --chrome-binary /usr/bin/google-chrome \
    --interface eth0 --dry-run

# the real thing
python src/collect_browsing.py --chrome-binary /usr/bin/google-chrome \
    --interface eth0 --use-login-profile --once \
    --session-seconds 20 --total-seconds 4000
```

Or use `collect_10k_loop.sh` for the full continuous sweep across all five
categories (`web_browsing`, `social_media_browsing`, `video_streaming`,
`audio_streaming`, `file_download`) — it loops until Ctrl+C. You'll need to
add `--chrome-binary` and `--interface` to the `collect_...` invocations
inside that script for Linux (they're hardcoded to rely on each script's own
defaults right now, which assume macOS).

`plans/*.json` (which domains get visited, per category) and
`analysis/top10k_login_requirements.csv` (which domains are believed to need
login, and which activity category they fall into) are already checked into
the repo — `git clone` brings those along automatically, no separate transfer
needed. Rerun `python src/build_plans.py` anytime the CSV's `category` column
gets updated, to pull any newly-categorized domains into the plan files.

## Quick checklist

- [ ] `git clone`, not a zip
- [ ] `--chrome-binary` pointed at real Chrome/Chromium on every command
- [ ] `--interface` set to the box's actual interface name
- [ ] `setcap` applied to `tcpdump` (not running the collector as root)
- [ ] `.browser-profiles/` present (yours or the student's), if collecting
      anything from a login-gated domain
- [ ] `--dry-run` on a plan before the real run, to catch config issues early
