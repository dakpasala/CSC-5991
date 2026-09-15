"""Local collection profiles and the shared exclusive browser/capture lock."""

from contextlib import contextmanager
import fcntl
import tempfile
from pathlib import Path

from config import ROOT

LOGIN_PROFILE = ROOT / ".browser-profiles" / "collection"
CHROME_BINARY = "/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome"


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
