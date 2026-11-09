#!/usr/bin/env python3
"""Dry-run-first maintenance for a dedicated SONiC runner; never system-prune."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from sonic_vs import MARKER, capture, maintenance_root, permissions_image, run, runner_lock

BUILD_REPO = re.compile(r"(?:tmp-)?sonic-slave-(?:bookworm|trixie|bullseye|buster|stretch|jessie)(?:-[A-Za-z0-9_.-]+)?\Z")


def runner_active():
    # Unlike an idle Runner.Listener, Runner.Worker exists for the entire job,
    # including gaps between locked build/publish/cleanup commands.
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes().split(b"\0", 1)[0]
        except FileNotFoundError:
            continue
        except PermissionError:
            raise ValueError("Cannot verify runner inactivity; run maintenance as the runner account or root")
        if command.rsplit(b"/", 1)[-1] == b"Runner.Worker":
            return True
    return False


def docker_objects(kind, identities):
    result = []
    for index in range(0, len(identities), 100):
        result.extend(json.loads(capture("docker", kind, "inspect", *identities[index:index + 100])))
    return result


def inventory():
    images = docker_objects("image", list(dict.fromkeys(capture(
        "docker", "image", "ls", "--all", "--quiet", "--no-trunc").splitlines())))
    containers = docker_objects("container", capture("docker", "container", "ls", "--all", "--quiet").splitlines())
    return images, {item["Image"] for item in containers}


def created(image):
    # Missing/unparseable timestamps are retained, never treated as ancient.
    try:
        return datetime.fromisoformat(image["Created"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return float("inf")


def image_candidates(images, used, keep=2, min_hours=1, now=None):
    groups = {}
    now = time.time() if now is None else now
    for image in images:
        for reference in image.get("RepoTags") or []:
            repository, separator, tag = reference.rpartition(":")
            if not separator or not tag or not BUILD_REPO.fullmatch(repository):
                continue
            groups.setdefault(repository, []).append((created(image), reference, image["Id"]))
    candidates = []
    for repository, entries in sorted(groups.items()):
        entries.sort(reverse=True)
        retained = 0 if repository.startswith("tmp-") else keep
        for timestamp, reference, identity in entries[retained:]:
            if identity not in used and now - timestamp >= min_hours * 3600:
                candidates.append(reference)
    return candidates


def workspaces(root, min_days, now=None):
    now = time.time() if now is None else now
    if root.is_symlink():
        raise ValueError("Work root cannot be a symlink")
    root = root.resolve()
    return sorted(path for path in root.glob("sonic-vs-*")
                  if not path.is_symlink() and path.is_dir() and (path / MARKER).is_file()
                  and now - path.stat().st_mtime >= min_days * 86400)


def remove_workspace(work):
    if work.is_symlink() or not (work / MARKER).is_file():
        raise ValueError("Refusing unmarked or symlinked workspace")
    source = work / "source"
    if source.is_symlink():
        source.unlink()
    elif source.exists():
        try:
            shutil.rmtree(source)
        except PermissionError:
            # Keep the ownership marker intact until root-owned source is gone.
            run("docker", "run", "--rm", "--network", "none", "--user", "0:0",
                "--mount", f"type=bind,source={work},target=/workspace",
                "--entrypoint", "/bin/sh", permissions_image(), "-ec",
                "test -f /workspace/.sonic-vs-builder && rm -rf -- /workspace/source")
    shutil.rmtree(work)


def prune_command(args):
    return ["docker", "buildx", "prune", "--builder", args.builder, "--force",
            "--max-used-space", str(args.max_cache_gib * 1024**3),
            "--reserved-space", str(args.reserve_cache_gib * 1024**3),
            "--min-free-space", str(args.min_free_gib * 1024**3)]


def maintain(args):
    if runner_active():
        print("SKIP: GitHub Runner.Worker is active; no resources changed")
        return
    images, used = inventory()
    selected = image_candidates(images, used, args.keep_images, args.image_min_hours) if args.sonic_images else []
    stale = workspaces(args.work_root, args.work_min_days)
    commands = [["docker", "image", "rm", reference] for reference in selected]
    if args.builder:
        # Only opt-in to an explicitly named, project-exclusive builder. No
        # image/container/volume prune and no implicit default builder pruning.
        help_text = capture("docker", "buildx", "prune", "--help")
        for flag in ("--max-used-space", "--reserved-space", "--min-free-space"):
            if flag not in help_text:
                raise ValueError(f"Installed buildx lacks {flag}; update buildx or omit --builder")
        commands.append(prune_command(args))
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(selected)} old SONiC tags, {len(stale)} stale marked workspaces")
    failures = []
    for path in stale:
        print(f"Remove marked workspace: {path}", flush=True)
        if args.apply:
            try:
                remove_workspace(path)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                failures.append(str(path))
                print(f"Cleanup failed for {path}: {error}", file=sys.stderr)
    for command in commands:
        print(shlex.join(command), flush=True)
        if args.apply:
            try:
                run(*command)
            except subprocess.CalledProcessError as error:
                failures.append(shlex.join(command))
                print(f"Cleanup command failed: {error}", file=sys.stderr)
    if not args.apply:
        print("Nothing removed. Review the plan and repeat with --apply.")
    print("Native package/download caches, registry image tags, volumes and unrelated repositories are retained.")
    if failures:
        raise ValueError(f"{len(failures)} cleanup action(s) failed; inspect the log. Independent actions were still attempted.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Execute the printed cleanup plan")
    parser.add_argument("--cache-dir", help="Same cache root used by the runner; also holds the maintenance lock")
    parser.add_argument("--work-root", type=Path, default=Path(os.environ.get("RUNNER_TEMP", str(Path.home() / "actions-runner/_work/_temp"))))
    parser.add_argument("--sonic-images", action="store_true", help="Opt in to retention for local sonic-slave-* and tmp-sonic-slave-* tags on this dedicated runner")
    parser.add_argument("--keep-images", type=int, default=2, help="Newest tags retained per non-temporary SONiC build repository")
    parser.add_argument("--image-min-hours", type=float, default=1)
    parser.add_argument("--work-min-days", type=float, default=2, help="Must exceed the 24-hour build timeout")
    parser.add_argument("--builder", help="Opt in to pruning this explicitly named PROJECT-EXCLUSIVE buildx builder")
    parser.add_argument("--max-cache-gib", type=int, default=80)
    parser.add_argument("--reserve-cache-gib", type=int, default=20)
    parser.add_argument("--min-free-gib", type=int, default=350)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Runner maintenance requires Linux")
    if args.keep_images < 1 or args.image_min_hours < 0 or args.work_min_days <= 1:
        parser.error("Keep at least one image; image age must be nonnegative and workspace age greater than one day")
    if not 0 < args.reserve_cache_gib <= args.max_cache_gib or args.min_free_gib <= 0:
        parser.error("Cache limits must be positive, with reserve <= maximum")
    if args.builder and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.builder):
        parser.error("Invalid builder name")
    try:
        with runner_lock(maintenance_root(args.cache_dir), blocking=False):
            maintain(args)
    except BlockingIOError:
        print("SKIP: a builder/publisher/cleanup command holds the maintenance lock")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"ERROR: {error}")
