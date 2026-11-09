"""Reproduce deleted groff device data and verify compiler inputs survive cleanup."""

from pathlib import Path
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).parents[1]
COMMIT = "8076a44fa579fde75f73299f03e7f3cac298d9e3"
with tempfile.TemporaryDirectory() as directory:
    source = Path(directory)
    hook = source / "src/sonic-build-hooks/scripts/post_run_cleanup"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(urllib.request.urlopen(
        f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/src/sonic-build-hooks/scripts/post_run_cleanup",
        timeout=60).read())
    original = source / "original-cleanup"
    original.write_bytes(hook.read_bytes())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "apply", str(ROOT / "patches/202605/0003-preserve-groff-build-inputs.patch")], check=True)
    (source / "buildinfo_base.sh").write_text('''
BUILD_VERSION_PATH=/tmp/build-versions
VERSION_DEB_PREFERENCE=/tmp/version-preference
get_version_cache_option() { :; }
symlink_build_hooks() { :; }
set_reproducible_mirrors() { :; }
''')
    (source / "Dockerfile").write_text(r'''
FROM debian:trixie-slim AS tools
RUN apt-get update && apt-get install -y --no-install-recommends groff ghostscript fakeroot
COPY buildinfo_base.sh /usr/local/share/buildinfo/scripts/buildinfo_base.sh
COPY original-cleanup /original-cleanup
COPY src/sonic-build-hooks/scripts/post_run_cleanup /fixed-cleanup
RUN printf '.TH TEST 1\n.SH NAME\nregression test\n' > /probe.1
FROM tools AS broken
RUN bash /original-cleanup sonic-slave-trixie
RUN if groff -man /probe.1 > /broken.ps 2>/broken.log; then echo 'Expected upstream groff failure'; exit 1; fi; grep "DESC" /broken.log
FROM tools AS fixed
RUN bash /fixed-cleanup sonic-slave-trixie
RUN fakeroot sh -ec 'groff -man /probe.1 > /probe.ps; ps2pdf /probe.ps /probe.pdf; test -s /probe.pdf'
FROM tools AS runtime
RUN bash /fixed-cleanup docker-runtime
RUN test -z "$(find /usr/share/groff -type f -print -quit)"
''')
    for target in ("broken", "fixed", "runtime"):
        subprocess.run(["docker", "build", "--progress=plain", "--target", target, str(source)], check=True)
print("Trixie regression reproduced; patched cleanup preserves groff/PDF compilation under fakeroot; runtime cleanup remains enabled")
