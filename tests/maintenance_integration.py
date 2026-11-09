"""Exercise dry-run, retention and container protection with isolated Docker tags."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import maintenance
from sonic_vs import capture, permissions_image, runner_lock

base = permissions_image()
identity = capture("docker", "image", "inspect", "--format", "{{.Id}}", base)
repo = "sonic-slave-bookworm-maintenance-" + uuid.uuid4().hex[:12]
tags = [repo + f":{n:02}" for n in range(3)]
container = "sonic-maintenance-" + uuid.uuid4().hex[:12]
try:
    for tag in tags:
        subprocess.run(["docker", "tag", base, tag], check=True)
    item = json.loads(capture("docker", "image", "inspect", tags[0]))[0]
    item["RepoTags"] = tags
    subprocess.run(["docker", "create", "--name", container, "--entrypoint", "/bin/sh", tags[0], "-c", "true"],
                   check=True, stdout=subprocess.DEVNULL)
    used = json.loads(capture("docker", "container", "inspect", container))[0]["Image"]
    assert maintenance.image_candidates([item], {used}, keep=1, min_hours=0) == []
    subprocess.run(["docker", "container", "rm", container], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        native = root / "v1/branches/202605/amd64/local-packages/preserved.tgz"
        native.parent.mkdir(parents=True)
        native.write_text("native package cache")
        args = SimpleNamespace(apply=False, builder=None, sonic_images=True, keep_images=1,
                               image_min_hours=0, work_root=root / "work", work_min_days=2)
        with runner_lock(root), patch.object(maintenance, "runner_active", return_value=False), \
                patch.object(maintenance, "inventory", return_value=([item], set())):
            maintenance.maintain(args)
            for tag in tags:
                assert capture("docker", "image", "inspect", "--format", "{{.Id}}", tag) == identity
            args.apply = True
            maintenance.maintain(args)
        assert capture("docker", "image", "inspect", "--format", "{{.Id}}", tags[-1]) == identity
        for tag in tags[:-1]:
            assert subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode != 0
        assert native.read_text() == "native package cache"
    assert capture("docker", "image", "inspect", "--format", "{{.Id}}", base) == identity
finally:
    subprocess.run(["docker", "container", "rm", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for tag in tags:
        subprocess.run(["docker", "image", "rm", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("Maintenance dry-run, container protection, newest-tag retention and native-cache preservation passed")
