"""Offline evidence, selection, and real-process supervisor tests."""

import json
import os
import signal
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collector_core as core
from assess_domains import probe, top_domains
from config import CATEGORIES, ROOT, available, load_plan
from evidence import media_progress, pcap_packets, rtc_progress


def fixture_worker(pipe, item, settings, deadline, profile):
    os.setsid()
    pipe.send({"event": "ready", "browser_version": "fixture"})
    pipe.recv()
    if item.get("hang"):
        time.sleep(10)
    else:
        pipe.send({"event": "done", "success": item.get("fixture_success", True)})
        if pipe.poll(3):
            pipe.recv()


class EvidenceTests(unittest.TestCase):
    def test_video_requires_frames_not_just_seek(self):
        before = dict(
            source="clip", time=1, frames=1, width=320, paused=False, ended=False, ready=4
        )
        after = {**before, "time": 2, "frames": 16}
        self.assertTrue(media_progress(before, after, 1))
        for bad in (
            None, {**after, "paused": True}, {**after, "time": 100}, {**after, "frames": 1}
        ):
            self.assertFalse(media_progress(before, bad, 1))

    def test_long_sampling_gap_is_not_continuous_playback(self):
        before = dict(source="sound", time=1, paused=False, ended=False, ready=4)
        after = {**before, "time": 2}
        self.assertFalse(media_progress(before, after, 25, video=False))

    def test_looped_audio_requires_time_progress(self):
        before = dict(source="sound", time=1.3, paused=False, ended=False, ready=4)
        after = {**before, "time": 0.3, "loop": True, "duration": 2}
        self.assertTrue(media_progress(before, after, 1, video=False))
        self.assertFalse(media_progress(before, {**after, "paused": True}, 1, video=False))
        self.assertFalse(media_progress(before, before, 1, video=False))

    def test_conference_rejects_preview_and_one_way_call(self):
        before = [dict(connected=True, incoming=10, outgoing=10, frames=1)]
        after = [dict(connected=True, incoming=30, outgoing=30, frames=3)]
        self.assertTrue(rtc_progress(before, after))
        self.assertFalse(rtc_progress(before, [{**after[0], "incoming": 10}]))
        self.assertFalse(rtc_progress(before, [{**after[0], "outgoing": 10}]))
        self.assertFalse(rtc_progress([], []))

    def test_pcap_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.pcap"
            header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 128, 1)
            path.write_bytes(header)
            self.assertEqual(pcap_packets(path), 0)
            path.write_bytes(header + struct.pack("<IIII", 1, 0, 4, 4) + b"data")
            self.assertEqual(pcap_packets(path), 1)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                pcap_packets(path)

    def test_top_domains_sort_numeric_not_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.csv"
            path.write_text("10,ten.com\n2,two.com\n1,one.com\n")
            self.assertEqual(top_domains(path, 2), [(1, "one.com"), (2, "two.com")])

    def test_invalid_dns_entry_is_recorded_without_network(self):
        self.assertEqual(probe("_wildcard_.ph", 1)["reason"], "invalid_domain")

    def test_invalid_duplicate_ranks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.csv"
            path.write_text("1,one.com\n1,two.com\n")
            with self.assertRaises(ValueError):
                top_domains(path, 2)

    def test_all_six_default_plans_validate(self):
        for category in CATEGORIES:
            plan = load_plan(ROOT / "plans" / f"{category}.json", category)
            self.assertGreater(len(plan), 0)
        conference = load_plan(ROOT / "plans/video_conferencing.json", "video_conferencing")
        self.assertFalse(available(conference[0]))

    def test_cross_category_plan_cannot_be_mislabelled(self):
        with self.assertRaises(ValueError):
            load_plan(ROOT / "plans/video_streaming.json", "web_browsing")

    def test_failure_cannot_promote_browser_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for directory in ("metadata", "failed", "video_streaming"):
                (output / directory).mkdir()
            pending = output / "failed/test.pcap"
            pending.write_bytes(b"partial")
            record = core.save_record({
                "session_id": "test", "browser_success": True,
                "requested_activity": "video_streaming",
            }, output, pending, capture_ok=False)
            self.assertFalse(record["success"])
            self.assertIsNone(record["actual_activity"])
            self.assertTrue(pending.exists())


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name)
        for name in (*CATEGORIES, "metadata", "failed"):
            (self.output / name).mkdir()
        executable = self.output / "fake_tcpdump"
        executable.write_text(
            f"#!{Path(sys.executable).resolve()}\n"
            "import sys,struct,signal,time\n"
            "p=sys.argv[sys.argv.index('-w')+1]\n"
            "h=b'\\xd4\\xc3\\xb2\\xa1'+struct.pack('<HHIIII',2,4,0,0,128,1)\n"
            "open(p,'wb').write(h+struct.pack('<IIII',1,0,4,4)+b'data')\n"
            "signal.signal(signal.SIGINT,lambda *args:sys.exit(0))\n"
            "time.sleep(20)\n"
        )
        executable.chmod(0o700)
        self.args = SimpleNamespace(
            session_seconds=4, interface="lo0", bpf_filter="ip", snaplen=128,
            tcpdump=str(executable),
        )
        core.STOP = False

    def tearDown(self):
        core.STOP = False
        self.tmp.cleanup()

    def run_fixture(self, **values):
        item = {"domain": "fixture.test", "activity": "web_browsing", **values}
        with patch.object(core, "worker", fixture_worker):
            return core.run_session(item, self.args, time.monotonic() + 4, self.output)

    def test_success_promotes_valid_capture(self):
        result = self.run_fixture()
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(result["pcap_path"]).parent.name, "web_browsing")
        self.assertEqual(result["packet_count"], 1)

    def test_failed_action_stays_unlabelled(self):
        result = self.run_fixture(fixture_success=False)
        self.assertFalse(result["success"])
        self.assertEqual(Path(result["pcap_path"]).parent.name, "failed")

    def test_total_deadline_bounds_hung_worker(self):
        item = {"domain": "fixture.test", "activity": "web_browsing", "hang": True}
        started = time.monotonic()
        with patch.object(core, "worker", fixture_worker):
            result = core.run_session(item, self.args, started + 1.5, self.output)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(result["success"])

    def test_interruption_is_logged_and_quarantined(self):
        timer = threading.Timer(0.8, lambda: setattr(core, "STOP", True))
        timer.start()
        try:
            result = self.run_fixture(hang=True)
        finally:
            timer.join()
        self.assertFalse(result["success"])
        self.assertIsNone(result["actual_activity"])
        self.assertTrue((self.output / "sessions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
