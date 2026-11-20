import os
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import bot


VALID_ENTRY = {
    "identifier": "reviewed-film",
    "title": "Проверенный фильм",
    "year": 2020,
    "director": "Режиссёр",
    "language": "ru",
    "synopsis": "Описание фильма",
    "genre": "Драма",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "rights_basis": "verified_cc_license",
    "rights_evidence_url": "https://rights.example.org/proof",
    "rights_status": "verified",
    "rights_territories": ["KZ"],
    "content_review": "passed",
    "reviewed_by": "editor",
    "reviewed_at": "2026-11-20",
    "approved": True,
}


class SafetyTests(unittest.TestCase):
    def test_dangerous_titles_are_rejected(self):
        for title in ("3 guys 1 hammer", "suicide by AK-47", "Самоубийство"):
            with self.subTest(title=title):
                self.assertFalse(bot.title_is_allowed(title))

    def test_ordinary_titles_are_allowed(self):
        self.assertTrue(bot.title_is_allowed("Советский художественный фильм"))
        self.assertTrue(bot.title_is_allowed("Она написала убийство (1984 - 1996)"))

    def test_license_accepts_known_cc_url(self):
        self.assertTrue(bot.check_license({
            "metadata": {"licenseurl": "https://creativecommons.org/licenses/by/4.0/"}
        }))

    def test_license_rejects_untrusted_domain(self):
        self.assertIsNone(bot.check_license({
            "metadata": {"licenseurl": "https://creativecommons.org.evil.example/licenses/by/4.0/"}
        }))

    def test_license_rejects_http(self):
        self.assertIsNone(bot.check_license({
            "metadata": {"licenseurl": "http://creativecommons.org/licenses/by/4.0/"}
        }))

    def test_valid_library_entry_passes(self):
        self.assertEqual(bot.validate_library_entry(VALID_ENTRY), VALID_ENTRY)

    def test_unverified_rights_fail_closed(self):
        entry = dict(VALID_ENTRY, rights_status="pending")
        with self.assertRaises(ValueError):
            bot.validate_library_entry(entry)

    def test_missing_kazakhstan_rights_fail_closed(self):
        entry = dict(VALID_ENTRY, rights_territories=["US"])
        with self.assertRaises(ValueError):
            bot.validate_library_entry(entry)

    def test_unreviewed_content_fails_closed(self):
        entry = dict(VALID_ENTRY, content_review="pending")
        with self.assertRaises(ValueError):
            bot.validate_library_entry(entry)

    def test_dangerous_catalog_title_fails_closed(self):
        entry = dict(VALID_ENTRY, title="3 guys 1 hammer")
        with self.assertRaises(ValueError):
            bot.validate_library_entry(entry)

    def test_non_cc_license_fails_closed(self):
        entry = dict(VALID_ENTRY, license_url="https://example.org/license")
        with self.assertRaises(ValueError):
            bot.validate_library_entry(entry)

    def test_dry_run_defaults_to_true(self):
        env = os.environ.copy()
        env.pop("DRY_RUN", None)
        code = "import bot; print('true' if bot.DRY_RUN else 'false')"
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "true")

    def test_only_explicit_false_disables_dry_run(self):
        env = os.environ.copy()
        env["DRY_RUN"] = "false"
        code = "import bot; print('true' if bot.DRY_RUN else 'false')"
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "false")


    def test_research_queue_candidates_are_never_preapproved(self):
        from pathlib import Path
        queue = json.loads(
            (Path(__file__).resolve().parents[1] / "film_research_queue.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(queue["schema_version"], 1)
        self.assertGreaterEqual(len(queue["candidates"]), 1)
        for candidate in queue["candidates"]:
            with self.subTest(candidate=candidate["candidate_id"]):
                self.assertEqual(candidate["approval_status"], "not_approved")
                self.assertEqual(candidate["source_file_review_status"], "pending")
                self.assertEqual(candidate["content_review_status"], "pending")
                self.assertTrue(candidate["rights_evidence_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
