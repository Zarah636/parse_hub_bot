import tempfile
import unittest
from pathlib import Path

from utils.helpers import clear_directory_contents


class ClearDirectoryContentsTests(unittest.TestCase):
    def test_preserves_root_and_removes_children(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "downloads"
            nested = root / "task" / "processed"
            nested.mkdir(parents=True)
            (root / "orphan.tmp").write_text("temporary", encoding="utf-8")
            (nested / "video.mp4").write_bytes(b"media")

            clear_directory_contents(root)

            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
