import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.prepare_movies import safe_url, safe_extract_zip, validate_redirect_chain


class PreparationSafetyTests(unittest.TestCase):
    def test_only_official_blender_download_host_is_allowed(self):
        self.assertEqual(
            safe_url("https://mirror.freedif.org/blender/demo/movies/test.mov"),
            "https://download.blender.org/demo/movies/test.mov",
        )
        for url in (
            "http://download.blender.org/file.mov",
            "https://example.org/file.mov",
            "https://download.blender.org.evil.example/file.mov",
            "https://user:pass@download.blender.org/file.mov",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_url(url)

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "no")
            with self.assertRaises(ValueError):
                safe_extract_zip(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_zip_expansion_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("large.bin", b"x" * 1024)
            import scripts.prepare_movies as prep
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
            history = [Hop("https://download.blender.org/redirect")]
            url = "https://evil.example/file"
        with self.assertRaises(ValueError):
            validate_redirect_chain(Response())


if __name__ == "__main__":
    unittest.main()
