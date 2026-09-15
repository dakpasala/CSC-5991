"""Profile preservation and exclusive login/capture ownership."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from browser_profiles import browser_profile, collection_lock


class ProfileTests(unittest.TestCase):
    def test_persistent_profile_survives_failure_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile"
            with self.assertRaises(RuntimeError):
                with browser_profile(True, path) as profile:
                    (Path(profile) / "sentinel").write_text("test fixture")
                    raise RuntimeError("simulated browser failure")
            with browser_profile(True, path) as profile:
                self.assertEqual((Path(profile) / "sentinel").read_text(), "test fixture")
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_fresh_profile_is_removed(self):
        with browser_profile() as profile:
            self.assertTrue(Path(profile).is_dir())
        self.assertFalse(Path(profile).exists())

    def test_login_and_capture_cannot_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            with collection_lock(directory):
                with self.assertRaisesRegex(RuntimeError, "login setup"):
                    with collection_lock(directory):
                        self.fail("Second owner acquired the lock")
            with collection_lock(directory):
                pass


if __name__ == "__main__":
    unittest.main()
