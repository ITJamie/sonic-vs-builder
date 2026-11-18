import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from upstream_patch_fixture import populate_patch_sources


class PatchFixture(unittest.TestCase):
    def test_later_edit_of_added_file_does_not_download_nonexistent_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.patch", root / "second.patch"
            first.write_text("--- /dev/null\n+++ b/new.py\n")
            second.write_text("--- a/new.py\n+++ b/new.py\n"
                              "--- a/original.py\n+++ b/original.py\n")
            source = root / "source"
            source.mkdir()
            with patch("upstream_patch_fixture.urllib.request.urlopen",
                       side_effect=lambda *a, **kw: io.BytesIO(b"original\n")) as fetch:
                populate_patch_sources(source, "a" * 40, [first, second])
            self.assertFalse((source / "new.py").exists())
            self.assertEqual((source / "original.py").read_text(), "original\n")
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(fetch.call_args.args[0].endswith("/original.py"))
