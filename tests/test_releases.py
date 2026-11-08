import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import releases
from sonic_vs import MARKER, checksum, write_json

REPO = "ITJamie/sonic-vs-builder"
DRAFT = {"id": 42, "tag_name": "sonic-202605-123-1", "draft": True,
         "html_url": "https://github.com/ITJamie/sonic-vs-builder/releases/tag/sonic-202605-123-1"}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / MARKER).touch()
        self.source = self.work / "source/target/sonic-broadcom.bin"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"#!/bin/sh\ninstaller payload\n")
        write_json(self.work / "validated.json", {
            "platform": "broadcom", "branch": "202605", "commit": "a" * 40,
            "upstream": "https://github.com/sonic-net/sonic-buildimage.git",
            "sha256": checksum(self.source), "size_bytes": self.source.stat().st_size,
        })

    @patch.object(releases, "api", return_value=[DRAFT])
    def test_finds_draft_through_release_listing(self, api):
        self.assertEqual(releases.release(REPO, DRAFT["tag_name"]), DRAFT)
        api.assert_called_once_with(REPO, "/releases?per_page=100&page=1")

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "upload")
    def test_upload_unsplit_output_checksum_and_metadata(self, upload, _release):
        uploaded = {}
        upload.side_effect = lambda repo, rid, path: uploaded.update({path.name: path.read_bytes()})
        releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        self.assertEqual(uploaded["sonic-broadcom.bin"], self.source.read_bytes())
        self.assertIn(checksum(self.source), uploaded["broadcom-SHA256SUMS"].decode())
        self.assertEqual(json.loads(uploaded["broadcom-metadata.json"])["release_assets"], ["sonic-broadcom.bin"])
        self.assertEqual(json.loads((self.work / "release.json").read_text())["tag"], DRAFT["tag_name"])

    @patch.object(releases, "release", return_value=None)
    @patch.object(releases, "api", return_value=DRAFT)
    @patch.object(releases, "upload")
    def test_new_release_is_draft_and_pinned_to_builder_commit(self, _upload, api, _release):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}):
            releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        body = api.call_args.args[3]
        self.assertTrue(body["draft"])
        self.assertEqual(body["target_commitish"], "b" * 40)
        self.assertEqual(body["tag_name"], DRAFT["tag_name"])
        self.assertEqual(body["make_latest"], "false")

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "upload")
    def test_split_output_reassembles_exactly_and_cleans_temporary_parts(self, upload, _release):
        uploaded = {}
        upload.side_effect = lambda repo, rid, path: uploaded.update({path.name: path.read_bytes()})
        with patch.object(releases, "ASSET_LIMIT", 20), patch.object(releases, "PART_SIZE", 8):
            releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        parts = sorted(name for name in uploaded if ".part" in name)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(uploaded[name]) <= 8 for name in parts))
        self.assertEqual(b"".join(uploaded[name] for name in parts), self.source.read_bytes())
        self.assertIn(b"cat sonic-broadcom.bin.part* > sonic-broadcom.bin", uploaded["broadcom-README.txt"])
        self.assertEqual(list(self.work.glob("release-assets-*")), [])

    @patch.object(releases, "release", return_value={**DRAFT, "draft": False})
    @patch.object(releases, "upload")
    def test_never_overwrites_published_release(self, upload, _release):
        with self.assertRaises(ValueError):
            releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        upload.assert_not_called()

    @patch.object(releases, "upload")
    def test_changed_output_cannot_be_published(self, upload):
        self.source.write_bytes(b"corrupted after build")
        with self.assertRaises(ValueError):
            releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        upload.assert_not_called()

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "api", return_value=[])
    def test_incomplete_release_stays_draft(self, api, _release):
        with self.assertRaises(ValueError):
            releases.finish(REPO, DRAFT["tag_name"], "both")
        self.assertTrue(all(call.args[1].endswith("page=1") for call in api.call_args_list))

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "api")
    def test_complete_release_is_published_without_becoming_latest(self, api, _release):
        names = ["vs-metadata.json", "vs-SHA256SUMS", "vs-README.txt", "docker-sonic-vs.gz",
                 "broadcom-metadata.json", "broadcom-SHA256SUMS", "broadcom-README.txt", "sonic-broadcom.bin.part0001"]
        api.side_effect = [[{"name": name} for name in names], {**DRAFT, "draft": False}]
        releases.finish(REPO, DRAFT["tag_name"], "both")
        api.assert_called_with(REPO, "/releases/42", "PATCH", {"draft": False, "make_latest": "false"})

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "api")
    def test_all_requires_kvm_assets_before_publication(self, api, _release):
        names = [f"{platform}-{suffix}" for platform in ("vs", "kvm", "broadcom")
                 for suffix in ("metadata.json", "SHA256SUMS", "README.txt")]
        names.extend(["docker-sonic-vs.gz", "sonic-broadcom.bin"])
        api.return_value = [{"name": name} for name in names]
        with self.assertRaisesRegex(ValueError, "sonic-vs.img.gz"):
            releases.finish(REPO, DRAFT["tag_name"], "all")
        self.assertTrue(all(call.args[1].endswith("page=1") for call in api.call_args_list))
        api.reset_mock()
        api.side_effect = [[{"name": name} for name in [*names, "sonic-vs.img.gz.part0001"]],
                           {**DRAFT, "draft": False}]
        releases.finish(REPO, DRAFT["tag_name"], "all")
        api.assert_called_with(REPO, "/releases/42", "PATCH", {"draft": False, "make_latest": "false"})

    @patch.object(releases, "release", return_value=DRAFT)
    @patch.object(releases, "upload")
    def test_kvm_release_has_disk_instructions_and_metadata(self, upload, _release):
        disk = self.source.parent / "sonic-vs.img.gz"
        disk.write_bytes(b"validated compressed disk")
        metadata = json.loads((self.work / "validated.json").read_text())
        metadata.update(platform="kvm", sha256=checksum(disk))
        write_json(self.work / "validated.json", metadata)
        uploaded = {}
        upload.side_effect = lambda repo, rid, path: uploaded.update({path.name: path.read_bytes()})
        releases.stage(self.work, REPO, DRAFT["tag_name"], "b" * 40)
        self.assertIn(b"gzip -dk sonic-vs.img.gz", uploaded["kvm-README.txt"])
        self.assertIn(b"QCOW2", uploaded["kvm-README.txt"])
        self.assertNotIn(b"ONIE", uploaded["kvm-README.txt"])

    @patch.object(releases.urllib.request, "urlopen")
    def test_asset_upload_streams_file_with_explicit_length(self, urlopen):
        def response(request, **kwargs):
            self.assertTrue(hasattr(request.data, "read"))
            self.assertEqual(request.data.read(), self.source.read_bytes())
            self.assertEqual(request.get_header("Content-length"), str(self.source.stat().st_size))
            self.assertTrue(request.full_url.startswith("https://uploads.github.com/"))
            return io.BytesIO(b'{"id": 123}')

        urlopen.side_effect = response
        with patch.dict(os.environ, {"GH_TOKEN": "test-token"}):
            result = releases.api(REPO, "/releases/42/assets?name=image.bin", "POST", file=self.source)
        self.assertEqual(result, {"id": 123})


if __name__ == "__main__":
    unittest.main()
