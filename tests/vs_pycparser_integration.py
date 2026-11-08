"""Reproduce Debian/pip ownership conflict, then exercise the shipped VS fix."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import archive_image, load_untagged

parser = argparse.ArgumentParser()
parser.add_argument("--cache-root", type=Path, help="Also install actual cached SONiC wheels on the cached SWSS base")
args = parser.parse_args()
COMMIT = "8076a44fa579fde75f73299f03e7f3cac298d9e3"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/"
ROOT = Path(__file__).parents[1]
tag = "sonic-vs-builder-pycparser-test:" + uuid.uuid4().hex[:12]
loaded = None


def extract_cached(cache, filename, destination):
    candidates = sorted(cache.glob(filename + "-*.tgz"), key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        with tarfile.open(candidate, "r:gz") as archive:
            matches = [item for item in archive.getmembers() if item.isfile() and Path(item.name).name == filename]
            if len(matches) == 1:
                with archive.extractfile(matches[0]) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                return
    raise ValueError(f"No cached output found for {filename}")


with tempfile.TemporaryDirectory() as directory:
    work = Path(directory).resolve()
    template = work / "platform/vs/docker-sonic-vs/Dockerfile.j2"
    template.parent.mkdir(parents=True)
    template.write_bytes(urllib.request.urlopen(URL + "platform/vs/docker-sonic-vs/Dockerfile.j2", timeout=60).read())
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    patch = ROOT / "patches/202605/0002-fix-vs-pycparser-package-ownership.patch"
    subprocess.run(["git", "-C", str(work), "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "-C", str(work), "apply", str(patch)], check=True)
    removal = re.search(r"(?m)^RUN (apt-get remove -y python3-pycparser)$", template.read_text()).group(1)
    wheels = []
    base = "debian:bookworm-slim"
    if args.cache_root:
        cache = args.cache_root.resolve()
        archive = work / "base.gz"
        extract_cached(cache, "docker-swss-layer-bookworm.gz", archive)
        archive_image(archive)
        previous = set(subprocess.check_output(["docker", "image", "ls", "--all", "--quiet", "--no-trunc"], text=True).splitlines())
        identity = load_untagged(archive)
        if identity not in previous:
            loaded = identity
        base = tag + "-base"
        subprocess.run(["docker", "tag", identity, base], check=True)
        archive.unlink()
        for name, version in (("sonic_py_common", "1.0"), ("sonic_platform_common", "1.0"),
                              ("sonic_yang_models", "1.0"), ("sonic_yang_mgmt", "1.0"),
                              ("sonic_utilities", "1.2"), ("sonic_host_services", "1.0")):
            filename = f"{name}-{version}-py3-none-any.whl"
            extract_cached(cache, filename, work / filename)
            wheels.append("/probe/" + filename)
    (work / "constraints").write_bytes(urllib.request.urlopen(
        URL + "files/build/versions-public/dockers/docker-sonic-vs/versions-py3", timeout=60).read())
    dockerfile = work / "Dockerfile"
    dockerfile.write_text(f"FROM {base}\n"
                         "RUN apt-get update && apt-get install -y python3-pip python3-pycparser "
                         "build-essential python3-dev libssl-dev swig libcairo2-dev libdbus-1-dev "
                         "libgirepository1.0-dev libsystemd-dev pkg-config python3-gi gir1.2-glib-2.0\n"
                         "RUN python3 -m pip install --break-system-packages pip==26.3 meson-python meson ninja patchelf\n")
    probe = '''
import importlib.metadata
import subprocess
import sys
pip = [sys.executable, "-m", "pip", "--disable-pip-version-check", "install", "--break-system-packages"]
baseline = subprocess.run([*pip, "pycparser==3.0"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
assert baseline.returncode != 0 and "distutils" in baseline.stdout and "pycparser" in baseline.stdout, baseline.stdout
print("Reproduced pycparser 2.21 distutils uninstall failure", flush=True)
subprocess.run(REMOVAL, shell=True, check=True)
subprocess.run([*pip, *PACKAGES], check=True)
assert importlib.metadata.version("pycparser") == "3.0"
from pycparser import c_parser
assert c_parser.CParser().parse("int main(void) { return 0; }")
import cffi
import cryptography
assert importlib.metadata.version("cffi") == "2.1.1"
assert importlib.metadata.version("cryptography") == "50.0.2"
subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
print("Pinned pycparser, CFFI and cryptography install/import checks passed", flush=True)
'''
    packages = ["-c", "/probe/constraints", *wheels] if wheels else ["pycparser==3.0", "cffi==2.1.1", "cryptography==50.0.2"]
    probe = "REMOVAL=" + repr(removal) + "\nPACKAGES=" + repr(packages) + "\n" + probe
    try:
        subprocess.run(["docker", "build", "-t", tag, str(work)], check=True)
        subprocess.run(["docker", "run", "--rm", "-i", "--entrypoint", "python3", "--mount",
                        f"type=bind,source={work},target=/probe,readonly", tag, "-u", "-"],
                       input=probe, text=True, check=True)
    finally:
        subprocess.run(["docker", "image", "rm", tag], check=False)
        if args.cache_root:
            subprocess.run(["docker", "image", "rm", base], check=False)
        if loaded:
            subprocess.run(["docker", "image", "rm", loaded], check=False)
