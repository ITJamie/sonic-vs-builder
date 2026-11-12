import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
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
        self.assertEqual(matrix, {"include": [{"branch": b, "commit": COMMIT, "images": ["vs", "kvm", "broadcom"]}
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
            ] * count).encode()), ("index.json", json.dumps({"schemaVersion": 2, "manifests": [
                {"annotations": {"io.containerd.image.name": "docker.io/library/docker-sonic-vs:latest",
                                 "org.opencontainers.image.ref.name": "latest", "keep": "value"}}
            ]}).encode())):
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
    @patch.object(builder, "apply_branch_patches", return_value=[])
    def test_broadcom_build_uses_correct_target_without_docker_publication(self, _patches, smoke, _capture, _preflight):
        def command(*args, **kwargs):
            if args[0] == "make" and "target/sonic-broadcom.bin" in args:
                output = self.work / "source/target/sonic-broadcom.bin"
                output.parent.mkdir()
                output.write_bytes(b"#!/bin/sh\ninstaller payload")

        with patch.object(builder, "run", side_effect=command) as run, \
                patch.object(builder, "cache_settings", return_value=[]):
            builder.build(self.work, "202605", COMMIT, "3", "broadcom")
        calls = [call.args for call in run.call_args_list]
        version = json.loads((self.work / "validated.json").read_text())["image_version"]
        self.assertIn(("make", "UNATTENDED=y", f"SONIC_IMAGE_VERSION={version}", "ENABLE_ZTP=n", "configure", "PLATFORM=broadcom", "PLATFORM_ARCH=amd64"), calls)
        self.assertIn(("make", "UNATTENDED=y", f"SONIC_IMAGE_VERSION={version}", "ENABLE_ZTP=n", "SONIC_BUILD_JOBS=3", "target/sonic-broadcom.bin"), calls)
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
                kwargs["stdout"].write("Loaded image ID: sha256:" + "d" * 64 + "\n")
                kwargs["stdout"].flush()
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
            self.assertEqual(builder.load_untagged(archive), "sha256:" + "d" * 64)
        with tarfile.open(fileobj=io.BytesIO(processes[0].stdin.saved)) as loaded:
            self.assertEqual(json.load(loaded.extractfile("manifest.json"))[0]["RepoTags"], [])
            self.assertEqual(json.load(loaded.extractfile("index.json"))["manifests"][0]["annotations"], {"keep": "value"})

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

    @patch.object(builder, "capture", return_value="linux/amd64")
    def test_legacy_smoke_without_rsync_still_executes_required_tools(self, _capture):
        version = self.work / "version.yml"
        version.write_text("sonic_os_version: 12\n")
        supervisor = self.work / "supervisord"
        supervisor.write_text("#!/bin/sh\nprintf '4.2.5\\n'\n")
        supervisor.chmod(0o755)
        cfggen = self.work / "sonic-cfggen"
        cfggen.write_text('#!/bin/sh\ntest "$1" = --help\n')
        cfggen.chmod(0o755)

        def execute_shell(*command):
            shell = command[-1].replace("/etc/sonic/sonic_version.yml", shlex.quote(str(version)))
            shell = shell.replace("/usr/local/bin/supervisord", shlex.quote(str(supervisor)))
            shell = shell.replace("sonic-cfggen", shlex.quote(str(cfggen)))
            shell = shell.replace("rsync --version", "sonic-vs-test-missing-rsync --version")
            subprocess.run(["/bin/sh", "-ec", shell], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        with patch.object(builder, "run", side_effect=execute_shell):
            checks = builder.smoke("sha256:legacy", require_rsync=False)
            self.assertIn("sonic-cfggen --help", checks)
            with self.assertRaises(subprocess.CalledProcessError) as missing:
                builder.smoke("sha256:modern", require_rsync=True)
            self.assertEqual(missing.exception.returncode, 127)
            cfggen.write_text("#!/bin/sh\nexit 23\n")
            with self.assertRaises(subprocess.CalledProcessError) as broken:
                builder.smoke("sha256:legacy", require_rsync=False)
            self.assertEqual(broken.exception.returncode, 23)

    @patch.object(builder, "capture", return_value="linux/amd64")
    def test_smoke_rejects_a_cached_image_with_a_stale_version(self, _capture):
        version = "202605-itjamie.20261112-22"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "sonic_version.yml"
            metadata.write_text(f"build_version: '{version}'\n")
            supervisor = root / "supervisord"
            supervisor.write_text("#!/bin/sh\nprintf '4.2.5\\n'\n")
            supervisor.chmod(0o755)
            cfggen = root / "sonic-cfggen"
            cfggen.write_text(f'#!/bin/sh\nif [ "$1" != --help ]; then printf "%s\\n" "{version}"; fi\n')
            cfggen.chmod(0o755)

            def shell(*command):
                script = command[-1].replace("/etc/sonic/sonic_version.yml", shlex.quote(str(metadata)))
                script = script.replace("/usr/local/bin/supervisord", shlex.quote(str(supervisor)))
                script = script.replace("sonic-cfggen", shlex.quote(str(cfggen)))
                subprocess.run(["/bin/sh", "-ec", script], check=True, capture_output=True)

            with patch.object(builder, "run", side_effect=shell):
                builder.smoke("image", require_rsync=False, expected_version=version)
                with self.assertRaises(subprocess.CalledProcessError):
                    builder.smoke("image", require_rsync=False, expected_version=version + "-2")

    @patch.object(builder, "preflight")
    @patch.object(builder, "run")
    @patch.object(builder, "capture", return_value=COMMIT)
    @patch.object(builder, "archive_image", return_value="sha256:example")
    @patch.object(builder, "load_untagged", return_value="sha256:example")
    @patch.object(builder, "cache_settings", return_value=[])
    @patch.object(builder, "smoke", side_effect=subprocess.CalledProcessError(1, "docker"))
    @patch.object(builder, "apply_branch_patches", return_value=[])
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


class RegistryPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / builder.MARKER).touch()
        self.image = "sha256:" + "c" * 64
        self.ghcr = "ghcr.io/itjamie/sonic-vs-builder:202605"
        self.hub = "docker.io/itjamie/docker-sonic-vs:202605"
        builder.write_json(self.work / "validated.json", {"branch": "202605", "commit": COMMIT,
                                                         "image_id": self.image, "platform": "vs"})
        patches = [patch.object(builder, "branches", return_value=["202605"]),
                   patch.dict(os.environ, {"GITHUB_ACTOR": "ITJamie", "GHCR_TOKEN": "gh-token",
                                           "DOCKERHUB_USERNAME": "itjamie", "DOCKERHUB_TOKEN": "hub-token",
                                           "GITHUB_STEP_SUMMARY": ""})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        command = patch.object(builder, "run")
        self.command = command.start()
        self.addCleanup(command.stop)
        process = patch.object(builder.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout=""))
        self.process = process.start()
        self.addCleanup(process.stop)
        capture = patch.object(builder, "capture", side_effect=self.digests)
        self.capture = capture.start()
        self.addCleanup(capture.stop)

    def digests(self, *args, **kwargs):
        if args[-1] == self.hub:
            # Docker omits the docker.io prefix in its local RepoDigests.
            return json.dumps(["itjamie/docker-sonic-vs@sha256:" + "2" * 64])
        return json.dumps(["ghcr.io/itjamie/sonic-vs-builder@sha256:" + "1" * 64])

    def test_same_validated_image_pushed_to_both_registries(self):
        builder.publish(self.work, "ITJamie/sonic-vs-builder", dockerhub=True)
        calls = [call.args for call in self.command.call_args_list]
        for destination in (self.ghcr, self.hub):
            self.assertIn(("docker", "tag", self.image, destination), calls)
            self.assertIn(("docker", "push", destination), calls)
        self.assertEqual([call[:3] for call in calls[:2]], [("docker", "login", "ghcr.io"),
                                                          ("docker", "login", "docker.io")])
        metadata = json.loads((self.work / "metadata.json").read_text())
        self.assertEqual(metadata["registries"]["dockerhub"]["digest"], "sha256:" + "2" * 64)
        self.assertEqual(metadata["registries"]["dockerhub"]["image"], self.hub)
        self.assertEqual(metadata["registries"]["ghcr"]["digest"], metadata["digest"])
        self.assertEqual(metadata["image"], self.ghcr)
        self.assertNotIn("hub-token", json.dumps(metadata))
        self.assertFalse(Path(self.command.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"]).exists())

    def test_ghcr_only_does_not_use_hub_even_when_secrets_exist(self):
        builder.publish(self.work, "ITJamie/sonic-vs-builder")
        calls = [call.args for call in self.command.call_args_list]
        self.assertNotIn(("docker", "push", self.hub), calls)
        self.assertFalse(any(call[:3] == ("docker", "login", "docker.io") for call in calls))
        self.assertEqual(set(json.loads((self.work / "metadata.json").read_text())["registries"]), {"ghcr"})

    def test_missing_hub_secret_fails_before_any_registry_changes(self):
        with patch.dict(os.environ, {"DOCKERHUB_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "Missing dockerhub"):
                builder.publish(self.work, "ITJamie/sonic-vs-builder", dockerhub=True)
        self.command.assert_not_called()
        self.process.assert_not_called()

    def test_hub_login_failure_prevents_all_pushes_and_removes_credentials(self):
        def command(*args, **kwargs):
            if args[:3] == ("docker", "login", "docker.io"):
                raise subprocess.CalledProcessError(1, "docker login")
        self.command.side_effect = command
        with self.assertRaises(subprocess.CalledProcessError):
            builder.publish(self.work, "ITJamie/sonic-vs-builder", dockerhub=True)
        self.assertFalse(any(call.args[:2] == ("docker", "push") for call in self.command.call_args_list))
        self.assertFalse(Path(self.command.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"]).exists())
        self.assertFalse((self.work / "metadata.json").exists())

    def test_hub_push_failure_keeps_ghcr_metadata_and_tracks_both_local_tags(self):
        def command(*args, **kwargs):
            if args == ("docker", "push", self.hub):
                raise subprocess.CalledProcessError(1, "docker push")
        self.command.side_effect = command
        with self.assertRaises(subprocess.CalledProcessError):
            builder.publish(self.work, "ITJamie/sonic-vs-builder", dockerhub=True)
        metadata = json.loads((self.work / "metadata.json").read_text())
        self.assertEqual(set(metadata["registries"]), {"ghcr"})
        tags = json.loads((self.work / "tag-state.json").read_text())["tags"]
        self.assertEqual([tag["reference"] for tag in tags], [self.ghcr, self.hub])
        self.assertFalse(Path(self.command.call_args_list[0].kwargs["env"]["DOCKER_CONFIG"]).exists())

    def test_cleanup_restores_existing_tag_and_removes_new_hub_tag(self):
        builder.write_json(self.work / "tag-state.json", {"tags": [
            {"reference": self.ghcr, "previous": "sha256:previous"},
            {"reference": self.hub, "previous": None},
        ]})
        builder.cleanup(self.work)
        self.command.assert_called_once_with("docker", "tag", "sha256:previous", self.ghcr)
        self.process.assert_called_once_with(["docker", "image", "rm", self.hub], check=False)


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
        self.config = "\n".join(f"{name} ?= none" for name in self.variables) + "\n# DOCKER_BUILDER_USER_MOUNT\n"
        (self.source / "rules/config").write_text(self.config)
        (self.source / "Makefile.work").write_text(self.config)
        self.root = self.base / "persistent"
        disk = patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4))
        disk.start()
        self.addCleanup(disk.stop)
        permissions = patch.object(builder, "prepare_cache_permissions")
        self.permissions = permissions.start()
        self.addCleanup(permissions.stop)

    def test_cache_survives_workspace_cleanup_and_is_reused(self):
        args = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        packages = self.root / "v1/branches/202605/amd64/packages"
        local_packages = self.root / "v1/branches/202605/amd64/local-packages"
        downloads = self.root / "v1/downloads/amd64"
        (packages / "package.tgz").write_bytes(b"dependency-checked package")
        (local_packages / "modified-package.tgz").write_bytes(b"dependency-checked modified package")
        (downloads / "download.bin").write_bytes(b"versioned download")
        again = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        self.assertEqual(args, again)
        self.assertIn(f"SONIC_DPKG_CACHE_SOURCE={packages}", args)
        self.assertIn(f"SONIC_VERSION_CACHE_SOURCE={downloads}", args)
        self.assertIn("SONIC_DPKG_CACHE_METHOD=cache", args)
        self.assertIn("SONIC_VERSION_CACHE_METHOD=cache", args)
        self.assertIn(f"DOCKER_BUILDER_USER_MOUNT={local_packages}:/sonic/target/cache", args)
        self.permissions.assert_called_with((packages, local_packages, downloads))
        self.assertEqual((self.source / "rules/config").read_text(), self.config)
        self.assertEqual((self.source / "Makefile.work").read_text(), self.config)
        builder.cleanup(self.work)
        self.assertEqual((packages / "package.tgz").read_bytes(), b"dependency-checked package")
        self.assertEqual((local_packages / "modified-package.tgz").read_bytes(), b"dependency-checked modified package")
        self.assertEqual((downloads / "download.bin").read_bytes(), b"versioned download")

    @patch.object(builder, "preflight")
    @patch.object(builder, "capture", return_value=COMMIT)
    @patch.object(builder, "apply_branch_patches", return_value=[])
    def test_build_forwards_native_cache_flags_through_every_make_stage(self, _patches, _capture, _preflight):
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
            self.assertIn(f"DOCKER_BUILDER_USER_MOUNT={self.root}/v1/branches/202605/amd64/local-packages:/sonic/target/cache", call.args)

    def test_platforms_share_cache_paths_but_branches_partition_packages(self):
        vs = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        broadcom = builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))
        older = builder.cache_settings(self.source, self.work, "202511", directory=str(self.root))
        self.assertEqual(vs, broadcom)
        self.assertNotEqual(vs[1], older[1])
        self.assertEqual(vs[3], older[3])

    def test_old_branch_uses_package_cache_without_unsupported_download_cache(self):
        old_config = "\n".join(f"{name} ?= none" for name in self.variables[:2]) + "\n# DOCKER_BUILDER_USER_MOUNT\n"
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
        self.permissions.assert_not_called()

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

    def test_missing_local_cache_mount_support_cannot_silently_discard_packages(self):
        (self.source / "Makefile.work").write_text(self.config.replace("DOCKER_BUILDER_USER_MOUNT", ""))
        with self.assertRaisesRegex(ValueError, "local package cache"):
            builder.cache_settings(self.source, self.work, "202605", directory=str(self.root))


if __name__ == "__main__":
    unittest.main()
