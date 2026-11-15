"""Fork selection must reach checkout, caches and release provenance."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import sonic_vs as builder
import releases

FORK = "ITJamie/sonic-buildimage"
URL = "https://github.com/ITJamie/sonic-buildimage.git"
OVERRIDES = {"src/sonic-platform-common": "ITJamie/fork-sonic-platform-common"}
COMMIT = "a" * 40


class SourceRepository(unittest.TestCase):
    def test_reject_unsafe_repositories_and_override_paths_before_resolution(self):
        for repository in ("", "-option/repo", "https://github.com/a/b", "a/b.git",
                           "a/..", "a/b/c", "a/b;id", "a/b\n"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                builder.source_url(repository)
        for value in ('[]', 'null', '{"../outside":"a/b"}', '{"src/./x":"a/b"}',
                      '{"src/x":"ssh://host/repo"}', '{"src/x":null}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.submodule_overrides(value)
        with patch.object(builder, "capture") as capture, self.assertRaises(ValueError):
            builder.prepare("accton-fixes", "3", "broadcom", FORK, '[]')
        capture.assert_not_called()

    def test_prepare_pins_selected_fork_and_retains_release_source(self):
        with patch.object(builder, "capture", return_value=f"{COMMIT}\trefs/heads/accton-fixes") as capture:
            entry = builder.prepare("accton-fixes", "3", "broadcom", FORK, OVERRIDES)["include"][0]
        capture.assert_called_once_with("git", "ls-remote", "--exit-code", "--heads", URL,
                                        "refs/heads/accton-fixes")
        self.assertEqual(entry["upstream"], URL)
        self.assertEqual(entry["submodule_overrides"], OVERRIDES)
        with patch.object(releases, "release", return_value=None), patch.object(releases, "api"), \
                patch.object(releases, "draft_release") as draft:
            releases.reserve_build("ITJamie/sonic-vs-builder", entry, "b" * 40,
                                   "123", "1", "2026-11-15T15:00:00Z", True)
        metadata = draft.call_args.args[3]
        self.assertEqual(metadata["upstream"], URL)
        self.assertEqual(metadata["submodule_overrides"], OVERRIDES)

    def test_override_uses_declared_submodule_name_and_preserves_gitmodules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            modules = root / ".gitmodules"
            original = ('[submodule "platform-common"]\n'
                        '\tpath = src/sonic-platform-common\n'
                        '\turl = https://github.com/sonic-net/sonic-platform-common\n')
            modules.write_text(original)
            builder.configure_submodule_overrides(root, OVERRIDES)
            url = builder.capture("git", "-C", str(root), "config", "--get", "submodule.platform-common.url")
            self.assertEqual(url, "https://github.com/ITJamie/fork-sonic-platform-common.git")
            self.assertEqual(modules.read_text(), original)
            with self.assertRaisesRegex(ValueError, "not a declared submodule"):
                builder.configure_submodule_overrides(root, {"src/missing": "a/b"})

    def test_build_uses_fork_override_and_isolated_cache_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            (work / builder.MARKER).touch()

            def command(*args, **kwargs):
                if "target/sonic-broadcom.bin" in args:
                    output = work / "source/target/sonic-broadcom.bin"
                    output.parent.mkdir()
                    output.write_bytes(b"#!/bin/sh\ninstaller")

            with patch.object(builder, "preflight"), patch.object(builder, "capture", return_value=COMMIT), \
                    patch.object(builder, "configure_submodule_overrides") as configure, \
                    patch.object(builder, "run", side_effect=command) as run, \
                    patch.object(builder, "cache_settings", return_value=[]) as cache:
                builder.build(work, "accton-fixes", COMMIT, "3", "broadcom", enable_ztp=True,
                              source_repository=FORK, overrides=OVERRIDES)
            self.assertIn(("git", "-C", str(work / "source"), "remote", "add", "origin", URL),
                          [call.args for call in run.call_args_list])
            self.assertIn(("git", "-C", str(work / "source"), "fetch", "--depth=1", "origin", COMMIT),
                          [call.args for call in run.call_args_list])
            configure.assert_called_once_with(work / "source", OVERRIDES)
            self.assertIn(URL.lower(), cache.call_args.kwargs["source_identity"])
            data = json.loads((work / "validated.json").read_text())
            self.assertEqual(data["upstream"], URL)
            self.assertEqual(data["submodule_overrides"], OVERRIDES)
            self.assertEqual(data["patches"], [])
            self.assertTrue(data["enable_ztp"])

    def test_source_caches_are_separate_but_versioned_downloads_are_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, work, cache = root / "source", root / "work", root / "cache"
            (source / "rules").mkdir(parents=True)
            work.mkdir()
            variables = "\n".join(("SONIC_DPKG_CACHE_METHOD", "SONIC_DPKG_CACHE_SOURCE",
                                    "SONIC_VERSION_CACHE_METHOD", "SONIC_VERSION_CACHE_SOURCE",
                                    "DOCKER_BUILDER_USER_MOUNT"))
            (source / "rules/config").write_text(variables)
            (source / "Makefile.work").write_text(variables)
            paths = []
            with patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)), \
                    patch.object(builder, "prepare_cache_permissions"):
                for identity in (None, URL, URL + json.dumps(OVERRIDES)):
                    builder.cache_settings(source, work, "202605", directory=str(cache), source_identity=identity)
                    paths.append(json.loads((work / "cache.json").read_text()))
            self.assertEqual(len({p["packages"] for p in paths}), 3)
            self.assertEqual(len({p["local_packages"] for p in paths}), 3)
            self.assertEqual(len({p["downloads"] for p in paths}), 1)
            self.assertEqual(paths[0]["packages"], str(cache / "v1/branches/202605/amd64/packages"))
