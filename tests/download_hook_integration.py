"""Test the exact upstream cached-download function without external downloads."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import fix_download_cache

COMMIT = "bafb878dcd083e9650672f1a870089d03f1244d0"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/src/sonic-build-hooks/scripts/buildinfo_base.sh"
upstream = urllib.request.urlopen(URL, timeout=60).read().decode()


def exercise(text, root, cached=False):
    root.mkdir()
    function = text[text.index("download_packages()\n{"):text.index("\nrun_pip_command()")]
    (root / "cache/web").mkdir(parents=True)
    url = "https://example.invalid/source" if cached else "https://packages.trafficmanager.net/public/socat.dsc"
    (root / "versions").write_text(f"{url}==knownhash\n")
    if cached:
        (root / "cache/web/source-knownhash.tgz").write_text("cached data")
    environment = dict(os.environ, ROOT=str(root), TEST_URL=url)
    script = '''
PKG_CACHE_PATH="$ROOT/cache"
URL_PREFIX=https://packages.trafficmanager.net/
WEB_VERSION_FILE="$ROOT/versions"
BUILD_WEB_VERSION_FILE="$ROOT/build-versions"
ENABLE_VERSION_CONTROL_WEB=y
GET_RETRY_COUNT=1
get_version_cache_option() { echo wcache; }
log_info() { :; }
mock_download() { echo download >> "$ROOT/calls"; echo 'downloaded data' > "$2"; }
REAL_COMMAND=mock_download
''' + function + '\ndownload_packages -NO "$ROOT/output" "$TEST_URL"\n'
    subprocess.run(["bash", "-ec", script], env=environment, check=True)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    # Reproduce the real bug: unmodified upstream claims success, no file exists.
    exercise(upstream, root / "broken")
    assert not (root / "broken/output").exists()
    source = root / "source"
    hook = source / "src/sonic-build-hooks/scripts/buildinfo_base.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text(upstream)
    template = source / "sonic-slave-bookworm/Dockerfile.j2"
    template.parent.mkdir()
    template.write_text("FROM debian:bookworm-slim\n")
    assert fix_download_cache(source, "read-write") == ["download-cache-mirror-url-restore"]
    fixed = hook.read_text()
    exercise(fixed, root / "mirror")
    assert (root / "mirror/output").read_text().strip() == "downloaded data"
    exercise(fixed, root / "hit", cached=True)
    assert (root / "hit/output").read_text() == "cached data"
    assert not (root / "hit/calls").exists(), "A confirmed cache hit downloaded again"
    assert "download-cache compatibility" in template.read_text()
    assert fix_download_cache(source, "read-write") == [], "Fix should be idempotent"
print("Upstream mirror-URL regression reproduced; corrected download and warm cache hit passed")
