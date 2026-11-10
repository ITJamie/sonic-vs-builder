import gzip
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("kvm_builder", Path(__file__).parents[1] / "scripts/sonic_vs.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
HEADER = b"QFI\xfb" + (3).to_bytes(4, "big") + b"\0" * 64


class KVM(unittest.TestCase):
    def test_selections_preserve_build_order_and_legacy_both(self):
        self.assertEqual(builder.selected_images("all"), ["vs", "kvm", "broadcom"])
        self.assertEqual(builder.selected_images("both"), ["vs", "broadcom"])
        self.assertEqual(builder.selected_images("kvm"), ["kvm"])
        with self.assertRaises(ValueError):
            builder.selected_images("invalid")

    def test_qcow2_gzip_validation_rejects_wrong_format_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.gz"
            archive.write_bytes(gzip.compress(HEADER + b"disk payload"))
            builder.validate_kvm(archive)
            for content in (gzip.compress(b"not qcow2"), gzip.compress(HEADER)[:-4],
                            gzip.compress(HEADER)[:-8] + b"\0" * 8):
                archive.write_bytes(content)
                with self.assertRaises((ValueError, OSError, EOFError)):
                    builder.validate_kvm(archive)

    def test_kvm_configures_vs_builds_disk_and_never_loads_docker_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / builder.MARKER).touch()

            def command(*args, **kwargs):
                if "target/sonic-vs.img.gz" in args:
                    output = work / "source/target/sonic-vs.img.gz"
                    output.parent.mkdir()
                    output.write_bytes(gzip.compress(HEADER + b"disk"))

            with patch.object(builder, "preflight") as preflight, \
                    patch.object(builder, "capture", return_value="a" * 40), \
                    patch.object(builder, "cache_settings", return_value=[]), \
                    patch.object(builder, "apply_branch_patches", return_value=[]), \
                    patch.object(builder, "load_untagged") as load, \
                    patch.object(builder, "run", side_effect=command) as run:
                builder.build(work, "202605", "a" * 40, 3, "kvm")
            preflight.assert_called_once_with(work.resolve(), require_kvm=True)
            calls = [call.args for call in run.call_args_list]
            self.assertIn(("make", "UNATTENDED=y", "ENABLE_ZTP=n", "configure", "PLATFORM=vs", "PLATFORM_ARCH=amd64"), calls)
            self.assertIn(("make", "UNATTENDED=y", "ENABLE_ZTP=n", "SONIC_BUILD_JOBS=3", "target/sonic-vs.img.gz"), calls)
            load.assert_not_called()
            metadata = json.loads((work / "validated.json").read_text())
            self.assertEqual(metadata["platform"], "kvm")
            self.assertEqual(metadata["configured_platform"], "vs")
            self.assertEqual(metadata["image_format"], "qcow2")
            self.assertNotIn("image_id", metadata)

    def test_workflow_orders_docker_kvm_broadcom(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/build.yml").read_text()
        self.assertLess(workflow.index("Build and validate VS"), workflow.index("Build and validate KVM"))
        self.assertLess(workflow.index("Clean VS workspace"), workflow.index("Create KVM workspace"))
        self.assertLess(workflow.index("Clean KVM workspace"), workflow.index("Create Broadcom workspace"))


if __name__ == "__main__":
    unittest.main()
