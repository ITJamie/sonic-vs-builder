"""Exercise cold/cache-hit Python ownership differences on Debian Bookworm."""

from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).parents[1]
COMMIT = "ff3c0c9f686a1d0dbddbe813b233b3b7a51bd8e2"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/"
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory).resolve()
    template = work / "sonic-slave-bookworm/Dockerfile.j2"
    template.parent.mkdir()
    template.write_bytes(urllib.request.urlopen(URL + "sonic-slave-bookworm/Dockerfile.j2", timeout=60).read())
    constraints = urllib.request.urlopen(
        URL + "files/build/versions/build/build-sonic-slave-bookworm/versions-py3", timeout=60).read().decode()
    (work / "constraints").write_text(constraints)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    patch = ROOT / "patches/202505/0003-prepare-cached-wheel-python-dependencies.patch"
    subprocess.run(["git", "-C", str(work), "apply", "--check", str(patch)], check=True)
    subprocess.run(["git", "-C", str(work), "apply", str(patch)], check=True)
    subprocess.run(["git", "-C", str(work), "apply", "--reverse", "--check", str(patch)], check=True)
    command = re.search(r"(?m)^RUN (pip3 install --ignore-installed --no-deps .+)$", template.read_text()).group(1)
    for package in command.split()[4:]:
        assert package in constraints.splitlines(), package
    (work / "probe.py").write_text('''
import importlib.metadata
import pathlib
import subprocess
import sys

pip = [sys.executable, '-m', 'pip', '--disable-pip-version-check', 'install', '--break-system-packages']
debian_before = subprocess.check_output(['dpkg', '-V', 'python3-pexpect', 'python3-pycparser'])
# A cache hit has no cold-build user-site dependency preparation. Both pinned
# upgrades must reproduce the Debian distutils uninstall failure independently.
for package in ('pexpect==4.9.0', 'pycparser==3.0'):
    result = subprocess.run([*pip, '--no-deps', package], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode != 0 and 'distutils' in result.stdout, result.stdout
    print('Reproduced cached-wheel ownership failure: ' + package, flush=True)
subprocess.run(COMMAND, shell=True, check=True)
# Ordinary dependency resolution remains active on the subsequent wheel-install
# path; no blanket ignore-installed/no-deps option is added to that path.
subprocess.run([*pip, '-c', '/probe/constraints', 'pexpect>=4.8.0', 'pycparser',
                'cffi>=2.0.0', 'click==7.0', 'pyroute2==0.7.12', 'urllib3>=2',
                'wcwidth>=0.7'], check=True)
for name, version in (('pexpect', '4.9.0'), ('pycparser', '3.0')):
    assert importlib.metadata.version(name) == version
    module = __import__(name)
    assert module.__file__.startswith('/usr/local/'), module.__file__
# The Debian-owned copies remain installed and byte-for-byte unchanged.
subprocess.run(['dpkg-query', '-W', 'python3-pexpect', 'python3-pycparser'], check=True)
debian_after = subprocess.check_output(['dpkg', '-V', 'python3-pexpect', 'python3-pycparser'])
assert debian_after == debian_before, (debian_before, debian_after)
import pexpect
assert pexpect.run('/bin/echo cache-hit').strip() == b'cache-hit'
from pycparser import c_parser
assert c_parser.CParser().parse('int main(void) { return 0; }')
subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)
# Repeating the regular install also succeeds, as it must on a warm build.
subprocess.run([*pip, '-c', '/probe/constraints', 'pexpect', 'pycparser'], check=True)
print('Pinned imports, dependency checks and Debian file preservation passed', flush=True)
'''.replace('COMMAND', repr(command)))
    (work / "Dockerfile").write_text(
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends python3-pip "
        "python3-pexpect python3-pycparser python3-click python3-pyroute2 python3-urllib3 python3-wcwidth\n"
        "RUN python3 -m pip install --break-system-packages pip==24.2\n"
        "ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONNOUSERSITE=1\n"
        "COPY constraints probe.py /probe/\n"
        "RUN python3 -u /probe/probe.py\n")
    tag = "sonic-vs-builder-cached-wheel-test:" + uuid.uuid4().hex[:12]
    try:
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "--progress=plain",
                        "-t", tag, str(work)], check=True)
    finally:
        subprocess.run(["docker", "image", "rm", tag], check=False)
