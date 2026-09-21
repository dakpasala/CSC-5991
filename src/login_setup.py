"""Open normal Chrome for manual sign-in, without Selenium logging or packet capture."""

import argparse
import subprocess

from browser_profiles import (
    CHROME_BINARY,
    LOGIN_DEBUG_PORT,
    SESSION_COOKIE_DOMAINS,
    browser_profile,
    collection_lock,
    save_session_cookies,
)


def extract_session_cookies(port, chrome_binary):
    """Read cookies out of the already-authenticated manual Chrome session.

    Attaches to the running browser via its own remote-debugging port rather than
    launching a new automated one, so this never touches the login flow itself.
    Cookie values only ever pass through memory here before being written once to
    the gitignored session-cookie file; nothing is logged or printed.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_experimental_option("debuggerAddress", f"localhost:{port}")
    options.binary_location = chrome_binary
    driver = webdriver.Chrome(options=options)
    try:
        cookies = driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
    finally:
        driver.quit()
    buckets = {domain: [] for domain in SESSION_COOKIE_DOMAINS}
    for cookie in cookies:
        host = cookie.get("domain", "").lstrip(".")
        for domain in SESSION_COOKIE_DOMAINS:
            if host == domain or host.endswith("." + domain):
                sanitized = {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie.get("domain"),
                    "path": cookie.get("path", "/"),
                    "secure": cookie.get("secure", False),
                }
                if cookie.get("sameSite") in ("Strict", "Lax", "None"):
                    sanitized["sameSite"] = cookie["sameSite"]
                if isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0:
                    sanitized["expiry"] = int(cookie["expires"])
                buckets[domain].append(sanitized)
    return buckets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-binary", default=CHROME_BINARY)
    parser.add_argument("--debug-port", type=int, default=LOGIN_DEBUG_PORT)
    args = parser.parse_args()
    try:
        with collection_lock(), browser_profile(persistent=True) as profile:
            browser = subprocess.Popen(
                [
                    args.chrome_binary,
                    f"--user-data-dir={profile}",
                    f"--remote-debugging-port={args.debug_port}",
                    "--no-first-run",
                    "--disable-sync",
                    "--new-window",
                    "https://accounts.google.com/",
                    "https://www.youtube.com/",
                    "https://www.instagram.com/accounts/login/",
                    "https://x.com/i/flow/login",
                    "https://www.facebook.com/login/",
                    "https://www.linkedin.com/login",
                    "https://www.pinterest.com/login/",
                    "https://www.tiktok.com/login",
                    "https://accounts.spotify.com/login",
                    "https://vk.com/",
                    "https://www.snapchat.com/",
                    "https://www.tumblr.com/login",
                    "https://weibo.com/",
                    "https://ok.ru/",
                    "https://www.xiaohongshu.com/",
                    "https://www.threads.net/login",
                    "https://www.netflix.com/login",
                    "https://www.primevideo.com/",
                    "https://www.disneyplus.com/login",
                    "https://www.paramountplus.com/",
                    "https://www.douyin.com/",
                    "https://www.hulu.com/",
                    "https://www.hbomax.com/",
                    "https://www.crunchyroll.com/login",
                    "https://www.peacocktv.com/",
                    "https://www.max.com/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                print("Login setup is open. Packet capture is OFF.", flush=True)
                print(
                    "Sign into whichever of these you need, then come back here. "
                    "No credentials or page content are read.",
                    flush=True,
                )
                input(
                    "When finished, return here and press Enter to close this dedicated browser: "
                )
                try:
                    save_session_cookies(
                        extract_session_cookies(args.debug_port, args.chrome_binary)
                    )
                    print("Session cookies captured for automated reuse.", flush=True)
                except Exception as error:
                    print(
                        f"Could not capture session cookies ({type(error).__name__}); "
                        "the saved Chrome profile is still preserved.",
                        flush=True,
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
