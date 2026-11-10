"""Check legacy-builder routing and selected upstream Docker dependencies."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import apply_branch_patches

COMMIT = "ff3c0c9f686a1d0dbddbe813b233b3b7a51bd8e2"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/Makefile"
TARGETS = ("configure", "target/docker-sonic-vs.gz", "target/sonic-vs.img.gz",
           "target/sonic-broadcom.bin")

with tempfile.TemporaryDirectory() as directory:
    work = Path(directory).resolve()
    source = work / "source"
    source.mkdir()
    original = urllib.request.urlopen(URL, timeout=60).read().decode()
    (source / "Makefile").write_text(original)
    binding = source / "src/sonic-sairedis/pyext/py3/Makefile.am"
    binding.parent.mkdir(parents=True)
    binding.write_bytes(urllib.request.urlopen(
        "https://raw.githubusercontent.com/sonic-net/sonic-sairedis/"
        "14db8ad4452f70f375795b2d7d7bf797d2c012b5/pyext/py3/Makefile.am",
        timeout=60).read())
    template = source / "sonic-slave-bookworm/Dockerfile.j2"
    template.parent.mkdir()
    template.write_bytes(urllib.request.urlopen(
        f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/"
        "sonic-slave-bookworm/Dockerfile.j2", timeout=60).read())
    (source / "build_image.sh").write_bytes(urllib.request.urlopen(
        f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/"
        "build_image.sh", timeout=60).read())
    upstream_image_script = (source / "build_image.sh").read_text()
    installer = urllib.request.urlopen(
        f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/"
        "installer/install.sh", timeout=60).read().decode()
    guard = installer.split("# Verify image platform is inside devices list\n", 1)[1].split(
        "# If running in ONIE", 1)[0]
    virtual = source / "device/virtual/x86_64-kvm_x86_64-r0"
    virtual.mkdir(parents=True)
    hardware = source / "device/vendor/wrong-asic-platform"
    hardware.mkdir(parents=True)
    (hardware / "platform_asic").write_text("broadcom\n")

    def platform_list(script, configured, target):
        function = re.search(r"generate_device_list\(\)\n\{.*?\n\}", script, re.S).group()
        subprocess.run(["bash", "-eu", "-c", function + '\ngenerate_device_list platforms_asic'],
                       cwd=source, check=True,
                       env={**os.environ, "CONFIGURED_PLATFORM": configured, "TARGET_MACHINE": target})
        return set((source / "platforms_asic").read_text().splitlines())

    def installer_accepts(platform):
        return subprocess.run(["bash", "-eu", "-c", guard], cwd=source,
                              env={**os.environ, "install_env": "onie", "onie_platform": platform},
                              input="n\n", text=True, capture_output=True).returncode == 0

    # A restored .deb does not run the package recipe that creates this mapping.
    assert virtual.name not in platform_list(upstream_image_script, "vs", "vs")
    assert not installer_accepts(virtual.name)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    # Keep the real upstream Makefile and recursive make selection. Replace only
    # the expensive leaf build commands with a trace of the environment/target.
    (source / "Makefile.work").write_text(
        '%:\n\t@printf "VISIT %s %s\\n" "$(BLDENV)" "$@"\n')
    retry = source / "scripts/run_with_retry"
    retry.parent.mkdir()
    retry.write_text('#!/bin/sh\nexec "$@"\n')
    retry.chmod(0o755)
    environment = os.environ.copy()
    for name in ("NOBUSTER", "NOBULLSEYE", "NOBOOKWORM", "NOJESSIE", "NOSTRETCH",
                 "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "PLATFORM", "BLDENV"):
        environment.pop(name, None)

    def visits(target, *options):
        result = subprocess.check_output(
            ["make", "-rR", "--no-print-directory", *options, target,
             "PLATFORM=vs", "PLATFORM_ARCH=amd64"], cwd=source,
            env=environment, text=True)
        return [line.split()[1:] for line in result.splitlines()
                if line.startswith("VISIT ")]

    for target in TARGETS:
        before = visits(target)
        assert {item[0] for item in before} == {"buster", "bullseye", "bookworm"}, (target, before)
    result = apply_branch_patches(source, work, "202505")
    assert len(result) == 4 and all(item["status"] == "applied" for item in result), result
    patched_image_script = (source / "build_image.sh").read_text()
    for configured in ("vs", "vpp"):
        (virtual / "platform_asic").unlink(missing_ok=True)
        assert platform_list(patched_image_script, configured, configured) == {virtual.name}
        assert (virtual / "platform_asic").read_text() == configured + "\n"
        assert installer_accepts(virtual.name)
        assert not installer_accepts(hardware.name)
    assert platform_list(patched_image_script, "vs", "broadcom") == {hardware.name}
    assert not installer_accepts(virtual.name)
    assert platform_list(patched_image_script, "vs", "generic") == {virtual.name, hardware.name}
    print("Cached KVM platform mapping restored for VS/VPP; installer ASIC guard preserved")
    assert (source / "Makefile").read_text() == original.replace(
        "NOBUSTER ?= 0\n", "NOBUSTER ?= 1\n", 1).replace(
        "NOBULLSEYE ?= 0\n", "NOBULLSEYE ?= 1\n", 1)
    for target in TARGETS:
        after = visits(target)
        assert {item[0] for item in after} == {"bookworm"}, (target, after)
        assert ["bookworm", target] in after, (target, after)
        assert any(item[0] == "buster" for item in visits(target, "NOBUSTER=0"))
        assert any(item[0] == "bullseye" for item in visits(target, "NOBULLSEYE=0"))
        print(f"{target}: Bookworm retained; both explicit legacy overrides preserved")
    assert apply_branch_patches(source, work, "202505")[0]["status"] == "already-applied"

    # Evaluate upstream Docker declarations rather than assuming that a Bookworm
    # host image cannot contain containers built on an older distribution.
    tree_url = f"https://api.github.com/repos/sonic-net/sonic-buildimage/git/trees/{COMMIT}?recursive=1"
    tree = json.load(urllib.request.urlopen(tree_url, timeout=60))["tree"]
    paths = {entry["path"] for entry in tree if entry["type"] == "blob" and (
        (entry["path"].startswith("rules/docker-") and entry["path"].endswith(".mk")) or
        (entry["path"].count("/") == 2 and entry["path"].endswith(".mk") and
         entry["path"].startswith(("platform/vs/", "platform/broadcom/"))))}
    paths.update({"rules/config", "platform/template/docker-syncd-bookworm.mk",
                  "platform/template/docker-gbsyncd-bookworm.mk"})
    paths.update(f"platform/components/docker-gbsyncd-{name}.mk"
                 for name in ("credo", "broncos", "milleniob"))

    def fetch(path):
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/{path}"
        target.write_bytes(urllib.request.urlopen(url, timeout=60).read())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, sorted(paths)))
    (source / "docker-graph.mk").write_text('''
PLATFORM_PATH = platform/$(PLATFORM)
DOCKERS_PATH = dockers
CONFIGURED_ARCH = amd64
CONFIGURED_PLATFORM = $(PLATFORM)
BLDENV = bookworm
DBG_IMAGE_MARK = dbg
include rules/config
include rules/docker-*.mk
include $(PLATFORM_PATH)/rules.mk
.PHONY: probe
probe:
\t@printf 'DOCKER|%s\\n' '$(DOCKER_SONIC_VS)'
\t@printf 'KVM|%s\\n' '$($(SONIC_KVM_IMAGE)_DOCKERS)'
\t@printf 'BROADCOM|%s\\n' '$($(SONIC_ONE_IMAGE)_DOCKERS)'
\t@printf 'LEGACY|%s\\n' '$(SONIC_BUSTER_DOCKERS) $(SONIC_BULLSEYE_DOCKERS)'
\t@$(foreach image,$(sort $(SONIC_DOCKER_IMAGES)),printf '%s|%s\\n' '$(image)' '$($(image)_LOAD_DOCKERS) $($(image)_AFTER)';)
''')
    for platform, selected in (("vs", ("DOCKER", "KVM")), ("broadcom", ("BROADCOM",))):
        output = subprocess.check_output(
            ["make", "-rR", "-s", "-f", "docker-graph.mk", "probe", f"PLATFORM={platform}"],
            cwd=source, env=environment, text=True)
        graph = {name: set(value.split()) for name, value in
                 (line.split("|", 1) for line in output.splitlines() if "|" in line)}
        for name in selected:
            assert graph[name], (platform, name, "missing output container roots")
            pending = list(graph[name])
            reachable = set()
            while pending:
                image = pending.pop()
                if image in reachable:
                    continue
                assert image in graph, (platform, name, "unknown Docker dependency", image)
                reachable.add(image)
                pending.extend(graph[image])
            assert not reachable & graph["LEGACY"], (platform, name, reachable & graph["LEGACY"])
            print(f"{platform}/{name}: {len(reachable)} runtime layers; no legacy-builder dependency")

print("Pinned upstream configure, target routing and Docker dependency graphs passed")
