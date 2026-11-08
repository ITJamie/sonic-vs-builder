"""Test native old/current SONiC child-mount forwarding across fresh workspaces."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import sonic_vs as builder


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory).resolve()
    cache = root / "cache"
    image = builder.permissions_image()
    for branch in ("202111", "202605"):
        url = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{branch}/"
        config = urllib.request.urlopen(url + "rules/config", timeout=60).read().decode()
        wrapper = urllib.request.urlopen(url + "Makefile.work", timeout=60).read().decode()
        # Exercise the actual upstream Make expression used to add bind mounts.
        mount = re.search(r"ifneq \(\$\(DOCKER_BUILDER_USER_MOUNT\),\)\n.*?\nendif", wrapper, re.S).group()
        for stage in ("save", "restore"):
            work = root / f"{branch}-{stage}"
            (work / "source/rules").mkdir(parents=True)
            (work / builder.MARKER).touch()
            source = work / "source"
            (source / "rules/config").write_text(config)
            (source / "Makefile.work").write_text(wrapper)
            with patch.object(builder.shutil, "disk_usage", return_value=SimpleNamespace(free=1024**4)):
                arguments = builder.cache_settings(source, work, branch, directory=str(cache))
            local = cache / f"v1/branches/{branch}/amd64/local-packages"
            (source / "target/debs").mkdir(parents=True)
            if stage == "save":
                command = "echo compiled-package > target/debs/payload; tar -czf target/cache/package.tgz -C target/debs payload"
            else:
                command = "tar -xzf target/cache/package.tgz -C target/debs; test `cat target/debs/payload` = compiled-package"
            recipe = ("comma := ,\n"
                      f"DOCKER_RUN = docker run --rm --network none --user {os.getuid()}:{os.getgid()} "
                      f"-v {source}:/sonic -w /sonic\n" + mount + "\n"
                      f"probe:\n\t$(DOCKER_RUN) --entrypoint /bin/sh {image} -ec '{command}'\n")
            (source / "probe.mk").write_text(recipe)
            subprocess.run(["make", "-f", "probe.mk", *arguments, "probe"], cwd=source, check=True)
            assert (local / "package.tgz").is_file(), "Native target/cache writes did not reach persistent storage"
            assert not (source / "target/cache/package.tgz").exists(), "Cache was written into the disposable source"
            builder.cleanup(work)
            assert (local / "package.tgz").is_file(), "Workspace cleanup removed the local package cache"
print("Native 202111/202605 mounts save and restore local package archives across fresh workspaces")
