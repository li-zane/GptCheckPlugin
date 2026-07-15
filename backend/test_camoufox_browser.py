import hashlib
import tempfile
import unittest
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from app.services.camoufox_browser import browser_profile_dir


class BrowserProfileIdentityTests(unittest.TestCase):
    def test_lossy_slug_collisions_receive_distinct_profile_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = SimpleNamespace(project_root=Path(temporary_directory))

            plus_profile = Path(browser_profile_dir("alice+ops@example.com", settings))
            underscore_profile = Path(browser_profile_dir("alice_ops@example.com", settings))

            self.assertNotEqual(plus_profile, underscore_profile)
            self.assertTrue(plus_profile.is_dir())
            self.assertTrue(underscore_profile.is_dir())
            expected_owner = hashlib.sha256(
                unicodedata.normalize("NFKC", "alice+ops@example.com").casefold().encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                (plus_profile / ".account-owner").read_text(encoding="ascii").strip(),
                expected_owner,
            )

    def test_existing_profile_with_wrong_owner_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = SimpleNamespace(project_root=Path(temporary_directory))
            profile = Path(browser_profile_dir("owner@example.com", settings))
            (profile / ".account-owner").write_text("not-the-owner\n", encoding="ascii")

            with self.assertRaisesRegex(RuntimeError, "ownership"):
                browser_profile_dir("owner@example.com", settings)

    def test_distinct_smtp_local_parts_never_share_a_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = SimpleNamespace(project_root=Path(temporary_directory))

            sharp_s = browser_profile_dir("ß@example.com", settings)
            letters = browser_profile_dir("ss@example.com", settings)
            full_width = browser_profile_dir("ａｌｉｃｅ@example.com", settings)
            ascii_name = browser_profile_dir("alice@example.com", settings)

            self.assertNotEqual(sharp_s, letters)
            self.assertNotEqual(full_width, ascii_name)

    def test_concurrent_first_use_publishes_one_complete_owner_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = SimpleNamespace(project_root=Path(temporary_directory))

            with ThreadPoolExecutor(max_workers=8) as executor:
                profiles = list(
                    executor.map(
                        lambda _index: browser_profile_dir("shared@example.com", settings),
                        range(24),
                    )
                )

            self.assertEqual(len(set(profiles)), 1)
            profile = Path(profiles[0])
            expected_owner = hashlib.sha256(b"shared@example.com").hexdigest()
            self.assertEqual(
                (profile / ".account-owner").read_text(encoding="ascii").strip(),
                expected_owner,
            )
            self.assertEqual(list(profile.glob(".account-owner.*")), [])

    def test_empty_legacy_owner_marker_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = SimpleNamespace(project_root=Path(temporary_directory))
            profile = Path(browser_profile_dir("owner@example.com", settings))
            (profile / ".account-owner").write_text("", encoding="ascii")

            recovered = Path(browser_profile_dir("owner@example.com", settings))

            expected_owner = hashlib.sha256(b"owner@example.com").hexdigest()
            self.assertEqual(recovered, profile)
            self.assertEqual(
                (profile / ".account-owner").read_text(encoding="ascii").strip(),
                expected_owner,
            )


if __name__ == "__main__":
    unittest.main()
