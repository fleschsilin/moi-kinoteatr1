import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import bot


class SafetyTests(unittest.TestCase):
    def test_dangerous_titles_are_rejected(self):
        for title in ("3 guys 1 hammer", "suicide by AK-47", "Самоубийство"):
            with self.subTest(title=title):
                self.assertFalse(bot.title_is_allowed(title))

    def test_ordinary_title_is_not_blocked_by_keyword_filter(self):
        self.assertTrue(bot.title_is_allowed("Советский художественный фильм"))

    def test_license_accepts_known_cc_url(self):
        self.assertTrue(bot.check_license({
            "metadata": {
                "licenseurl": "https://creativecommons.org/licenses/by/4.0/"
            }
        }))

    def test_license_rejects_untrusted_domain(self):
        self.assertIsNone(bot.check_license({
            "metadata": {
                "licenseurl": "https://creativecommons.org.evil.example/licenses/by/4.0/"
            }
        }))

    def test_license_rejects_http(self):
        self.assertIsNone(bot.check_license({
            "metadata": {
                "licenseurl": "http://creativecommons.org/licenses/by/4.0/"
            }
        }))

    def test_unapproved_identifier_is_not_allowed(self):
        with patch.object(bot, "APPROVED_IDENTIFIERS", set()):
            self.assertFalse(bot.is_manually_approved("unreviewed-item"))

    def test_approved_identifier_is_allowed(self):
        with patch.object(bot, "APPROVED_IDENTIFIERS", {"reviewed-item"}):
            self.assertTrue(bot.is_manually_approved("reviewed-item"))

    def test_dry_run_defaults_to_true(self):
        env = os.environ.copy()
        env.pop("DRY_RUN", None)
        code = "import bot; print('true' if bot.DRY_RUN else 'false')"
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "true")

    def test_only_explicit_false_disables_dry_run(self):
        env = os.environ.copy()
        env["DRY_RUN"] = "false"
        code = "import bot; print('true' if bot.DRY_RUN else 'false')"
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "false")


if __name__ == "__main__":
    unittest.main()
