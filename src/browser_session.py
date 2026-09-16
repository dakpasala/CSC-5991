"""Selenium actions performed in an isolated, deadline-controlled worker."""

import json
import os
import signal
import time
from urllib.parse import urlsplit, urlunsplit

from browser_profiles import load_session_cookies
from config import BROWSING
from evidence import BROWSING_STATE, MEDIA_STATE, NO_MEDIA, RTC_HOOK, RTC_STATS
from evidence import media_progress, rtc_progress

DOWNLOAD = r"""
const url = arguments[0], maximum = arguments[1], done = arguments[arguments.length - 1];
(async () => {
 const controller = new AbortController();
 const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
 const type = response.headers.get('content-type') || '';
 if (!response.ok || /text\/html/i.test(type)) {
   controller.abort(); return {ok:false, reason:'HTTP error or HTML instead of file'};
 }
 if (Number(response.headers.get('content-length')) > maximum) {
   controller.abort(); return {ok:false, reason:'Download exceeds byte limit'};
 }
 const reader = response.body.getReader();
 let bytes = 0;
 while (true) {
   const chunk = await reader.read();
   if (chunk.done) break;
   bytes += chunk.value.byteLength;
   if (bytes > maximum) {
     controller.abort(); return {ok:false, reason:'Download exceeds byte limit'};
   }
 }
 return {ok:true, bytes, content_type:type, status:response.status};
})().then(done).catch(() => done({ok:false, reason:'Download interrupted or inaccessible'}));
"""


def redact_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))


def worker(pipe, item, settings, deadline, profile):
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    driver = None
    phase = "startup"
    try:
        from selenium import webdriver
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as conditions
        from selenium.webdriver.support.ui import WebDriverWait

        def emit(event, **data):
            pipe.send({"event": event, "time": time.time(), **data})

        def remaining():
            seconds = deadline - time.monotonic() - 1
            if seconds <= 0:
                raise TimeoutError("Session deadline reached")
            return seconds

        def find(selector):
            nonlocal phase
            phase = "wait_for_selector"
            emit("selector_wait", selector=selector)
            return WebDriverWait(
                driver, min(settings["timeout"], remaining()), poll_frequency=0.2,
                ignored_exceptions=(StaleElementReferenceException,),
            ).until(conditions.visibility_of_element_located((By.CSS_SELECTOR, selector)))

        def navigate(url):
            nonlocal phase
            phase = "navigation"
            driver.set_page_load_timeout(min(settings["timeout"], remaining()))
            driver.execute_script("window.__collectorNavigationPending = true")
            driver.get(url)
            WebDriverWait(
                driver, min(settings["timeout"], remaining()), poll_frequency=0.2,
                ignored_exceptions=(StaleElementReferenceException,),
            ).until(
                lambda d: d.current_url.startswith(("https://", "http://"))
                and d.execute_script("return !window.__collectorNavigationPending && "
                                     "['interactive', 'complete'].includes(document.readyState)")
            )
            if not driver.current_url.startswith(("https://", "http://")):
                raise RuntimeError("Navigation did not reach an HTTP page")
            if driver.execute_script("return !!document.querySelector('#main-frame-error')"):
                raise RuntimeError("Chrome network/access error page")
            title = driver.title.lower()
            if any(word in title for word in (
                "access denied", "just a moment", "captcha", "privacy error", "403 forbidden"
            )):
                raise RuntimeError("Blocked page or browser error")
            if item["activity"] in BROWSING:
                WebDriverWait(
                    driver, min(settings["timeout"], remaining()), poll_frequency=0.2,
                    ignored_exceptions=(StaleElementReferenceException,),
                ).until(lambda d: len(d.find_element(By.TAG_NAME, "body").text.strip()) >= 20)
            for entry in driver.get_log("performance"):
                message = json.loads(entry["message"])["message"]
                if message["method"] != "Network.responseReceived":
                    continue
                params = message["params"]
                response = params["response"]
                same_url = response["url"] == driver.current_url
                if params.get("type") == "Document" and same_url:
                    if response["status"] >= 400:
                        raise RuntimeError("HTTP navigation failure")
            if item["activity"] == "video_conferencing":
                emit("navigate", host=urlsplit(driver.current_url).hostname)
            else:
                emit("navigate", url=redact_url(driver.current_url))

        dismissed_prompt = False

        def browsing_evidence():
            nonlocal dismissed_prompt
            state = driver.execute_script(BROWSING_STATE, item.get("content_selector", "body"))
            if not state["content"] and item.get("dismiss_selector") and not dismissed_prompt:
                for button in driver.find_elements(By.CSS_SELECTOR, item["dismiss_selector"]):
                    if button.is_displayed():
                        button.click()
                        dismissed_prompt = True
                        emit("dismiss_prompt", selector=item["dismiss_selector"])
                        time.sleep(min(0.3, remaining()))
                        state = driver.execute_script(
                            BROWSING_STATE, item.get("content_selector", "body")
                        )
                        break
            emit("content_sample", **state)
            if not state["content"] or state["playing_media"]:
                raise RuntimeError("Readable browsing content absent or media playing")
            return state

        options = webdriver.ChromeOptions()
        options.page_load_strategy = "none"
        headed = settings["headed"] or item.get("headed", False)
        if not headed:
            options.add_argument("--headless=new")
        for argument in (
            "--disable-background-networking", "--disable-sync", "--no-first-run",
            "--disable-default-apps", "--disable-extensions", "--disable-component-update",
            "--window-size=1280,900", "--autoplay-policy=no-user-gesture-required",
            f"--user-data-dir={profile}",
        ):
            options.add_argument(argument)
        options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "download_restrictions": 3,
        })
        if settings["chrome_binary"]:
            options.binary_location = settings["chrome_binary"]
        if item["activity"] == "video_conferencing":
            options.add_argument("--use-fake-device-for-media-stream")
            options.add_argument("--use-fake-ui-for-media-stream")
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        service = Service(settings["chromedriver"]) if settings["chromedriver"] else Service()
        driver = webdriver.Chrome(options=options, service=service)
        driver.command_executor.client_config.timeout = min(settings["timeout"], remaining())
        driver.set_script_timeout(min(5, settings["timeout"]))
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        script = ""
        if item["activity"] in BROWSING:
            script = NO_MEDIA
        elif item["activity"] == "video_conferencing":
            script = RTC_HOOK
        if script:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        emit("ready", browser_version=driver.capabilities.get("browserVersion"), headed=headed)
        if pipe.recv() != "go":
            return
        if settings.get("use_login_profile"):
            cookies = load_session_cookies(item["domain"])
            if cookies:
                phase = "session_cookie_injection"
                driver.get(f"https://{item['domain']}/")
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        pass
                emit("session_cookies_injected", count=len(cookies))
        url = item.get("url") or os.environ[item["meeting_url_env"]]
        navigate(url)
        for step in item.get("steps", []):
            element = find(step["selector"])
            if step["type"] == "click":
                element.click()
            elif step["type"] == "fill":
                element.clear()
                element.send_keys(os.environ[step["value_env"]])
            emit("step", action=step["type"])
        if item.get("frame_selector"):
            driver.switch_to.frame(find(item["frame_selector"]))
        if item.get("content_selector"):
            find(item["content_selector"])
        verified = 0.0
        activity = item["activity"]
        if activity in BROWSING:
            phase = "browsing"
            if activity == "social_media_browsing":
                driver.execute_script("document.querySelector(arguments[0]).scrollIntoView({block:'center'})",
                                      item["content_selector"])
            # Network.setCacheDisabled forces resources to fetch fresh each run, and
            # page_load_strategy="none" can return before a heavy/SPA page finishes its
            # first real paint (or briefly clears content mid-hydration); poll rather
            # than a single fixed check before treating it as a failure.
            settle_deadline = min(time.monotonic() + 10, deadline - 1.5)
            while True:
                try:
                    browsing_evidence()
                    break
                except RuntimeError:
                    if time.monotonic() >= settle_deadline:
                        raise
                    time.sleep(min(1.5, max(0, settle_deadline - time.monotonic())))
            post_index = 0
            link_index = 0
            link_time = time.monotonic()
            while deadline - time.monotonic() > 1.5:
                if activity == "social_media_browsing":
                    driver.execute_script("""
                        const posts = document.querySelectorAll(arguments[0]);
                        if (posts.length) posts[arguments[1] % posts.length].scrollIntoView({block:'center'});
                    """, item["content_selector"], post_index)
                    post_index += 1
                else:
                    driver.execute_script("window.scrollBy(0, window.innerHeight * 0.7)")
                emit("scroll")
                before = time.monotonic()
                time.sleep(min(2, max(0, deadline - before - 1.5)))
                browsing_evidence()
                verified += time.monotonic() - before
                links = item.get("links", [])
                if link_index < len(links) and time.monotonic() - link_time >= 8:
                    navigate(links[link_index])
                    if item.get("content_selector"):
                        find(item["content_selector"])
                    link_index += 1
                    link_time = time.monotonic()
        elif activity == "file_download":
            driver.set_script_timeout(min(settings["timeout"], remaining()))
            result = driver.execute_async_script(
                DOWNLOAD, item["download_url"], settings["max_download_bytes"]
            )
            emit("download_result", **result)
            if not result["ok"] or result.get("bytes", 0) < settings["min_download_bytes"]:
                raise RuntimeError("Download did not complete with sufficient bytes")
            emit("done", success=True, downloaded_bytes=result["bytes"])
            if pipe.poll(5):
                pipe.recv()
            return
        else:
            video = activity == "video_streaming"
            conference = activity == "video_conferencing"
            selector = item.get("media_selector", "video" if video else "audio")
            if conference:
                find(item["joined_selector"])
                emit("joined_indicator_visible")
                previous = driver.execute_async_script(RTC_STATS)
            else:
                phase = "media_lookup"
                find(selector)
                phase = "playback"
                driver.execute_script(
                    "const m=document.querySelector(arguments[0]); m.muted=true; "
                    "m.loop=arguments[1]; m.play().catch(()=>{});",
                    selector, bool(item.get("loop", False)),
                )
                emit("play_requested", media="video" if video else "audio")
                previous = driver.execute_script(MEDIA_STATE, selector)
            sampled = last_progress = time.monotonic()
            while deadline - time.monotonic() > 1.5:
                time.sleep(min(1, max(0, deadline - time.monotonic() - 1.5)))
                if conference:
                    current = driver.execute_async_script(RTC_STATS)
                    now = time.monotonic()
                    progressing = now - sampled <= 3 and rtc_progress(previous, current)
                    details = {"peers": current}
                else:
                    current = driver.execute_script(MEDIA_STATE, selector)
                    now = time.monotonic()
                    progressing = media_progress(previous, current, now - sampled, video)
                    details = {"media_time": current["time"] if current else None}
                if progressing:
                    verified += now - sampled
                    last_progress = now
                emit("media_sample", progressing=progressing, **details)
                previous, sampled = current, now
                if now - last_progress > settings["stall_seconds"]:
                    # Once genuine playback already cleared the bar, a late stall
                    # (ad, bot-check wall, natural end) ends the session gracefully
                    # instead of discarding an already-verified capture.
                    if verified >= settings["min_verified_seconds"]:
                        break
                    raise RuntimeError("Playback/call absent or stalled")
            if (
                time.monotonic() - last_progress > 3
                and verified < settings["min_verified_seconds"]
            ):
                raise RuntimeError("Media was not progressing at session end")
        if verified < settings["min_verified_seconds"]:
            raise RuntimeError("Insufficient verified activity duration")
        emit("done", success=True, verified_seconds=round(verified, 3))
        if pipe.poll(5):
            pipe.recv()
    except BaseException as error:
        try:
            if driver:
                try:
                    diagnostics = driver.execute_script("""
                        return {
                          audio_elements: document.querySelectorAll('audio').length,
                          video_elements: document.querySelectorAll('video').length,
                          article_elements: document.querySelectorAll('article').length,
                          post_links: document.querySelectorAll('a[href*="/p/"],a[href*="/reel/"]').length,
                          login_prompt: /log in|sign in|sign up/i.test(document.body.innerText),
                          access_denied: /access.{0,20}denied|HTTP ERROR 403|don't have authorization/i.test(document.body.innerText)
                        };
                    """)
                    diagnostics["window_count"] = len(driver.window_handles)
                    emit("browser_diagnostic", phase=phase, **diagnostics)
                except Exception as diagnostic_error:
                    emit("browser_diagnostic", phase=phase,
                         diagnostic_error=type(diagnostic_error).__name__)
            reason = f"Browser action failed during {phase}; inspect access or selectors"
            if type(error) in (RuntimeError, TimeoutError):
                reason = str(error)
            pipe.send({
                "event": "done", "success": False,
                "reason": reason, "error_type": type(error).__name__,
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        pipe.close()
