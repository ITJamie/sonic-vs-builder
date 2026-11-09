import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("cache_builder", Path(__file__).parents[1] / "scripts/sonic_vs.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
SWITCH = "ifneq ($(strip $(SONIC_CONFIG_USE_DOCKER_CACHE)),y)\nDOCKER_NO_CACHE_FLAG = --no-cache\nendif\n"


class DockerCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = self.root / "sonic-slave-bookworm/Dockerfile.j2"
        self.template.parent.mkdir()
        self.template.write_text("FROM debian:bookworm\nRUN echo install\nFROM debian:bookworm AS final\nRUN echo final\n")
        self.versions = self.root / "files/build/versions-public/build/slave/versions-py3"
        self.versions.parent.mkdir(parents=True)
        self.versions.write_text("wcwidth==0.9.2\n")
        self.hook = self.root / "src/sonic-build-hooks/scripts/hook"
        self.hook.parent.mkdir(parents=True)
        self.hook.write_text("original hook\n")

    def test_supported_enabled_and_disabled_flags(self):
        (self.root / "Makefile.work").write_text(SWITCH)
        for enabled in (True, False):
            args, metadata = builder.docker_cache_settings(self.root, enabled)
            self.assertEqual(args, [f"SONIC_CONFIG_USE_DOCKER_CACHE={'y' if enabled else 'n'}"])
            self.assertEqual(metadata, {"requested": enabled, "supported": True, "enabled": enabled})

    def test_old_branch_retains_native_behavior(self):
        (self.root / "Makefile.work").write_text("docker build --no-cache\n")
        args, metadata = builder.docker_cache_settings(self.root, True)
        self.assertEqual(args, [])
        self.assertFalse(metadata["supported"])
        self.assertFalse(metadata["enabled"])

    def test_stable_stamp_before_every_stage_install(self):
        first = builder.stamp_docker_cache_inputs(self.root, [])
        text = self.template.read_text()
        self.assertEqual(text.count("ARG SONIC_VS_BUILDER_LAYER_INPUTS="), 2)
        self.assertRegex(text, r"FROM debian:bookworm\nARG SONIC_VS_BUILDER_LAYER_INPUTS=.*\nRUN")
        self.assertEqual(first["input_files"], 2)
        self.assertEqual(builder.stamp_docker_cache_inputs(self.root, []), first)
        self.assertEqual(text, self.template.read_text())

    def test_constraints_hooks_and_patch_changes_invalidate(self):
        keys = [builder.stamp_docker_cache_inputs(self.root, [])["input_sha256"]]
        self.versions.write_text("wcwidth==0.9.3\n")
        keys.append(builder.stamp_docker_cache_inputs(self.root, [])["input_sha256"])
        self.hook.write_text("changed hook\n")
        keys.append(builder.stamp_docker_cache_inputs(self.root, [])["input_sha256"])
        patches = [{"path": "202605/fix.patch", "sha256": "a" * 64, "status": "applied"}]
        keys.append(builder.stamp_docker_cache_inputs(self.root, patches)["input_sha256"])
        self.assertEqual(len(set(keys)), 4)
        patches[0]["status"] = "already-applied"
        self.assertEqual(builder.stamp_docker_cache_inputs(self.root, patches)["input_sha256"], keys[-1])

    def test_generated_hook_package_and_staging_do_not_invalidate(self):
        hooks = self.hook.parents[1]
        package = hooks / "buildinfo/sonic-build-hooks_1.0_all.deb"
        config = hooks / "buildinfo/config/buildinfo.config"
        config.parent.mkdir(parents=True)
        config.write_text("tracked configuration")
        first = builder.stamp_docker_cache_inputs(self.root, [])
        package.write_bytes(b"first package with timestamp metadata")
        staging = hooks / "tmp/sonic-build-hooks/usr/local/share/buildinfo/scripts/hook"
        staging.parent.mkdir(parents=True)
        staging.write_text("generated staging copy")
        self.assertEqual(builder.stamp_docker_cache_inputs(self.root, []), first)
        package.write_bytes(b"rebuilt package with different timestamps")
        staging.write_text("new staging copy")
        self.assertEqual(builder.stamp_docker_cache_inputs(self.root, []), first)
        config.write_text("changed tracked configuration")
        self.assertNotEqual(builder.stamp_docker_cache_inputs(self.root, [])["input_sha256"], first["input_sha256"])

    def test_cache_enabled_flags_and_metadata_reach_build_with_package_cache_off(self):
        work = self.root / "work"
        work.mkdir()
        (work / builder.MARKER).touch()

        def command(*args, **kwargs):
            source = work / "source"
            if args[0] == "git" and "--detach" in args:
                (source / "Makefile.work").write_text(SWITCH)
                template = source / "sonic-slave-bookworm/Dockerfile.j2"
                template.parent.mkdir()
                template.write_text("FROM debian:bookworm\nRUN echo install\n")
            if "target/sonic-broadcom.bin" in args:
                output = source / "target/sonic-broadcom.bin"
                output.parent.mkdir()
                output.write_bytes(b"#!/bin/sh\ninstaller")

        with patch.object(builder, "preflight"), patch.object(builder, "capture", return_value="a" * 40), \
                patch.object(builder, "apply_branch_patches", return_value=[]), \
                patch.object(builder, "run", side_effect=command) as run:
            builder.build(work, "202605", "a" * 40, 3, "broadcom", "off")
        makes = [call.args for call in run.call_args_list if call.args[0] == "make"]
        self.assertEqual(len(makes), 3)
        self.assertTrue(all("SONIC_CONFIG_USE_DOCKER_CACHE=y" in args for args in makes))
        metadata = json.loads((work / "validated.json").read_text())["docker_layer_cache"]
        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata, json.loads((work / "docker-cache.json").read_text()))


if __name__ == "__main__":
    unittest.main()
