import difflib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("patch_builder", Path(__file__).parents[1] / "scripts/sonic_vs.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BranchPatches(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        self.root = self.work / "patches"
        (self.root / "202605").mkdir(parents=True)
        self.target = self.source / "constraints"
        self.target.write_text("old\n")
        self.template = self.source / "sonic-slave-bookworm/Dockerfile.j2"
        self.template.parent.mkdir()
        self.template.write_text("FROM debian:bookworm\n")

    def add_patch(self, name, old="old\n", new="fixed\n", target="constraints"):
        path = self.root / "202605" / name
        path.write_text("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                  fromfile=f"a/{target}", tofile=f"b/{target}")))
        return path

    def apply(self, branch="202605"):
        return builder.apply_branch_patches(self.source, self.work, branch, self.root)

    def test_exact_branch_only_and_empty_report(self):
        self.add_patch("0001.patch")
        self.assertEqual(self.apply("202511"), [])
        self.assertEqual(self.target.read_text(), "old\n")
        self.assertEqual(json.loads((self.work / "patches.json").read_text()),
                         {"branch": "202511", "patches": []})

    def test_reject_invalid_branch(self):
        with self.assertRaises(ValueError):
            self.apply("../202605")

    def test_order_hash_metadata_and_image_invalidation(self):
        self.add_patch("0002.patch", "first\n", "fixed\n")
        first = self.add_patch("0001.patch", "old\n", "first\n")
        results = self.apply()
        self.assertEqual(self.target.read_text(), "fixed\n")
        self.assertEqual([p["path"] for p in results], ["202605/0001.patch", "202605/0002.patch"])
        self.assertEqual(results[0]["sha256"], builder.checksum(first))
        self.assertTrue(all(p["status"] == "applied" for p in results))
        self.assertIn("branch patches:", self.template.read_text())
        self.assertEqual(json.loads((self.work / "patches.json").read_text())["patches"], results)

    def test_upstream_already_contains_fix(self):
        self.add_patch("0001.patch")
        self.target.write_text("fixed\n")
        self.assertEqual(self.apply()[0]["status"], "already-applied")
        self.assertIn("branch patches:", self.template.read_text())

    def test_patch_can_modify_initialized_submodule_files(self):
        submodule = self.source / "src/example"
        submodule.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(submodule)], check=True)
        # Register a gitlink in the parent index, as in the upstream checkout.
        subprocess.run(["git", "-C", str(self.source), "update-index", "--add", "--cacheinfo",
                        "160000", "a" * 40, "src/example"], check=True)
        (submodule / "requirements.txt").write_text("old\n")
        self.add_patch("0001.patch", target="src/example/requirements.txt")
        self.assertEqual(self.apply()[0]["status"], "applied")
        self.assertEqual((submodule / "requirements.txt").read_text(), "fixed\n")

    def test_patch_edit_changes_image_stamp(self):
        self.add_patch("0001.patch")
        self.apply()
        first = self.template.read_text()
        self.target.write_text("old\n")
        self.template.write_text("FROM debian:bookworm\n")
        self.add_patch("0001.patch", new="different fix\n")
        self.apply()
        self.assertNotEqual(first, self.template.read_text())

    def test_drift_fails_and_retains_report(self):
        self.add_patch("0001.patch")
        self.target.write_text("different upstream change\n")
        with self.assertRaisesRegex(ValueError, "no longer applies"):
            self.apply()
        self.assertEqual(self.target.read_text(), "different upstream change\n")
        self.assertEqual(json.loads((self.work / "patches.json").read_text())["patches"][0]["status"], "failed")

    def test_patch_failure_stops_build_before_configure_or_publication(self):
        (self.source / "constraints").unlink()
        self.template.unlink()
        self.template.parent.rmdir()
        (self.source / ".git").rename(self.work / "git-state")
        self.source.rmdir()
        (self.work / builder.MARKER).touch()
        with patch.object(builder, "preflight"), patch.object(builder, "capture", return_value="a" * 40), \
                patch.object(builder, "cache_settings", return_value=[]), \
                patch.object(builder, "run") as command, \
                patch.object(builder, "apply_branch_patches", side_effect=ValueError("patch drift")):
            with self.assertRaisesRegex(ValueError, "patch drift"):
                builder.build(self.work, "202605", "a" * 40, 3, cache_mode="off")
        calls = [call.args for call in command.call_args_list]
        self.assertIn(("make", "UNATTENDED=y", "init"), calls)
        self.assertFalse(any("configure" in call or "docker" in call for call in calls))
        self.assertFalse((self.work / "validated.json").exists())


if __name__ == "__main__":
    unittest.main()
