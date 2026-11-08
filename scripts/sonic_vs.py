#!/usr/bin/env python3
"""Build upstream SONiC VS archives and publish validated images (stdlib only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

UPSTREAM = "https://github.com/sonic-net/sonic-buildimage.git"
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
SHA = re.compile(r"[0-9a-f]{40}\Z")
MARKER = ".sonic-vs-builder"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(*args, **kwargs):
    return run(*args, stdout=subprocess.PIPE, **kwargs).stdout.strip()


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("build_jobs must be a positive integer")
    return int(value)


def branches(value):
    result = []
    for entry in value.split(","):
        branch = entry.strip()
        if not TAG.fullmatch(branch):
            raise ValueError(f"Branch must also be a valid Docker tag: {branch!r}")
        # A Docker tag can contain sequences that Git branch names cannot.
        run("git", "check-ref-format", f"refs/heads/{branch}", stdout=subprocess.DEVNULL)
        if branch not in result:
            result.append(branch)
    return result


def prepare(value, jobs):
    positive(jobs)
    entries = []
    for branch in branches(value):
        ref = f"refs/heads/{branch}"
        output = capture("git", "ls-remote", "--exit-code", "--heads", UPSTREAM, ref)
        matches = [line.split() for line in output.splitlines() if line.split()[-1] == ref]
        if len(matches) != 1 or not SHA.fullmatch(matches[0][0]):
            raise ValueError(f"Cannot resolve upstream branch: {branch}")
        entries.append({"branch": branch, "commit": matches[0][0]})
    return {"include": entries}


def owned_work(path):
    if Path(path).is_symlink():
        raise ValueError("Work directory cannot be a symlink")
    work = Path(path).resolve()
    if not (work / MARKER).is_file():
        raise ValueError(f"Not a project-owned work directory: {work}")
    return work


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def preflight(work):
    if sys.platform != "linux" or os.uname().machine != "x86_64" or os.geteuid() == 0:
        raise ValueError("Builds require a non-root Linux x86_64 user")
    for command in ("git", "make", "docker", "j2"):
        if not shutil.which(command):
            raise ValueError(f"Missing prerequisite: {command}")
    info = json.loads(capture("docker", "info", "--format", "{{json .}}"))
    if info["OSType"] != "linux" or info["Architecture"] not in ("x86_64", "amd64"):
        raise ValueError("Docker must use a local Linux amd64 daemon")
    # Checking the host filesystem is meaningful only for a local Docker daemon.
    endpoint = capture("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if not os.environ.get("DOCKER_HOST", endpoint).startswith("unix://"):
        raise ValueError("Remote Docker daemons are unsupported")
    for location in (work, Path(info["DockerRootDir"])):
        if shutil.disk_usage(location).free < 300 * 1024**3:
            raise ValueError(f"Need at least 300 GiB free on the filesystem containing {location}")
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:"))) * 1024
    if memory < 15 * 1024**3:
        raise ValueError("Use a host with at least 16 GB RAM")
    run("sudo", "-n", "modprobe", "overlay")


def archive_image(archive):
    with tarfile.open(archive, "r:gz") as source:
        manifest = json.load(source.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("Expected exactly one image in the Docker archive")
        config = source.extractfile(manifest[0]["Config"]).read()
        digest = hashlib.sha256(config).hexdigest()
        image = json.loads(config)
        if image.get("os") != "linux" or image.get("architecture") != "amd64":
            raise ValueError("Expected a Linux amd64 image")
    return "sha256:" + digest


def load_untagged(archive):
    """Strip upstream tags while streaming, so existing host tags cannot be replaced."""
    import io

    process = subprocess.Popen(["docker", "load"], stdin=subprocess.PIPE)
    try:
        with tarfile.open(archive, "r|gz") as source, tarfile.open(fileobj=process.stdin, mode="w|") as target:
            for member in source:
                data = source.extractfile(member) if member.isfile() else None
                if member.name == "manifest.json":
                    manifest = json.load(data)
                    for entry in manifest:
                        entry["RepoTags"] = []
                    content = json.dumps(manifest).encode()
                    member.size = len(content)
                    data = io.BytesIO(content)
                # Legacy Docker tag mapping is unnecessary and could overwrite host tags.
                if member.name != "repositories":
                    target.addfile(member, data)
        process.stdin.close()
        if process.wait() != 0:
            raise ValueError("docker load failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def smoke(image):
    actual = capture("docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image)
    if actual != "linux/amd64":
        raise ValueError(f"Unexpected image platform: {actual}")
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
        "-ec", "test -s /etc/sonic/sonic_version.yml; cat /etc/sonic/sonic_version.yml; sonic-cfggen --help")


def build(work, branch, commit, jobs):
    work = owned_work(work)
    if branches(branch) != [branch] or not SHA.fullmatch(commit):
        raise ValueError("Expected one validated branch and a full commit SHA")
    positive(jobs)
    preflight(work)
    source = work / "source"
    source.mkdir()
    run("git", "init", str(source))
    run("git", "-C", str(source), "remote", "add", "origin", UPSTREAM)
    run("git", "-C", str(source), "fetch", "--depth=1", "origin", commit)
    run("git", "-C", str(source), "checkout", "--detach", commit)
    if capture("git", "-C", str(source), "rev-parse", "HEAD") != commit:
        raise ValueError("Checkout differs from resolved upstream commit")
    for args in (("init",), ("configure", "PLATFORM=vs", "PLATFORM_ARCH=amd64"),
                 (f"SONIC_BUILD_JOBS={jobs}", "target/docker-sonic-vs.gz")):
        run("make", "UNATTENDED=y", *args, cwd=source)
    archive = source / "target/docker-sonic-vs.gz"
    image = archive_image(archive)
    existed = subprocess.run(["docker", "image", "inspect", image],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    # Persist cleanup ownership before loading or smoke testing can fail.
    write_json(work / "image-state.json", {"image_id": image, "preexisting": existed})
    load_untagged(archive)
    smoke(image)
    write_json(work / "validated.json", {"upstream": UPSTREAM, "branch": branch,
                                        "commit": commit, "image_id": image})


def publish(work, repository):
    work = owned_work(work)
    repository = repository.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_.-]*", repository):
        raise ValueError("Expected GitHub owner/repository")
    metadata = json.loads((work / "validated.json").read_text())
    branch = metadata["branch"]
    if branches(branch) != [branch]:
        raise ValueError("Invalid branch tag")
    image = metadata["image_id"]
    reference = f"ghcr.io/{repository}:{branch}"
    # Workflow GITHUB_TOKEN publication links the package to its repository.
    with tempfile.TemporaryDirectory(prefix="sonic-vs-auth-") as config:
        env = dict(os.environ, DOCKER_CONFIG=config)
        run("docker", "login", "ghcr.io", "--username", os.environ["GITHUB_ACTOR"],
            "--password-stdin", input=os.environ["GHCR_TOKEN"] + "\n", env=env)
        previous = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Record the previous local tag before changing it, including when push fails.
        write_json(work / "tag-state.json", {"reference": reference,
                                            "previous": previous.stdout.strip() if previous.returncode == 0 else None})
        run("docker", "tag", image, reference, env=env)
        run("docker", "push", reference, env=env)
        digests = json.loads(capture("docker", "image", "inspect", "--format", "{{json .RepoDigests}}",
                                     reference, env=env))
        digest = next(item.split("@", 1)[1] for item in digests if item.startswith(f"ghcr.io/{repository}@"))
        metadata.update({"image": reference, "digest": digest, "pull_command": f"docker pull {reference}",
                         "repository": f"https://github.com/{repository}"})
        write_json(work / "metadata.json", metadata)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write(f"### {branch}\n\n```json\n{json.dumps(metadata, indent=2)}\n```\n")


def cleanup(work):
    work = owned_work(work)
    tag_file = work / "tag-state.json"
    if tag_file.exists():
        tag = json.loads(tag_file.read_text())
        if tag["previous"]:
            run("docker", "tag", tag["previous"], tag["reference"])
        else:
            subprocess.run(["docker", "image", "rm", tag["reference"]], check=False)
    state_file = work / "image-state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        if not state["preexisting"]:
            # No --force: never remove another consumer's tags or containers.
            subprocess.run(["docker", "image", "rm", state["image_id"]], check=False)
    shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--branches", required=True)
    prep.add_argument("--build-jobs", default="1")
    sub.add_parser("create-work")
    for name in ("build", "publish", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("--work", required=True)
        if name == "build":
            command.add_argument("--branch", required=True)
            command.add_argument("--commit", required=True)
            command.add_argument("--build-jobs", default="1")
        if name == "publish":
            command.add_argument("--repository", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args.branches, args.build_jobs), separators=(",", ":")))
    elif args.command == "create-work":
        work = Path(tempfile.mkdtemp(prefix="sonic-vs-", dir=os.environ.get("RUNNER_TEMP")))
        (work / MARKER).touch()
        print(work)
    elif args.command == "build":
        build(args.work, args.branch, args.commit, args.build_jobs)
    elif args.command == "publish":
        publish(args.work, args.repository)
    else:
        cleanup(args.work)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"ERROR: {error}")
