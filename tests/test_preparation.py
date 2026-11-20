import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_movies


class PreparationTests(unittest.TestCase):
    def test_bitrate_is_under_telegram_budget(self):
        rate = prepare_movies.video_bitrate_kbps(600)
        self.assertGreaterEqual(rate, 160)
        self.assertLess(rate, 600)

    def test_long_runtime_rejected_if_too_large_for_budget(self):
        with self.assertRaises(ValueError):
            prepare_movies.video_bitrate_kbps(5000)

    def test_only_https_sources(self):
        with self.assertRaises(ValueError):
            prepare_movies.safe_url("http://example.com/movie.mp4")
        self.assertEqual(
            prepare_movies.safe_url("https://mirror.freedif.org/blender/movie.mp4"),
            "https://mirror.freedif.org/blender/movie.mp4"
        )
        with self.assertRaises(ValueError):
            prepare_movies.safe_url("https://example.com/movie.mp4")

    def test_zip_path_traversal_is_rejected(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "no")
            with self.assertRaises(ValueError):
                prepare_movies.safe_extract_zip(archive, root / "out")


if __name__ == "__main__":
    unittest.main()
