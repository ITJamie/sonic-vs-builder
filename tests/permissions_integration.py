"""Exercise root/non-root bind mounts and inherited ACLs using real Docker."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import sonic_vs as builder


def container(image, path, user, command, environment=None):
    args = ["docker", "run", "--rm", "--network", "none", "--user", user,
            "--mount", f"type=bind,source={path},target=/cache", "--workdir", "/cache",
            "--entrypoint", "/bin/sh"]
    for name, value in (environment or {}).items():
        args.extend(["--env", f"{name}={value}"])
    subprocess.run([*args, image, "-ec", command], check=True)


with tempfile.TemporaryDirectory() as directory:
    base = Path(directory).resolve()
    cache = base / "persistent-cache"
    cache.mkdir()
    image = builder.permissions_image()
    user = f"{os.getuid()}:{os.getgid()}"
    # Reproduce a cache created by a root hook before the permissions fix.
    container(image, cache, "0:0", "umask 022; mkdir existing; touch existing/web.flock")
    builder.prepare_cache_permissions((cache,))
    container(image, cache, user, "echo runner >> existing/web.flock")
    # New root-created files must also be writable without rerunning the fix.
    container(image, cache, "0:0", "umask 022; mkdir future; touch future/web.flock")
    container(image, cache, user, "echo runner >> future/web.flock; mkdir future/user-created")
    container(image, cache, "0:0", "echo root >> future/web.flock")
    # Root-owned private Docker data must not block workspace cleanup.
    work = base / "work"
    work.mkdir()
    (work / builder.MARKER).touch()
    source = work / "source"
    source.mkdir()
    container(image, source, "0:0", "mkdir fsroot.docker.bookworm; chmod 700 fsroot.docker.bookworm; "
              "echo data > fsroot.docker.bookworm/private")
    builder.cleanup(work)
    assert not work.exists(), "Root-owned source was not cleaned"
    assert (cache / "future/web.flock").exists(), "Cleanup removed persistent cache"
print("Root/non-root cache sharing, inherited permissions and workspace cleanup passed")
