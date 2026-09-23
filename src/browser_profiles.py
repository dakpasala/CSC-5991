"""Local collection profiles and the shared exclusive browser/capture lock."""

from contextlib import contextmanager
import fcntl
import json
import tempfile
from pathlib import Path

from config import ROOT

LOGIN_PROFILE = ROOT / ".browser-profiles" / "collection"
CHROME_BINARY = "/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome"
LOGIN_DEBUG_PORT = 9223
SESSION_COOKIES = ROOT / ".browser-profiles" / "session_cookies.json"
SESSION_COOKIE_DOMAINS = (
    "x.com",
    "instagram.com",
    "google.com",
    "youtube.com",
    "facebook.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "spotify.com",
    "snapchat.com",
    "amazon.com",
    "whatsapp.com",
    "reddit.com",
    "adobe.com",
    "bsky.app",
    "myanimelist.net",
    "bing.com",
    "cloud.microsoft",
    "twitch.tv",
    "indeed.com",
)


@contextmanager
def collection_lock(root=ROOT):
    with (Path(root) / ".capture.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "A collector or login setup is open; finish it before continuing"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def browser_profile(persistent=False, path=LOGIN_PROFILE):
    if persistent:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        # Chrome owns its own process lock. Never remove its lock or session files.
        yield str(path.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="collector-chrome-") as profile:
            yield profile


def save_session_cookies(cookies_by_domain):
    """Write to the gitignored profile directory, mode 0600, never logged."""
    SESSION_COOKIES.parent.mkdir(parents=True, exist_ok=True)
    SESSION_COOKIES.write_text(json.dumps(cookies_by_domain))
    SESSION_COOKIES.chmod(0o600)


def load_session_cookies(domain):
    """Cookies are bucketed by base domain (e.g. "pinterest.com"); match a plan
    entry like "in.pinterest.com" against its base domain, not by exact key."""
    if not SESSION_COOKIES.exists():
        return []
    try:
        buckets = json.loads(SESSION_COOKIES.read_text())
    except (OSError, ValueError):
        return []
    for base in SESSION_COOKIE_DOMAINS:
        if domain == base or domain.endswith("." + base):
            return buckets.get(base, [])
    return []
