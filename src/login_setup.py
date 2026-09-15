"""Open normal Chrome for manual sign-in, without Selenium logging or packet capture."""

import argparse
import subprocess

from browser_profiles import CHROME_BINARY, browser_profile, collection_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", default=CHROME_BINARY)
    args = parser.parse_args()
    try:
        with collection_lock(), browser_profile(persistent=True) as profile:
            browser = subprocess.Popen(
                [
                    args.chrome_binary,
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--disable-sync",
                    "--new-window",
                    "https://accounts.google.com/",
                    "https://www.instagram.com/accounts/login/",
                    "https://x.com/i/flow/login",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                print("Login setup is open. Packet capture is OFF.", flush=True)
                print(
                    "Sign into the three sites yourself. No credentials or page content are read.",
                    flush=True,
                )
                input(
                    "When finished, return here and press Enter to close this dedicated browser: "
                )
            finally:
                if browser.poll() is None:
                    browser.terminate()
                    try:
                        browser.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        print(
                            "Chrome is still saving/closing. Close this dedicated window before collecting."
                        )
                print(
                    "Profile preserved locally. Login state must be checked through visible site content."
                )
    except (RuntimeError, OSError) as error:
        parser.exit(1, f"{error}\n")
    except (KeyboardInterrupt, EOFError):
        print("Login setup ended; the profile is preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
