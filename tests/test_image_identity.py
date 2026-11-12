import os
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import releases
import sonic_vs as builder

REPO = "ITJamie/sonic-vs-builder"
ENTRY = {"branch": "202605", "commit": "a" * 40, "images": ["broadcom"]}
START = "2026-11-12T22:18:17Z"
BASE = "sonic-202605-20261112-22"


class ImageIdentity(unittest.TestCase):
    def test_start_time_is_converted_to_utc_and_versions_are_safe(self):
        self.assertEqual(builder.date_label(START), "20261112-22")
        self.assertEqual(builder.date_label("2026-07-01T00:30:00+01:00"), "20260630-23")
        for branch in ("202111", "202211", "202311", "202405", "202505", "202511", "202605"):
            self.assertEqual(builder.version_name(branch, "20261112-22"), f"{branch}-itjamie.20261112-22")
            self.assertEqual(builder.version_name(branch, "20261112-22-2", True),
                             f"{branch}-itjamie.20261112-22-2-ztp")
        for value in ("2026-11-12T22:18:17", "not a date"):
            with self.assertRaises(ValueError):
                builder.date_label(value)
        for value in ("20260230-22", "20261112-24", "20261112-22-1", "20261112-22;touch /tmp/x"):
            with self.assertRaises(ValueError):
                builder.version_name("202605", value)
        for value in ("", "../x", "x y", "x'$(id)", "a" * 129):
            with self.assertRaises(ValueError):
                builder.validate_version(value)

    @patch.object(releases, "draft_release")
    @patch.object(releases, "release", return_value=None)
    @patch.object(releases, "api", return_value={})
    def test_date_label_is_reserved_before_build_without_public_id(self, api, _release, draft):
        identity = releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, True)
        self.assertEqual(identity["release_tag"], BASE)
        self.assertEqual(identity["build_label"], "20261112-22")
        self.assertEqual(identity["image_version"], "202605-itjamie.20261112-22-ztp")
        self.assertNotIn("12345", identity["release_tag"])
        api.assert_called_once_with(REPO, "/git/refs", "POST", {"ref": "refs/tags/" + BASE, "sha": "b" * 40})
        metadata = draft.call_args.args[3]
        self.assertEqual(metadata["workflow_run_id"], "12345")
        self.assertEqual(metadata["workflow_run_attempt"], "1")
        self.assertEqual(metadata["build_started_at"], START)

    @patch.object(releases, "draft_release")
    @patch.object(releases, "api", return_value={})
    def test_same_hour_and_same_builder_do_not_reuse_another_runs_assets(self, api, draft):
        previous = {"draft": False, "target_commitish": "b" * 40, "body": "another run"}
        with patch.object(releases, "release", side_effect=[previous, None]):
            identity = releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, False)
        self.assertEqual(identity["release_tag"], BASE + "-2")
        self.assertEqual(identity["image_version"], "202605-itjamie.20261112-22-2")
        self.assertEqual(api.call_args.args[3]["ref"], "refs/tags/" + BASE + "-2")

    @patch.object(releases, "draft_release")
    @patch.object(releases, "release", return_value=None)
    def test_orphaned_or_racing_git_tag_consumes_its_label(self, _release, draft):
        with patch.object(releases, "api", side_effect=[
                releases.GitHubAPIError("POST", "/git/refs", 422, "Reference exists"),
                {"ref": "refs/tags/" + BASE}, {}]) as api:
            identity = releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, False)
        self.assertEqual(identity["release_tag"], BASE + "-2")
        self.assertEqual(api.call_count, 3)

    @patch.object(releases, "draft_release")
    @patch.object(releases, "release", return_value=None)
    def test_other_422_errors_do_not_loop_over_date_suffixes(self, _release, draft):
        invalid = releases.GitHubAPIError("POST", "/git/refs", 422, "Invalid target")
        with patch.object(releases, "api", side_effect=[invalid,
                releases.GitHubAPIError("GET", "/git/ref", 404, "Not Found")]) as api:
            with self.assertRaisesRegex(releases.GitHubAPIError, "Invalid target"):
                releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, False)
        self.assertEqual(api.call_count, 2)
        draft.assert_not_called()

    @patch.object(releases, "draft_release")
    @patch.object(releases, "api")
    def test_only_same_run_attempt_can_reuse_its_own_draft(self, api, draft):
        previous = {"draft": True, "target_commitish": "b" * 40,
                    "body": "<!-- sonic-vs-builder-run: 12345/1 -->"}
        with patch.object(releases, "release", return_value=previous):
            identity = releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, False)
        self.assertEqual(identity["release_tag"], BASE)
        api.assert_not_called()
        with patch.object(releases, "release", side_effect=[previous, None]):
            identity = releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "2", START, False)
        self.assertEqual(identity["release_tag"], BASE + "-2")

    @patch.object(releases, "draft_release")
    @patch.object(releases, "release", return_value=None)
    def test_permissions_failure_never_looks_like_a_tag_collision(self, _release, draft):
        with patch.object(releases, "api", side_effect=releases.GitHubAPIError(
                "POST", "/git/refs", 403, "Forbidden")) as api:
            with self.assertRaises(releases.GitHubAPIError):
                releases.reserve_build(REPO, ENTRY, "b" * 40, "12345", "1", START, False)
        self.assertEqual(api.call_count, 1)
        draft.assert_not_called()

    def test_readable_placeholder_draft_uses_exact_tag_marker(self):
        draft = {"draft": True, "tag_name": "untagged-placeholder", "name": "SONiC 202605 — 20261112-22",
                 "body": f"<!-- sonic-vs-builder-release: {BASE} -->"}
        with patch.object(releases, "api", return_value=[draft]):
            self.assertEqual(releases.release(REPO, BASE), draft)
            self.assertIsNone(releases.release(REPO, BASE + "-2"))

    def test_release_title_and_notes_use_date_but_keep_provenance(self):
        metadata = {**ENTRY, "upstream": builder.UPSTREAM, "build_label": "20261112-22",
                    "image_version": "202605-itjamie.20261112-22", "enable_ztp": False,
                    "build_started_at": START, "workflow_run_id": "12345", "workflow_run_attempt": "1"}
        with patch.object(releases, "release", return_value=None), patch.object(releases, "api") as api, \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "12345"}):
            api.return_value = {"draft": True}
            releases.draft_release(REPO, BASE, "b" * 40, metadata)
        body = api.call_args.args[3]
        self.assertEqual(body["name"], "SONiC 202605 — 20261112-22")
        for text in (metadata["image_version"], START, "b" * 40, "a" * 40, "actions/runs/12345",
                     "<!-- sonic-vs-builder-run: 12345/1 -->", f"<!-- sonic-vs-builder-release: {BASE} -->"):
            self.assertIn(text, body["body"])

    def test_workflow_shares_allocated_identity_across_every_platform(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/build.yml").read_text()
        for text in ("matrix: ${{ steps.identity.outputs.matrix }}", "RELEASE_TAG: ${{ matrix.release_tag }}",
                     'output.write(f"SONIC_IMAGE_VERSION={version}\\n")', "BUILD_LABEL: ${{ matrix.build_label }}",
                     '["run_started_at"]', "actions: read"):
            self.assertIn(text, workflow)
        self.assertNotIn("RELEASE_TAG: sonic-${{ matrix.branch }}-${{ github.run_id }}", workflow)

    def test_actual_workflow_output_avoids_masked_username_and_rebuilds_exact_version(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/build.yml").read_text()
        scripts = [textwrap.dedent(block.split("          PY", 1)[0])
                   for block in workflow.split("python3 - <<'PY'\n")[1:]]
        self.assertEqual(len(scripts), 2)
        # Execute the actual Actions Python blocks, including the job-output
        # boundary where the runner suppresses values containing a secret.
        for ztp in (False, True):
            identity = {**ENTRY, "build_label": "20261112-22-2", "release_tag": BASE + "-2",
                        "image_version": builder.version_name("202605", "20261112-22-2", ztp),
                        "build_started_at": START}
            with self.subTest(ztp=ztp), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                environment = Path(directory) / "env"
                values = {"MATRIX": json.dumps({"include": [ENTRY]}), "REPOSITORY": REPO,
                          "BUILDER_COMMIT": "b" * 40, "GITHUB_RUN_ID": "12345",
                          "GITHUB_RUN_ATTEMPT": "1", "ENABLE_ZTP": str(ztp).lower(),
                          "GITHUB_OUTPUT": str(output), "GITHUB_ENV": str(environment)}
                with patch.dict(os.environ, values), \
                        patch.object(releases, "api", return_value={"run_started_at": START}), \
                        patch.object(releases, "reserve_build", return_value=dict(identity)):
                    exec(compile(scripts[0], "prepare-workflow", "exec"), {})
                    exported = output.read_text().removeprefix("matrix=").strip()
                    self.assertNotIn("itjamie", exported.lower())
                    entry = json.loads(exported)["include"][0]
                    self.assertEqual(entry["release_tag"], identity["release_tag"])
                    with patch.dict(os.environ, {"BRANCH": entry["branch"],
                                                 "BUILD_LABEL": entry["build_label"]}):
                        exec(compile(scripts[1], "build-workflow", "exec"), {})
                    self.assertEqual(environment.read_text(),
                                     f"SONIC_IMAGE_VERSION={identity['image_version']}\n")


if __name__ == "__main__":
    unittest.main()
