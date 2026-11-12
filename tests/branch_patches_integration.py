"""Apply the shipped patch to pinned upstream and reproduce/fix pip resolution."""

from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import apply_branch_patches

COMMIT = "8076a44fa579fde75f73299f03e7f3cac298d9e3"
CONSTRAINTS = "files/build/versions-public/build/build-sonic-slave-bookworm/versions-py3"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/{CONSTRAINTS}"


def relevant(text):
    return "\n".join(line for line in text.splitlines()
                     if line.startswith(("blessed==", "enlighten==", "wcwidth=="))) + "\n"


with tempfile.TemporaryDirectory() as directory:
    work = Path(directory).resolve()
    source = work / "source"
    target = source / CONSTRAINTS
    target.parent.mkdir(parents=True)
    original = urllib.request.urlopen(URL, timeout=60).read().decode()
    target.write_text(original)
    template = source / "platform/vs/docker-sonic-vs/Dockerfile.j2"
    template.parent.mkdir(parents=True)
    template_url = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/platform/vs/docker-sonic-vs/Dockerfile.j2"
    template.write_text(urllib.request.urlopen(template_url, timeout=60).read().decode())
    hook = source / "src/sonic-build-hooks/scripts/post_run_cleanup"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(urllib.request.urlopen(f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/src/sonic-build-hooks/scripts/post_run_cleanup", timeout=60).read())
    sdk_path = "device/accton/x86_64-accton_as7326_56x-r0/Accton-AS7326-56X/td3-as7326-48x25G+8x100G.config.bcm"
    sdk = source / sdk_path
    sdk.parent.mkdir(parents=True)
    sdk_original = urllib.request.urlopen(f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/{sdk_path}", timeout=60).read().decode()
    sdk.write_text(sdk_original)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    result = apply_branch_patches(source, work, "202605")
    assert len(result) == 4 and all(item["status"] == "applied" for item in result)
    assert sdk.read_text() == sdk_original.replace(
        "stable_size=0x5500000\n", "stable_size=0x8000000\n"
    ).replace(
        "sai_load_hw_config=/etc/bcm/flex/bcm56870_a0_premium_issu/b870.6.4.1/\n",
        "sai_load_hw_config=/etc/bcm/flex/bcm56870_a0_premium_issu/default/\n",
    )
    assert template.read_text().index("RUN apt-get remove -y python3-pycparser") < template.read_text().index("# Install locally-built Python wheel dependencies")
    assert target.read_text() == original.replace("wcwidth==0.7.0\n", "wcwidth==0.9.2\n")
    assert all(item["status"] == "already-applied" for item in apply_branch_patches(source, work, "202605"))
    (work / "broken.txt").write_text(relevant(original))
    (work / "fixed.txt").write_text(relevant(target.read_text()))
    probe = '''
import importlib.metadata
import subprocess
import sys

pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
broken = subprocess.run([*pip, "install", "--dry-run", "-r", "/probe/broken.txt"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
assert broken.returncode != 0, broken.stdout
assert "ResolutionImpossible" in broken.stdout and "wcwidth" in broken.stdout, broken.stdout
print("Reproduced original upstream pip dependency conflict", flush=True)
subprocess.run([*pip, "install", "-r", "/probe/fixed.txt"], check=True)
subprocess.run([*pip, "check"], check=True)
assert importlib.metadata.version("wcwidth") == "0.9.2"
assert importlib.metadata.version("blessed") == "1.52.0"
assert importlib.metadata.version("enlighten") == "1.14.1"
from blessed import Terminal
from wcwidth import wcswidth
import enlighten
assert Terminal().length("hello") == 5
assert wcswidth("hello") == 5
manager = enlighten.get_manager()
manager.stop()
print("Patched constraints resolve; pip check and dependency smoke checks passed", flush=True)
'''
    subprocess.run(["docker", "run", "--rm", "-i", "--platform", "linux/amd64",
                    "--mount", f"type=bind,source={work},target=/probe,readonly",
                    "python:3.11-slim-bookworm", "python", "-u", "-"], input=probe, text=True, check=True)
