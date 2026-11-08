import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sonic_vs", Path(__file__).parents[1] / "scripts/sonic_vs.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
COMMIT = "a" * 40


class Inputs(unittest.TestCase):
    def test_trim_and_deduplicate(self):
        self.assertEqual(builder.branches(" 202605,202511, 202605 "), ["202605", "202511"])

    def test_invalid_names(self):
        for value in ("", "202605,", ",202605", "feature/test", "-flag", "a b", "x" * 129, "$(id)", "a..b"):
            with self.subTest(value=value), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                builder.branches(value)

    def test_positive_jobs(self):
        for value in ("0", "-1", "1.5", "one", "1;id", ""):
            with self.assertRaises(ValueError):
                builder.positive(value)
        self.assertEqual(builder.positive("4"), 4)

    @patch.object(builder, "capture")
    def test_resolve_multiple_exact_refs(self, capture):
        capture.side_effect = [f"{COMMIT}\trefs/heads/202605", f"{COMMIT}\trefs/heads/202511"]
        matrix = builder.prepare("202605,202511", "1")
        self.assertEqual(matrix, {"include": [{"branch": b, "commit": COMMIT, "images": ["vs", "broadcom"]}
                                              for b in ("202605", "202511")]})

    @patch.object(builder, "capture", return_value=f"{COMMIT}\trefs/heads/202605")
    def test_select_single_platform(self, _capture):
        self.assertEqual(builder.prepare("202605", "3", "broadcom")["include"][0]["images"], ["broadcom"])
        with self.assertRaises(ValueError):
            builder.prepare("202605", "3", "unknown")

    @patch.object(builder, "capture")
    def test_missing_branch_aborts_whole_preparation(self, capture):
        capture.side_effect = [f"{COMMIT}\trefs/heads/202605", subprocess.CalledProcessError(2, "git")]
        with self.assertRaises(subprocess.CalledProcessError):
            builder.prepare("202605,missing", "1")

    @patch.object(builder, "capture", return_value=f"{COMMIT}\trefs/heads/different")
    def test_unexpected_ref_rejected(self, _capture):
        with self.assertRaises(ValueError):
            builder.prepare("202605", "1")


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / builder.MARKER).touch()

    def archive(self, config=None, count=1):
        config = json.dumps(config or {"os": "linux", "architecture": "amd64"}).encode()
        name = hashlib.sha256(config).hexdigest() + ".json"
        archive = self.work / "image.gz"
        with tarfile.open(archive, "w:gz") as output:
            for filename, data in ((name, config), ("manifest.json", json.dumps([
                {"Config": name, "RepoTags": ["docker-sonic-vs:latest"], "Layers": []}
            ] * count).encode())):
                member = tarfile.TarInfo(filename)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        return archive, "sha256:" + name[:-5]

    def test_archive_identity_is_config_hash(self):
        archive, image = self.archive()
        self.assertEqual(builder.archive_image(archive), image)

    @patch.object(builder, "preflight")
    @patch.object(builder, "capture", return_value=COMMIT)
    @patch.object(builder, "smoke")
    def test_broadcom_build_uses_correct_target_without_docker_publication(self, smoke, _capture, _preflight):
        def command(*args, **kwargs):
            if args[0] == "make" and "target/sonic-broadcom.bin" in args:
                output = self.work / "source/target/sonic-broadcom.bin"
                output.parent.mkdir()
                output.write_bytes(b"#!/bin/sh\ninstaller payload")

        with patch.object(builder, "run", side_effect=command) as run, \
                patch.object(builder, "cache_settings", return_value=[]):
            builder.build(self.work, "202605", COMMIT, "3", "broadcom")
        calls = [call.args for call in run.call_args_list]
        self.assertIn(("make", "UNATTENDED=y", "configure", "PLATFORM=broadcom", "PLATFORM_ARCH=amd64"), calls)
        self.assertIn(("make", "UNATTENDED=y", "SONIC_BUILD_JOBS=3", "target/sonic-broadcom.bin"), calls)
        smoke.assert_not_called()
        metadata = json.loads((self.work / "validated.json").read_text())
        self.assertEqual(metadata["platform"], "broadcom")
        self.assertEqual(metadata["sha256"], builder.checksum(self.work / "source/target/sonic-broadcom.bin"))
        self.assertNotIn("image_id", metadata)

    def test_reject_wrong_architecture_and_multiple_images(self):
        for config, count in (({"os": "linux", "architecture": "arm64"}, 1), (None, 2)):
            archive, _ = self.archive(config, count)
            with self.assertRaises(ValueError):
                builder.archive_image(archive)

    def test_load_does_not_import_upstream_tags(self):
        archive, _ = self.archive()

        class LoadProcess:
            def __init__(self, *args, **kwargs):
                class Buffer(io.BytesIO):
                    def close(self):
                        self.saved = self.getvalue()
                        super().close()
                self.stdin = Buffer()
                self.returncode = None
                processes.append(self)

            def wait(self):
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

        processes = []
        with patch.object(builder.subprocess, "Popen", LoadProcess):
            builder.load_untagged(archive)
        with tarfile.open(fileobj=io.BytesIO(processes[0].stdin.saved)) as loaded:
            self.assertEqual(json.load(loaded.extractfile("manifest.json"))[0]["RepoTags"], [])

    @patch.object(builder, "run")
    def test_publication_requires_successful_validation(self, run):
        with self.assertRaises(FileNotFoundError):
            builder.publish(self.work, "ITJamie/sonic-vs-builder")
        run.assert_not_called()

    @patch.object(builder, "preflight")
    @patch.object(builder, "run", side_effect=subprocess.CalledProcessError(1, "make"))
    def test_build_failure_never_creates_validation(self, _run, _preflight):
        with self.assertRaises(subprocess.CalledProcessError):
            builder.build(self.work, "202605", COMMIT, "1")
        self.assertFalse((self.work / "validated.json").exists())

    @patch.object(builder, "capture", return_value="linux/arm64")
    @patch.object(builder, "run")
    def test_smoke_rejects_wrong_architecture(self, run, _capture):
        with self.assertRaises(ValueError):
            builder.smoke("sha256:example")
        run.assert_not_called()

    @patch.object(builder, "preflight")
    @patch.object(builder, "run")
    @patch.object(builder, "capture", return_value=COMMIT)
    @patch.object(builder, "archive_image", return_value="sha256:example")
    @patch.object(builder, "load_untagged")
    @patch.object(builder, "cache_settings", return_value=[])
    @patch.object(builder, "smoke", side_effect=subprocess.CalledProcessError(1, "docker"))
    def test_smoke_failure_never_creates_validation(self, *_mocks):
        with patch.object(builder.subprocess, "run") as process:
            process.return_value.returncode = 1
            with self.assertRaises(subprocess.CalledProcessError):
                builder.build(self.work, "202605", COMMIT, "1")
        self.assertTrue((self.work / "image-state.json").exists())
        self.assertFalse((self.work / "validated.json").exists())

    def test_cleanup_refuses_unowned_directory(self):
        (self.work / builder.MARKER).unlink()
        with self.assertRaises(ValueError):
            builder.cleanup(self.work)
        self.assertTrue(self.work.exists())

    @patch.object(builder, "run")
    @patch.object(builder, "capture")
    @patch.object(builder, "branches", return_value=["202605"])
    def test_publish_exact_tag_and_digest(self, _branches, capture, run):
        repository = "itjamie/sonic-vs-builder"
        digest = "sha256:" + "b" * 64
        builder.write_json(self.work / "validated.json", {
            "upstream": builder.UPSTREAM, "branch": "202605", "commit": COMMIT,
            "image_id": "sha256:" + "c" * 64,
        })
        capture.return_value = json.dumps([f"ghcr.io/{repository}@{digest}"])
        result = subprocess.CompletedProcess([], 1, stdout="")
        with patch.dict(os.environ, {"GITHUB_ACTOR": "ITJamie", "GHCR_TOKEN": "test-token"}), \
                patch.object(builder.subprocess, "run", return_value=result):
            builder.publish(self.work, "ITJamie/sonic-vs-builder")
        metadata = json.loads((self.work / "metadata.json").read_text())
        self.assertEqual(metadata["image"], f"ghcr.io/{repository}:202605")
        self.assertEqual(metadata["digest"], digest)
        self.assertEqual(metadata["commit"], COMMIT)
        calls = [call.args for call in run.call_args_list]
        self.assertIn(("docker", "push", f"ghcr.io/{repository}:202605"), calls)
        self.assertFalse(any(":latest" in str(call) for call in calls))
        login_env = run.call_args_list[0].kwargs["env"]
        self.assertFalse(Path(login_env["DOCKER_CONFIG"]).exists())

    @patch.object(builder, "run")
    @patch.object(builder.subprocess, "run")
    def test_cleanup_preserves_preexisting_image_and_restores_tag(self, process, run):
        builder.write_json(self.work / "image-state.json", {"image_id": "sha256:existing", "preexisting": True})
        builder.write_json(self.work / "tag-state.json", {
            "reference": "ghcr.io/itjamie/sonic-vs-builder:202605", "previous": "sha256:previous",
        })
        builder.cleanup(self.work)
        run.assert_called_once_with("docker", "tag", "sha256:previous", "ghcr.io/itjamie/sonic-vs-builder:202605")
        process.assert_not_called()
        self.assertFalse(self.work.exists())

    @patch.object(builder, "run", side_effect=subprocess.CalledProcessError(1, "docker"))
    @patch.object(builder, "branches", return_value=["202605"])
    def test_publish_login_failure_removes_temporary_credentials(self, _branches, run):
        builder.write_json(self.work / "validated.json", {"branch": "202605", "image_id": "sha256:example"})
        with patch.dict(os.environ, {"GITHUB_ACTOR": "ITJamie", "GHCR_TOKEN": "test-token"}):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.publish(self.work, "ITJamie/sonic-vs-builder")
        self.assertFalse(Path(run.call_args.kwargs["env"]["DOCKER_CONFIG"]).exists())
        self.assertFalse((self.work / "metadata.json").exists())


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / "work"
        self.work.mkdir()
        (self.work / builder.MARKER).touch()
        self.source = self.work / "source"
        (self.source / "rules").mkdir(parents=True)
        self.variables = ("SONIC_DPKG_CACHE_METHOD", "SONIC_DPKG_CACHE_SOURCE",
                          "SONIC_VERSION_CACHE_METHOD", "SONIC_VERSION_CACHE_SOURCE")
        self.config = "\n".join(f"{name} ?= none" for name in self.variables)
        (self.source / "rules/config").write_text(self.config)
        (self.source / "Makefile.work").write_text(self.config)
        self.root = self.base / "persistent"
        disk = patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4))
        disk.start()
        self.addCleanup(disk.stop)

    def test_cache_survives_workspace_cleanup_and_is_reused(self):
        args = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        packages = self.root / "v1/branches/202605/amd64/packages"
        downloads = self.root / "v1/downloads/amd64"
        (packages / "package.tgz").write_bytes(b"dependency-checked package")
        (downloads / "download.bin").write_bytes(b"versioned download")
        again = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        self.assertEqual(args, again)
        self.assertIn(f"SONIC_DPKG_CACHE_SOURCE={packages}", args)
        self.assertIn(f"SONIC_VERSION_CACHE_SOURCE={downloads}", args)
        self.assertIn("SONIC_DPKG_CACHE_METHOD=cache", args)
        self.assertIn("SONIC_VERSION_CACHE_METHOD=cache", args)
        self.assertEqual((self.source / "rules/config").read_text(), self.config)
        self.assertEqual((self.source / "Makefile.work").read_text(), self.config)
        builder.cleanup(self.work)
        self.assertEqual((packages / "package.tgz").read_bytes(), b"dependency-checked package")
        self.assertEqual((downloads / "download.bin").read_bytes(), b"versioned download")

    @patch.object(builder, "preflight")
    @patch.object(builder, "capture", return_value=COMMIT)
    def test_build_forwards_native_cache_flags_through_every_make_stage(self, _capture, _preflight):
        work = self.base / "build-work"
        work.mkdir()
        (work / builder.MARKER).touch()

        def command(*args, **kwargs):
            if args[0] == "git" and "--detach" in args:
                source = work / "source"
                (source / "rules").mkdir()
                (source / "rules/config").write_text(self.config)
                (source / "Makefile.work").write_text(self.config)
            if args[0] == "make" and "target/sonic-broadcom.bin" in args:
                target = work / "source/target"
                target.mkdir()
                (target / "sonic-broadcom.bin").write_bytes(b"#!/bin/sh\ninstaller")

        with patch.object(builder, "run", side_effect=command) as run:
            builder.build(work, "202605", COMMIT, "3", "broadcom", "read-write", str(self.root))
        makes = [call for call in run.call_args_list if call.args[0] == "make"]
        self.assertEqual(len(makes), 3)
        for call in makes:
            self.assertIn("SONIC_DPKG_CACHE_METHOD=cache", call.args)
            self.assertIn(f"SONIC_DPKG_CACHE_SOURCE={self.root}/v1/branches/202605/amd64/packages", call.args)
            self.assertIn("SONIC_VERSION_CACHE_METHOD=cache", call.args)
            self.assertIn(f"SONIC_VERSION_CACHE_SOURCE={self.root}/v1/downloads/amd64", call.args)

    def test_platforms_share_cache_paths_but_branches_partition_packages(self):
        vs = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        broadcom = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        older = builder.cache_settings(self.source, self.work, "202511", directory=str(self.root))
        self.assertEqual(vs, broadcom)
        self.assertNotEqual(vs[1], older[1])
        self.assertEqual(vs[3], older[3])

    def test_old_branch_uses_package_cache_without_unsupported_download_cache(self):
        old_config = "\n".join(f"{name} ?= none" for name in self.variables[:2])
        (self.source / "rules/config").write_text(old_config)
        (self.source / "Makefile.work").write_text(old_config)
        args = builder.cache_settings(self.source, self.work, "202111", directory=str(self.root))
        self.assertIn("SONIC_DPKG_CACHE_METHOD=cache", args)
        self.assertIn("SONIC_VERSION_CACHE_METHOD=none", args)
        self.assertFalse((self.root / "v1/downloads").exists())
        self.assertIsNone(json.loads((self.work / "cache.json").read_text())["downloads"])

    def test_off_does_not_create_or_access_persistent_storage(self):
        args = builder.cache_settings(self.base / "nonexistent", self.work, "202605", "off", str(self.root))
        self.assertEqual(args, ["SONIC_DPKG_CACHE_METHOD=none", "SONIC_VERSION_CACHE_METHOD=none"])
        self.assertFalse(self.root.exists())

    def test_environment_override_and_home_default(self):
        with patch.dict(os.environ, {"SONIC_CACHE_DIR": str(self.root)}):
            builder.cache_settings(self.source, self.work, "202605")
        self.assertTrue(self.root.exists())
        with patch.dict(os.environ, {"SONIC_CACHE_DIR": ""}), patch.object(builder.Path, "home", return_value=self.base / "home"):
            builder.cache_settings(self.source, self.work, "202605")
        self.assertTrue((self.base / "home/.cache/sonic-vs-builder/v1").exists())

    def test_reject_temporary_or_make_unsafe_paths(self):
        for path in (str(self.work / "cache"), "relative/path", str(self.root) + " bad", str(self.root) + "$(id)"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                builder.cache_settings(self.source, self.work, "202605", directory=path)

    def test_low_space_prevents_building_into_cache(self):
        with patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=1024)):
            with self.assertRaisesRegex(ValueError, "300 GiB"):
                builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))

    def test_unsupported_branch_reports_bypass_option(self):
        (self.source / "rules/config").write_text("")
        with self.assertRaisesRegex(ValueError, "cache_mode=off"):
            builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))


if __name__ == "__main__":
    unittest.main()
