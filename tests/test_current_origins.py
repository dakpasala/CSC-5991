import csv
import argparse
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import assess_domains as a
import assess_current_origins as c


class CurrentOriginTests(unittest.TestCase):
    def test_runner_fetches_prefix_resumes_and_stops_at_qualifying_target(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            source = directory / "input.csv"
            source.write_text(
                "origin,rank\nhttps://first.example,1000\nhttps://second.example,1000\nhttps://third.example,5000\n"
            )
            args = argparse.Namespace(
                csv=source,
                output=directory / "output.csv",
                database=directory / "state.sqlite3",
                target=2,
                max_rows=1,
                offline=False,
                workers=1,
                interval=0.001,
                timeout=1,
                dry_run=False,
            )
            with (
                patch.object(c, "isolated_probe", return_value=self.result()) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                c.run(args)
                self.assertEqual(fetch.call_count, 1)
                args.max_rows = None
                c.run(args)
                self.assertEqual(fetch.call_count, 2)
                c.run(args)
                self.assertEqual(fetch.call_count, 2)
                args.offline = True
                c.run(args)
                self.assertEqual(fetch.call_count, 2)
            report = json.loads(args.output.with_suffix(".summary.json").read_text())
            self.assertEqual(report["rows_walked"], 2)
            self.assertEqual(report["qualifying"], 2)
            self.assertTrue(report["completed"])
            self.assertFalse(report["input_exhausted"])

    def result(self, **signals):
        return dict(
            http=[
                dict(
                    status=200,
                    final_url="https://news.example/",
                    signals=dict(
                        title="Daily News",
                        meta_description="Latest news and reporting",
                        public_text="A public news story about the local community. " * 30,
                        **signals,
                    ),
                )
            ]
        )

    def test_order_bucket_and_exact_subdomain(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input.csv"
            p.write_text(
                "origin,rank\nhttps://m.example.com,1000\nhttps://www.example.com,1000\nhttps://sellercentral.example.com,5000\nhttps://UPPER.example.com,5000\n"
            )
            rows = c.read_origins(p)
            self.assertEqual([r["rank"] for r in rows], ["1000", "1000", "5000", "5000"])
            self.assertEqual(rows[0]["domain"], "m.example.com")
            self.assertEqual(rows[2]["domain"], "sellercentral.example.com")
            self.assertEqual(rows[3]["domain"], "UPPER.example.com")
            self.assertFalse(rows[3]["valid"])

    def test_nonstandard_ports_preserve_origin_and_bare_hostname(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "input.csv"
            p.write_text(
                "origin,rank\nhttps://portal.example.com:8443,1000\nhttps://emp_gate.example.com,1000\nhttps://example.com:99999,5000\n"
            )
            rows = c.read_origins(p)
            self.assertTrue(rows[0]["valid"])
            self.assertEqual(rows[0]["domain"], "portal.example.com")
            self.assertEqual(rows[0]["origin"], "https://portal.example.com:8443")
            self.assertFalse(rows[1]["valid"])
            self.assertFalse(rows[2]["valid"])

    def test_real_reading_and_account_evidence(self):
        self.assertEqual(c.assess(self.result())[1]["login_required"], "no")
        self.assertEqual(c.assess(self.result(login_control=True))[1]["login_required"], "partial")
        r = self.result()
        r["http"][0]["signals"]["public_text"] = ""
        self.assertEqual(c.assess(r)[0], "unassessable")

    def test_no_default_and_failed_fetch(self):
        for r in (
            {},
            {"http": [{"status": 302}]},
            {"http": [{"status": 403}]},
            {
                "http": [
                    {
                        "status": 200,
                        "signals": {"title": "Welcome", "meta_description": "Our official website"},
                    }
                ]
            },
        ):
            self.assertEqual(c.assess(r)[0], "unassessable")

    def test_positive_browsing_purpose_without_default(self):
        cases = [
            (
                "Últimas Notícias",
                "Notícias de todo o mundo",
                "notícias públicas " * 100,
                "retained",
            ),
            (
                "Clothing online store",
                "Shop clothing products",
                "Our products and prices " * 100,
                "retained",
            ),
            (
                "Build an online store",
                "Create your shopping platform",
                "Our products and prices " * 100,
                "unassessable",
            ),
            (
                "Local weather",
                "Weather forecast",
                "Temperature and humidity forecast " * 100,
                "retained",
            ),
            (
                "Official website",
                "Welcome to our website",
                "Public text of unspecified purpose " * 100,
                "unassessable",
            ),
        ]
        for title, description, public, expected in cases:
            r = self.result()
            r["http"][0]["signals"].update(
                title=title, meta_description=description, public_text=public
            )
            self.assertEqual(c.assess(r)[0], expected, title)

    def test_explicit_og_description_cannot_hide_behind_benign_description(self):
        r = self.result()
        r["http"][0]["signals"].update(
            title="Read stories",
            meta_description="Read our stories",
            og_description="Free porn videos",
        )
        self.assertEqual(c.assess(r)[0], "explicit")

    def test_explicit_requires_metadata_not_domain(self):
        r = self.result()
        r["http"][0]["final_url"] = "https://porn.example/"
        self.assertEqual(c.assess(r)[0], "retained")
        r["http"][0]["signals"]["title"] = "Video library"
        r["http"][0]["signals"]["meta_description"] = "Watch free porn videos"
        self.assertEqual(c.assess(r)[0], "explicit")
        r["http"][0]["signals"]["title"] = "Health research"
        self.assertEqual(c.assess(r)[0], "unassessable")

    def test_conflicting_media_and_login_are_skipped(self):
        r = self.result(video_tag=True, audio_tag=True)
        s = r["http"][0]["signals"]
        s.update(
            title="Media",
            meta_description="Watch movies and listen to music",
            public_text="No account required",
        )
        self.assertEqual(c.assess(r)[0], "unassessable")
        s.update(
            title="Daily News",
            meta_description="Latest news",
            video_tag=False,
            audio_tag=False,
            public_text="No account required. Login required.",
        )
        self.assertEqual(c.assess(r)[0], "unassessable")

    def test_all_active_categories_need_corrobation(self):
        for category, title, description, extra in [
            ("video_streaming", "Movies", "Watch movies online", {"video_tag": True}),
            ("audio_streaming", "Music", "Listen to music online", {"audio_tag": True}),
            ("social_media_browsing", "Our social network", "A social network to share posts", {}),
            ("file_download", "Software downloads", "Download software", {"download_link": True}),
            ("excluded_conferencing", "Private messenger", "A secure messaging app", {}),
        ]:
            r = self.result()
            r["http"][0]["signals"].update(
                title=title,
                meta_description=description,
                public_text="No account required",
                **extra,
            )
            outcome, row = c.assess(r)
            self.assertEqual(outcome, "retained", row)
            self.assertEqual(row["category"], category)

    def test_ordered_export_and_excluded_not_counted(self):
        with tempfile.TemporaryDirectory() as d:
            db = sqlite3.connect(":memory:")
            db.execute(
                "CREATE TABLE origins (position INTEGER PRIMARY KEY, origin TEXT, rank TEXT, checked_at REAL, result TEXT)"
            )
            rows = [
                dict(
                    position=i, rank="1000", origin=f"https://d{i}.example", domain=f"d{i}.example"
                )
                for i in range(1, 5)
            ]
            excluded = self.result(login_control=True)
            excluded["http"][0]["signals"].update(
                title="Calling", meta_description="Video conferencing platform"
            )
            results = [excluded, self.result(), self.result(), self.result()]
            for i in [0, 2, 3]:
                r = rows[i]
                db.execute(
                    "INSERT INTO origins VALUES (?, ?, ?, 0, ?)",
                    (r["position"], r["origin"], r["rank"], json.dumps(results[i])),
                )
            path = Path(d) / "out.csv"
            counts, retained, _ = c.export(rows, db, path, 2)
            self.assertEqual(counts["rows_walked"], 1)
            self.assertEqual(counts["qualifying"], 0)
            r = rows[1]
            db.execute(
                "INSERT INTO origins VALUES (?, ?, ?, 0, ?)",
                (r["position"], r["origin"], r["rank"], json.dumps(results[1])),
            )
            counts, retained, _ = c.export(rows, db, path, 2)
            self.assertEqual(counts["rows_walked"], 3)
            self.assertEqual(counts["qualifying"], 2)
            self.assertEqual(counts["excluded_conferencing"], 1)
            with path.open() as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, c.FIELDS)
                self.assertEqual(
                    [r["domain"] for r in reader], ["d1.example", "d2.example", "d3.example"]
                )
            db.close()

    def test_resume_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "input.csv"
            source.write_text("origin,rank\nhttps://example.com,1000\n")
            checkpoint = Path(d) / "state.sqlite3"
            c.open_database(checkpoint, source).close()
            c.open_database(checkpoint, source).close()
            source.write_text("origin,rank\nhttps://other.example,1000\n")
            with self.assertRaises(ValueError):
                c.open_database(checkpoint, source)

    def test_origin_scheme_single_fetch(self):
        response = MagicMock(status=302)
        response.read.return_value = b""
        response.getheader.return_value = "text/html"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with (
            patch.object(
                a.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]
            ) as dns,
            patch.object(a.socket, "socket"),
            patch.object(a.http.client, "HTTPConnection", return_value=connection),
        ):
            result = a.probe("m.example.com", 1, "http://m.example.com")
        dns.assert_called_once_with("m.example.com", 80, type=a.socket.SOCK_STREAM)
        connection.request.assert_called_once()
        self.assertEqual(result["http"][0]["final_url"], "http://m.example.com")
        self.assertEqual(c.assess(result)[0], "unassessable")

    def test_parser_ignores_embedded_markup_and_values(self):
        parser = a.PageSignals()
        parser.feed(
            '<title>News</title><body><script>fake login <audio></script><template><video></template><svg><title>logo</title></svg><p>Actual public content</p><a href="/login">Sign in</a><input type="password" value="secret"><a href="/app.zip">Download</a></body>'
        )
        s = parser.signals()
        self.assertEqual(s["title"], "News")
        self.assertNotIn("secret", s["public_text"])
        self.assertNotIn("fake", s["public_text"])
        self.assertFalse(s["audio_tag"])
        self.assertFalse(s["video_tag"])
        self.assertTrue(s["login_control"])
        self.assertTrue(s["password_input"])
        self.assertTrue(s["download_link"])


if __name__ == "__main__":
    unittest.main()
