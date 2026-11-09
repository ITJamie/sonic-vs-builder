"""Exercise the shipped Buster skip patch against pinned upstream make routing."""

from pathlib import Path
import os
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
        assert any(item[0] == "buster" for item in before), (target, before)
    result = apply_branch_patches(source, work, "202505")
    assert len(result) == 1 and result[0]["status"] == "applied", result
    assert (source / "Makefile").read_text() == original.replace(
        "NOBUSTER ?= 0\n", "NOBUSTER ?= 1\n", 1)
    for target in TARGETS:
        after = visits(target)
        assert {item[0] for item in after} == {"bullseye", "bookworm"}, (target, after)
        assert ["bookworm", target] in after, (target, after)
        assert any(item[0] == "buster" for item in visits(target, "NOBUSTER=0"))
        print(f"{target}: Buster skipped; Bullseye/Bookworm and explicit override preserved")
    assert apply_branch_patches(source, work, "202505")[0]["status"] == "already-applied"

print("Pinned upstream configure and all three target routes passed")
