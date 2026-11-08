"""Verify upstream cache flags and real Docker reuse/invalidation on small images."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import docker_cache_settings, stamp_docker_cache_inputs

COMMIT = "8076a44fa579fde75f73299f03e7f3cac298d9e3"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/Makefile.work"
wrapper = urllib.request.urlopen(URL, timeout=60).read().decode()
switch = re.search(r"ifneq \(\$\(strip \$\(SONIC_CONFIG_USE_DOCKER_CACHE\)\),y\).*?\nendif", wrapper, re.S).group()
tag = "sonic-vs-builder-layer-test:" + uuid.uuid4().hex[:12]
images = set()


def output(*args):
    return subprocess.check_output(args, text=True).strip()


with tempfile.TemporaryDirectory() as directory:
    source = Path(directory).resolve()
    (source / "Makefile.work").write_text(wrapper)
    # Exercise the actual upstream condition rather than reimplementing its flags.
    (source / "probe.mk").write_text(switch + '\nprobe:\n\t@echo "$(DOCKER_NO_CACHE_FLAG)"\n')
    for enabled in (True, False):
        args, settings = docker_cache_settings(source, enabled)
        assert settings["supported"]
        flags = output("make", "-s", "-f", str(source / "probe.mk"), *args, "probe")
        assert flags == ("" if enabled else "--no-cache"), flags
    old_url = "https://raw.githubusercontent.com/sonic-net/sonic-buildimage/202111/Makefile.work"
    (source / "Makefile.work").write_text(urllib.request.urlopen(old_url, timeout=60).read().decode())
    assert docker_cache_settings(source, True)[0] == []
    versions = source / "files/build/versions-public/build/probe/versions-py3"
    versions.parent.mkdir(parents=True)
    versions.write_text("dependency==1\n")
    hook = source / "src/sonic-build-hooks/scripts/probe"
    hook.parent.mkdir(parents=True)
    hook.write_text("hook-v1\n")
    template = source / "sonic-slave-bookworm/Dockerfile.j2"
    template.parent.mkdir()
    template.write_text("FROM debian:bookworm-slim\n"
                        "RUN date +%s%N > /layer-created\n"
                        "COPY files/build/versions-public/build/probe/versions-py3 /installed-constraint\n"
                        "COPY src/sonic-build-hooks/scripts/probe /installed-hook\n")
    patches = []

    def build(label, no_cache=False):
        stamp_docker_cache_inputs(source, patches)
        started = time.monotonic()
        flags = ["--no-cache"] if no_cache else []
        subprocess.run(["docker", "build", "--progress=plain", *flags, "-t", tag,
                        "-f", str(template), str(source)], check=True)
        images.add(output("docker", "image", "inspect", "--format", "{{.Id}}", tag))
        result = output("docker", "run", "--rm", "--network", "none", tag, "sh", "-ec",
                        "cat /layer-created /installed-constraint /installed-hook")
        print(f"{label}: {time.monotonic() - started:.2f}s; {result!r}", flush=True)
        return result

    try:
        first = build("cold")
        assert build("warm") == first, "Identical inputs failed to reuse the installation layer"
        versions.write_text("dependency==2\n")
        changed = build("changed constraint")
        assert changed.splitlines()[0] != first.splitlines()[0], "Constraint change retained an old install layer"
        assert "dependency==2" in changed
        hook.write_text("hook-v2\n")
        hook_changed = build("changed hook")
        assert hook_changed.splitlines()[0] != changed.splitlines()[0], "Hook change retained an old install layer"
        assert "hook-v2" in hook_changed
        patches.append({"path": "202605/fix.patch", "sha256": "a" * 64})
        patch_changed = build("changed patch")
        assert patch_changed.splitlines()[0] != hook_changed.splitlines()[0]
        assert build("disabled", no_cache=True).splitlines()[0] != patch_changed.splitlines()[0]
    finally:
        subprocess.run(["docker", "image", "rm", tag], check=False)
        for image in images:
            subprocess.run(["docker", "image", "rm", image], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("Docker warm reuse, dependency/hook/patch invalidation and disabled-cache rebuild passed")
