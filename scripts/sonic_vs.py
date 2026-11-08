#!/usr/bin/env python3
"""Build upstream SONiC VS/Broadcom images and publish VS to GHCR (stdlib only)."""

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
TARGETS = {"vs": "docker-sonic-vs.gz", "broadcom": "sonic-broadcom.bin"}


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def prepare(value, jobs, images="both"):
    positive(jobs)
    if images not in ("both", *TARGETS):
        raise ValueError("images must be both, vs or broadcom")
    entries = []
    for branch in branches(value):
        ref = f"refs/heads/{branch}"
        output = capture("git", "ls-remote", "--exit-code", "--heads", UPSTREAM, ref)
        matches = [line.split() for line in output.splitlines() if line.split()[-1] == ref]
        if len(matches) != 1 or not SHA.fullmatch(matches[0][0]):
            raise ValueError(f"Cannot resolve upstream branch: {branch}")
        entries.append({"branch": branch, "commit": matches[0][0],
                        "images": list(TARGETS) if images == "both" else [images]})
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


def permissions_image():
    """Small local utility image; avoids granting the runner unrestricted sudo."""
    dockerfile = Path(__file__).with_name("Dockerfile.tools")
    tag = "sonic-vs-builder-tools:" + checksum(dockerfile)[:12]
    present = subprocess.run(["docker", "image", "inspect", tag], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL).returncode == 0
    if not present:
        run("docker", "build", "--tag", tag, "--file", str(dockerfile), str(dockerfile.parent))
    return tag


def prepare_cache_permissions(paths):
    # Upstream build hooks create some directories as root, then access them as
    # the build user. Default ACLs preserve shared access for future children,
    # even when the creating process uses umask 022.
    image = permissions_image()
    for path in paths:
        run("docker", "run", "--rm", "--network", "none", "--user", "0:0",
            "--mount", f"type=bind,source={path},target=/cache",
            "--entrypoint", "/bin/sh", image, "-ec",
            "chmod -R a+rwX /cache; "
            "find /cache -type d -exec setfacl -m d:u::rwx,d:g::rwx,d:o::rwx {} +")


def cache_settings(source, work, branch, mode="read-write", directory=None):
    """Use native dependency/version caches; never share raw target directories."""
    if mode not in ("read-write", "off"):
        raise ValueError("cache_mode must be read-write or off")
    if mode == "off":
        write_json(work / "cache.json", {"mode": "off"})
        print("Persistent cache disabled", flush=True)
        return ["SONIC_DPKG_CACHE_METHOD=none", "SONIC_VERSION_CACHE_METHOD=none"]

    config = (source / "rules/config").read_text()
    makefile = (source / "Makefile.work").read_text()
    for variable in ("SONIC_DPKG_CACHE_METHOD", "SONIC_DPKG_CACHE_SOURCE"):
        if variable not in config or variable not in makefile:
            raise ValueError("This branch lacks native package-cache support; select cache_mode=off")
    configured = directory or os.environ.get("SONIC_CACHE_DIR") or str(Path.home() / ".cache/sonic-vs-builder")
    path = Path(configured).expanduser()
    if not path.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
        raise ValueError("SONIC_CACHE_DIR must be an absolute path using letters, numbers, /, _, . or -")
    root = path.resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)):
        raise ValueError("Resolved cache path contains characters unsupported by SONiC's Makefiles")
    # Cleanup removes the entire work directory. Persistent data must be outside it.
    if root.is_relative_to(work.resolve()):
        raise ValueError("Persistent cache must be outside the temporary work directory")
    packages = root / "v1/branches" / branch / "amd64/packages"
    downloads = root / "v1/downloads/amd64"
    package_source = str(packages)
    # 'cache' is the read/write spelling supported by old and current branches.
    arguments = ["SONIC_DPKG_CACHE_METHOD=cache", f"SONIC_DPKG_CACHE_SOURCE={package_source}"]
    supports_downloads = all(variable in config and variable in makefile for variable in (
        "SONIC_VERSION_CACHE_METHOD", "SONIC_VERSION_CACHE_SOURCE"))
    packages.mkdir(parents=True, exist_ok=True)
    if supports_downloads:
        downloads.mkdir(parents=True, exist_ok=True)
        arguments.extend(["SONIC_VERSION_CACHE_METHOD=cache", f"SONIC_VERSION_CACHE_SOURCE={downloads}"])
    else:
        arguments.append("SONIC_VERSION_CACHE_METHOD=none")
        print("Upstream branch has no native download/version cache; package caching remains enabled", flush=True)
    paths = (packages, downloads) if supports_downloads else (packages,)
    for location in paths:
        if shutil.disk_usage(location).free < 300 * 1024**3:
            raise ValueError(f"Need at least 300 GiB free on the cache filesystem containing {location}")
    prepare_cache_permissions(paths)
    for location in paths:
        # Check actual write access before starting a long source build.
        with tempfile.TemporaryFile(dir=location):
            pass
    details = {"mode": mode, "root": str(root), "packages": package_source,
               "downloads": str(downloads) if supports_downloads else None}
    write_json(work / "cache.json", details)
    print(f"Persistent package cache: {packages}", flush=True)
    if supports_downloads:
        print(f"Persistent download cache: {downloads}", flush=True)
    return arguments


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


def build(work, branch, commit, jobs, platform="vs", cache_mode="read-write", cache_dir=None):
    work = owned_work(work)
    if platform not in TARGETS:
        raise ValueError("Unsupported build platform")
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
    cache_args = cache_settings(source, work, branch, cache_mode, cache_dir)
    filename = TARGETS[platform]
    for args in (("init",), ("configure", f"PLATFORM={platform}", "PLATFORM_ARCH=amd64"),
                 (f"SONIC_BUILD_JOBS={jobs}", f"target/{filename}")):
        run("make", "UNATTENDED=y", *cache_args, *args, cwd=source)
    archive = source / "target" / filename
    metadata = {"upstream": UPSTREAM, "branch": branch, "commit": commit,
                "platform": platform, "filename": filename}
    if platform == "vs":
        image = archive_image(archive)
        existed = subprocess.run(["docker", "image", "inspect", image],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        # Persist cleanup ownership before loading or smoke testing can fail.
        write_json(work / "image-state.json", {"image_id": image, "preexisting": existed})
        load_untagged(archive)
        smoke(image)
        metadata["image_id"] = image
    else:
        with archive.open("rb") as stream:
            if not stream.read(2) == b"#!" or archive.stat().st_size == 0:
                raise ValueError("Broadcom output is not a nonempty ONIE shell installer")
    metadata["sha256"] = checksum(archive)
    metadata["size_bytes"] = archive.stat().st_size
    write_json(work / "validated.json", metadata)


def publish(work, repository, dockerhub=False):
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
    destinations = [("ghcr", "ghcr.io", os.environ.get("GITHUB_ACTOR"),
                     os.environ.get("GHCR_TOKEN"), reference)]
    if dockerhub:
        destinations.append(("dockerhub", "docker.io", os.environ.get("DOCKERHUB_USERNAME"),
                             os.environ.get("DOCKERHUB_TOKEN"), f"docker.io/itjamie/docker-sonic-vs:{branch}"))
    for name, _registry, username, token, _reference in destinations:
        if not username or not token:
            raise ValueError(f"Missing {name} publishing credentials")
    metadata["registries"] = {}
    # Workflow GITHUB_TOKEN publication links the package to its repository.
    with tempfile.TemporaryDirectory(prefix="sonic-vs-auth-") as config:
        env = dict(os.environ, DOCKER_CONFIG=config)
        # Authenticate all selected destinations before changing either registry.
        for _name, registry, username, token, _reference in destinations:
            run("docker", "login", registry, "--username", username,
                "--password-stdin", input=token + "\n", env=env)
        tags = []
        for name, _registry, _username, _token, destination in destinations:
            previous = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", destination],
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            # Track both tags even if a later push fails; restore preexisting tags.
            tags.append({"reference": destination,
                         "previous": previous.stdout.strip() if previous.returncode == 0 else None})
            write_json(work / "tag-state.json", {"tags": tags})
            run("docker", "tag", image, destination, env=env)
            run("docker", "push", destination, env=env)
            digests = json.loads(capture("docker", "image", "inspect", "--format", "{{json .RepoDigests}}",
                                        destination, env=env)) or []
            image_name = destination.rsplit(":", 1)[0]
            # Docker commonly omits docker.io/ in RepoDigests for Hub images.
            normalize = lambda value: value.removeprefix("docker.io/").removeprefix("index.docker.io/")
            digest = next((item.split("@", 1)[1] for item in digests
                           if "@" in item and normalize(item.split("@", 1)[0]) == normalize(image_name)), None)
            if not digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"Cannot determine pushed digest for {destination}")
            details = {"image": destination, "digest": digest, "pull_command": f"docker pull {destination}"}
            metadata["registries"][name] = details
            if name == "ghcr":
                # Preserve existing consumers of the top-level GHCR fields.
                metadata.update(details)
                metadata["repository"] = f"https://github.com/{repository}"
            # Keep successful destination metadata if the subsequent push fails.
            write_json(work / "metadata.json", metadata)
            print(f"Published {destination}@{digest}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write(f"### {branch}\n\n```json\n{json.dumps(metadata, indent=2)}\n```\n")


def cleanup(work):
    work = owned_work(work)
    tag_file = work / "tag-state.json"
    if tag_file.exists():
        state = json.loads(tag_file.read_text())
        # Also support recovering workspaces created by the GHCR-only publisher.
        for tag in state.get("tags", [state]):
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
    source = work / "source"
    if source.exists() and not source.is_symlink():
        try:
            shutil.rmtree(source)
        except PermissionError:
            # Docker-in-Docker leaves root-owned fsroot.docker.* files. The
            # helper sees only this marked workspace, never the persistent cache.
            image = permissions_image()
            run("docker", "run", "--rm", "--network", "none", "--user", "0:0",
                "--mount", f"type=bind,source={work},target=/workspace",
                "--entrypoint", "/bin/sh", image, "-ec",
                "test -f /workspace/.sonic-vs-builder && rm -rf -- /workspace/source")
    shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--branches", required=True)
    prep.add_argument("--build-jobs", default="3")
    prep.add_argument("--images", choices=("both", *TARGETS), default="both")
    sub.add_parser("create-work")
    for name in ("build", "publish", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("--work", required=True)
        if name == "build":
            command.add_argument("--branch", required=True)
            command.add_argument("--commit", required=True)
            command.add_argument("--build-jobs", default="3")
            command.add_argument("--platform", choices=TARGETS, default="vs")
            command.add_argument("--cache-mode", choices=("read-write", "off"), default="read-write")
            command.add_argument("--cache-dir", help="Persistent cache root (or SONIC_CACHE_DIR; defaults to ~/.cache/sonic-vs-builder)")
        if name == "publish":
            command.add_argument("--repository", required=True)
            command.add_argument("--dockerhub", choices=("true", "false"), default="false")
    args = parser.parse_args()
    if args.command == "prepare":
        print(json.dumps(prepare(args.branches, args.build_jobs, args.images), separators=(",", ":")))
    elif args.command == "create-work":
        work = Path(tempfile.mkdtemp(prefix="sonic-vs-", dir=os.environ.get("RUNNER_TEMP")))
        (work / MARKER).touch()
        print(work)
    elif args.command == "build":
        build(args.work, args.branch, args.commit, args.build_jobs, args.platform, args.cache_mode, args.cache_dir)
    elif args.command == "publish":
        publish(args.work, args.repository, args.dockerhub == "true")
    else:
        cleanup(args.work)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"ERROR: {error}")
