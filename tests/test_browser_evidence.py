"""Optional local HTML browser checks; no live-site activity or PCAP labels."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from evidence import BROWSING_STATE


@unittest.skipUnless(os.environ.get("COLLECTOR_BROWSER_TESTS") == "1", "opt-in local Chrome tests")
class BrowserEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selenium import webdriver

        cls.profile = tempfile.TemporaryDirectory(prefix="collector-evidence-test-")
        options = webdriver.ChromeOptions()
        options.binary_location = "/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome"
        for argument in (
            "--headless=new", "--disable-background-networking", "--disable-sync",
            "--no-first-run", "--host-resolver-rules=MAP * ~NOTFOUND",
            f"--user-data-dir={cls.profile.name}",
        ):
            options.add_argument(argument)
        cls.driver = webdriver.Chrome(options=options)
        cls.driver.set_page_load_timeout(5)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.profile.cleanup()

    def state(self, html):
        self.driver.get("data:text/html," + quote(html))
        return self.driver.execute_script(BROWSING_STATE, ".post")

    def test_readable_post_is_content(self):
        self.assertTrue(self.state("<article class='post'>A real readable public post with text.</article>")["content"])

    def test_login_shell_is_not_post_content(self):
        self.assertFalse(self.state("<main>Log in to see the public profile.</main>")["content"])

    def test_hidden_offscreen_and_covered_posts_fail(self):
        for style in ("display:none", "margin-top:2000px"):
            with self.subTest(style=style):
                self.assertFalse(self.state(
                    f"<article class='post' style='{style}'>A real readable public post with text.</article>"
                )["content"])
        self.assertFalse(self.state(
            "<article class='post'>A real readable public post with text.</article>"
            "<div style='position:fixed;inset:0;background:white;z-index:999'>Please log in</div>"
        )["content"])

    def test_broken_post_image_is_not_content(self):
        self.assertFalse(self.state("<a class='post'><img src='data:,' width='50' height='50'></a>")["content"])


if __name__ == "__main__":
    unittest.main()
