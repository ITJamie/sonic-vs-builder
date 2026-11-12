"""Keep the dispatch ZTP setting consistent across selected image builds."""
import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("ztp_builder", ROOT / "scripts/sonic_vs.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ZTP(unittest.TestCase):
    def test_enabled_and_disabled_reach_configuration_and_each_output(self):
        for platform, filename in builder.TARGETS.items():
            for enabled in (False, True):
                with self.subTest(platform=platform, enabled=enabled), tempfile.TemporaryDirectory() as directory:
                    work = Path(directory)
                    (work / builder.MARKER).touch()

                    def command(*args, **kwargs):
                        if f"target/{filename}" in args:
                            output = work / "source/target" / filename
                            output.parent.mkdir()
                            content = (gzip.compress(b"QFI\xfb" + (3).to_bytes(4, "big") + b"\0" * 64)
                                       if platform == "kvm" else b"#!/bin/sh\npayload")
                            output.write_bytes(content)

                    with patch.object(builder, "preflight"), \
                            patch.object(builder, "capture", return_value="a" * 40), \
                            patch.object(builder, "cache_settings", return_value=[]), \
                            patch.object(builder, "apply_branch_patches", return_value=[]), \
                            patch.object(builder, "archive_image", return_value="sha256:" + "b" * 64), \
                            patch.object(builder, "load_untagged", return_value="sha256:" + "c" * 64), \
                            patch.object(builder, "smoke", return_value=[]) as smoke, \
                            patch.object(builder, "run", side_effect=command) as run:
                        version = "202505-itjamie.20261112-22" + ("-ztp" if enabled else "")
                        builder.build(work, "202505", "a" * 40, 3, platform, enable_ztp=enabled,
                                      image_version=version)
                    calls = [call.args for call in run.call_args_list if call.args[0] == "make"]
                    flag = "ENABLE_ZTP=y" if enabled else "ENABLE_ZTP=n"
                    for call in calls:
                        if "configure" in call or f"target/{filename}" in call:
                            self.assertIn(flag, call)
                            self.assertIn(f"SONIC_IMAGE_VERSION={version}", call)
                    metadata = json.loads((work / "validated.json").read_text())
                    self.assertIs(metadata["enable_ztp"], enabled)
                    self.assertEqual(metadata["image_version"], version)
                    if platform == "vs":
                        smoke.assert_called_once_with(
                            "sha256:" + "c" * 64, require_rsync=False, expected_version=version)

    def test_workflow_forwards_dispatch_input_to_every_build(self):
        workflow = (ROOT / ".github/workflows/build.yml").read_text()
        self.assertIn("      enable_ztp:\n", workflow)
        # Preparation, native version reconstruction, and all three builds.
        self.assertEqual(workflow.count('ENABLE_ZTP: ${{ inputs.enable_ztp }}'), 5)
        self.assertEqual(workflow.count('--enable-ztp "$ENABLE_ZTP"'), 3)


if __name__ == "__main__":
    unittest.main()
