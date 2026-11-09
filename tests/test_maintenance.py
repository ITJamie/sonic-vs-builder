from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import maintenance
from sonic_vs import MARKER, maintenance_root, runner_lock

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp()


def image(tag, age, identity=None):
    return {"Id": identity or "id-" + tag, "RepoTags": [tag],
            "Created": datetime.fromtimestamp(NOW - age * 3600, timezone.utc).isoformat()}


class Maintenance(unittest.TestCase):
    def test_retention_is_scoped_and_preserves_used_and_recent_images(self):
        images = [image(f"sonic-slave-bookworm:{n}", n) for n in range(1, 6)]
        images += [image("tmp-sonic-slave-bookworm:old", 4),
                   image("tmp-sonic-slave-trixie:new", .1),
                   image("unrelated:old", 100), image("ghcr.io/itjamie/sonic-vs-builder:202605", 100)]
        result = maintenance.image_candidates(images, {"id-sonic-slave-bookworm:3"}, now=NOW)
        self.assertEqual(set(result), {"sonic-slave-bookworm:4", "sonic-slave-bookworm:5", "tmp-sonic-slave-bookworm:old"})

    def test_unknown_dates_and_other_tags_are_never_deleted(self):
        unknown = image("sonic-slave-trixie:unknown", 10)
        unknown["Created"] = "not a timestamp"
        multi = image("sonic-slave-trixie:old", 10)
        multi["RepoTags"].append("other-project:important")
        result = maintenance.image_candidates([unknown, multi], set(), keep=1, now=NOW)
        self.assertEqual(result, ["sonic-slave-trixie:old"])

    def test_stale_workspace_needs_marker_and_cannot_be_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "sonic-vs-old"
            recent = root / "sonic-vs-recent"
            unmarked = root / "sonic-vs-unmarked"
            for path in (old, recent, unmarked):
                path.mkdir()
                if path != unmarked:
                    (path / MARKER).touch()
            os.utime(old, (NOW - 4 * 86400,) * 2)
            os.utime(unmarked, (NOW - 4 * 86400,) * 2)
            os.utime(recent, (NOW,) * 2)
            (root / "sonic-vs-link").symlink_to(old, target_is_directory=True)
            self.assertEqual(maintenance.workspaces(root, 2, NOW), [old.resolve()])

    def args(self, apply=False, builder=None):
        return SimpleNamespace(apply=apply, builder=builder, sonic_images=True, keep_images=1,
                               image_min_hours=1, work_root=Path("/unused"), work_min_days=2,
                               max_cache_gib=80, reserve_cache_gib=20, min_free_gib=350)

    @patch.object(maintenance, "runner_active", return_value=True)
    @patch.object(maintenance, "inventory")
    @patch.object(maintenance, "run")
    def test_active_job_skips_every_mutation(self, run, inventory, _active):
        maintenance.maintain(self.args(apply=True, builder="default"))
        inventory.assert_not_called()
        run.assert_not_called()

    @patch.object(maintenance, "runner_active", return_value=False)
    @patch.object(maintenance, "workspaces", return_value=[])
    @patch.object(maintenance, "inventory")
    @patch.object(maintenance, "capture", return_value="--max-used-space --reserved-space --min-free-space")
    @patch.object(maintenance, "run")
    def test_dry_run_never_deletes_and_builder_is_explicit(self, run, _capture, inventory, _work, _active):
        inventory.return_value = ([image("sonic-slave-bookworm:new", 1), image("sonic-slave-bookworm:old", 5)], set())
        with patch.object(maintenance.time, "time", return_value=NOW):
            maintenance.maintain(self.args(builder="sonic-project"))
        run.assert_not_called()

    @patch.object(maintenance, "runner_active", return_value=False)
    @patch.object(maintenance, "workspaces", return_value=[])
    @patch.object(maintenance, "inventory")
    @patch.object(maintenance, "run")
    def test_apply_removes_only_selected_tags_without_force_or_implicit_prune(self, run, inventory, _work, _active):
        inventory.return_value = ([image("sonic-slave-bookworm:new", 1), image("sonic-slave-bookworm:old", 5)], set())
        with patch.object(maintenance.time, "time", return_value=NOW):
            maintenance.maintain(self.args(apply=True))
        run.assert_called_once_with("docker", "image", "rm", "sonic-slave-bookworm:old")

    def test_builder_command_is_bounded_and_never_system_prune(self):
        command = maintenance.prune_command(self.args(builder="sonic-project"))
        self.assertEqual(command[:5], ["docker", "buildx", "prune", "--builder", "sonic-project"])
        self.assertIn(str(80 * 1024**3), command)
        self.assertNotIn("system", command)

    @patch.object(maintenance, "runner_active", return_value=False)
    @patch.object(maintenance, "workspaces", return_value=[])
    @patch.object(maintenance, "inventory")
    @patch.object(maintenance, "capture", return_value="--max-used-space --reserved-space --min-free-space")
    @patch.object(maintenance, "run")
    def test_one_image_conflict_does_not_prevent_builder_cleanup(self, run, _capture, inventory, _work, _active):
        inventory.return_value = ([image("sonic-slave-bookworm:new", 1), image("sonic-slave-bookworm:old", 5)], set())
        run.side_effect = [subprocess.CalledProcessError(1, "docker image rm"), None]
        with patch.object(maintenance.time, "time", return_value=NOW):
            with self.assertRaisesRegex(ValueError, "1 cleanup action"):
                maintenance.maintain(self.args(apply=True, builder="default"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[:3], ("docker", "buildx", "prune"))

    def test_maintenance_cannot_enter_while_build_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with runner_lock(root):
                with self.assertRaises(BlockingIOError):
                    with runner_lock(root, blocking=False):
                        self.fail("A maintenance process entered an active build lock")
            with runner_lock(root, blocking=False):
                pass

    @patch("sonic_vs.os.geteuid", return_value=0)
    @patch("sonic_vs.os.fchown")
    def test_root_maintenance_keeps_lock_owned_by_cache_account(self, chown, _root):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = root.stat()
            with runner_lock(root):
                self.assertEqual(chown.call_args.args[1:], (owner.st_uid, owner.st_gid))

    def test_local_custom_cache_root_is_reused_for_publish_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            custom = work / "custom-cache"
            (work / "runner-lock.json").write_text(json.dumps({"root": str(custom)}))
            self.assertEqual(maintenance_root(work=work), custom.resolve())

    def test_cli_workspace_lifecycle_uses_lock_without_polluting_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = dict(os.environ, SONIC_CACHE_DIR=str(root / "cache"), RUNNER_TEMP=str(root))
            script = str(Path(__file__).parents[1] / "scripts/sonic_vs.py")
            work = Path(subprocess.check_output([sys.executable, script, "create-work"],
                                                env=environment, text=True).strip())
            self.assertTrue((work / MARKER).is_file())
            self.assertTrue((root / "cache/maintenance.lock").is_file())
            subprocess.run([sys.executable, script, "cleanup", "--work", str(work)], env=environment, check=True)
            self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
