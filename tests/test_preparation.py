import tempfile
import unittest
import zipfile
from pathlib import Path

import scripts.prepare_movies as prep
from scripts.prepare_movies import safe_url, safe_extract_zip, validate_redirect_chain


class PreparationTests(unittest.TestCase):
    def test_quality_policy_has_no_fixed_48mb_target(self):
        self.assertEqual(prep.VIDEO_CRF, 18)
        self.assertEqual(prep.VIDEO_PRESET, "slow")
        self.assertFalse(hasattr(prep, "TARGET_BYTES"))
        self.assertFalse(hasattr(prep, "video_bitrate_kbps"))

    def test_only_https_sources(self):
        with self.assertRaises(ValueError):
            safe_url("http://mirror.freedif.org/movie.mp4")
        self.assertEqual(
            safe_url("https://mirror.freedif.org/blender/movie.mp4"),
            "https://mirror.freedif.org/blender/movie.mp4")
        with self.assertRaises(ValueError):
            safe_url("https://example.com/movie.mp4")

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "no")
            with self.assertRaises(ValueError):
                safe_extract_zip(archive, root / "out")

    def test_zip_expansion_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("large.bin", b"x" * 1024)
            old_limit = prep.MAX_UNPACKED_BYTES
            try:
                prep.MAX_UNPACKED_BYTES = 100
                with self.assertRaises(ValueError):
                    safe_extract_zip(archive, root / "out")
            finally:
                prep.MAX_UNPACKED_BYTES = old_limit

    def test_redirect_to_unapproved_host_is_rejected(self):
        class Hop:
            def __init__(self, url):
                self.url = url
        class Response:
            history = [Hop("https://mirror.freedif.org/blender/redirect")]
            url = "https://evil.example/file"
        with self.assertRaises(ValueError):
            validate_redirect_chain(Response())


if __name__ == "__main__":
    unittest.main()
